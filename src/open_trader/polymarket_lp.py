"""Durable, single-market Polymarket liquidity-provider session."""

from __future__ import annotations

import logging
import math
import threading
import uuid
from copy import deepcopy
from collections.abc import Callable, Collection, Mapping, MutableMapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

from .polymarket_lp_risk import (
    BOOK_FRESHNESS_SECONDS,
    LP_QUEUE_PROTECTION_THRESHOLD,
    TERMINAL_ORDER_STATES,
    _account_after_reservations,
    _decimal,
    _executable_bid_value,
    _field,
    _freshness,
    _has_market_order,
    _items,
    _levels,
    _maybe_decimal,
    _projected_taker_fee,
    _qualify_reward_quote,
    _timestamp,
    estimate_lp_queue_position,
    estimate_lp_target_share_yield,
    evaluate_lp_entry,
    first_observation_baseline,
)
from .prediction_arbitrage_store import (
    LP_RESERVED_MANUAL_SESSION_ID,
    PredictionArbitrageStore,
)
from .notifications import beijing_clock


STOP_LOSS = Decimal("5")
SCORING_STALE_SECONDS = Decimal("15")
# Issue 163 定案 8 / Issue 166: an augment can only land on ``entry_open``.
# Every other non-terminal state maps to its real rejection reason; the two
# augment entry points (single-shot submit_augment and the legacy two-phase
# _augment_session) share this one table so the two paths never drift.
_LP_AUGMENT_STATE_BLOCKS = {
    "review": "session_review_exit",
    "stop_loss_exit": "session_stop_loss_exit",
    "needs_attention": "session_needs_attention",
    "passive_exit": "session_passive_exit",
    "entry_submit_pending": "session_entry_pending",
}
SCORING_POLL_SECONDS = Decimal("5")
SCORING_FAILURE_WINDOW_SECONDS = Decimal("60")
GTD_REVIEW_BUFFER_SECONDS = 60
SDK_MIN_EXPIRATION_SECONDS = 180
PREVIEW_TTL_SECONDS = 10
REWARD_THRESHOLD = Decimal("1")
REWARD_STALE_SECONDS = Decimal("180")
LP_CANDIDATE_REFRESH_SECONDS = Decimal("300")
LP_RECOMMENDATION_REFRESH_SECONDS = Decimal("60")
# Issue #146: maintenance refreshes the head's sources on the oldest source
# age (30-second lead) instead of waiting for the 60-second expiry.
LP_RECOMMENDATION_REFRESH_LEAD_SECONDS = Decimal("30")
# Issue #146: after consecutive maintenance failures the next attempt waits
# 60/120/300 seconds counted from the attempt's finish; three or more
# consecutive failures hold at 300 seconds. A success resets the schedule.
_CANDIDATE_MAINTENANCE_BACKOFF_SECONDS = (
    Decimal("60"),
    Decimal("120"),
    Decimal("300"),
)
# Issue #146: scheduler waits never spin below one second and never exceed
# the 300-second scan cadence.
_CANDIDATE_SCHEDULER_WAIT_FLOOR_SECONDS = 1.0
_CANDIDATE_SCHEDULER_WAIT_CEILING_SECONDS = 300.0
# Issue #157 rolling pool: an estimate stays valid for five minutes from its
# own judgment time; expiry is computed against the current clock on every
# read and publish, never by a background event.
LP_CANDIDATE_VALIDITY_SECONDS = Decimal("300")
# Issue #157: one exploration batch handles at most ten distinct markets in
# one order-book read (binary markets: at most 20 tokens).
_LP_CANDIDATE_BATCH_SIZE = 10
# Issue #157: the exploration loop never polls faster than one batch per two
# seconds; the runtime scheduler clamps its wait to this floor.
_LP_CANDIDATE_BATCH_MIN_INTERVAL_SECONDS = 2.0
# Issue #157: official competition refreshes on its own thread; candidate
# paths only read the in-memory cache and never block on a competition read.
# #181: 密度是慢变量，小时级节奏。
_LP_COMPETITION_REFRESH_SECONDS = 3600
# Issue #157: pool publications persist at most once every five seconds.
_LP_CANDIDATE_SNAPSHOT_SAVE_MIN_INTERVAL_SECONDS = 5.0
_LP_BOOK_SAMPLE_BATCH_SIZE = 100
_LP_BOOK_SAMPLE_MAX_CONCURRENCY = 8
_LP_PRICE_HISTORY_BATCH_SIZE = 20
_LP_PRICE_HISTORY_MAX_CONCURRENCY = 4
_LP_PRICE_HISTORY_WINDOW = timedelta(hours=24)
_LP_PRICE_HISTORY_OVERLAP = timedelta(minutes=1)
_LP_METADATA_BATCH_SIZE = 1500
# Keep transient preparation failures recoverable while still bounding the
# amount of upstream work during an outage.  The final interval is repeated
# indefinitely until a validated read succeeds or an operator blocker is
# classified.
_LP_PREPARATION_RETRY_DELAYS_SECONDS = (300, 600, 1200, 1800, 1800)
_BEIJING = ZoneInfo("Asia/Shanghai")
TERMINAL_TRADE_STATES = frozenset({"CONFIRMED", "FAILED"})
# Issue 152: consecutive tick-level data outages (snapshot/book unknown) turn
# into a conservative cancel of the protected BUY once this limit is reached.
LP_PROTECTION_DATA_FAILURE_LIMIT = 10
# 「需要核对」克制通知：进入该状态满 5 分钟仍未自愈才经 _notify_protection
# 推一条；同一次进入（episode）只推一条，恢复后清键重置。
LP_NEEDS_ATTENTION_NOTIFY_SECONDS = 300
# Issue 152: only these tick early-exit reasons count as data outages;
# identity/holding conflicts are decision blockers, not data outages.
_QUEUE_DATA_FAILURE_REASONS = frozenset(
    {"external_snapshot_unknown", "book_unknown", "book_freshness_unknown"}
)

logger = logging.getLogger(__name__)


class _MutationBlocked(RuntimeError):
    """The shared execution guard currently forbids an exchange mutation."""


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _reward_accrual_rows(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    rows: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        amount = _maybe_decimal(item.get("amount"))
        asset = _text(item.get("asset"))
        if amount is None or amount < 0 or asset is None:
            continue
        row: dict[str, object] = {"amount": amount, "asset": asset}
        address = _text(item.get("asset_address"))
        if address is not None:
            row["asset_address"] = address
        rows.append(row)
    return tuple(rows)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _first_seen_stamp_key(value: object) -> float:
    """Anchor tie-break sort key: venue stamps ascend, missing stamps last."""

    stamp = value if isinstance(value, datetime) else None
    if stamp is None:
        return float("inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


def _first_seen_stamp_value(value: object, *, default: datetime) -> datetime:
    """Parse a first-seen stamp, accepting ISO strings and datetimes."""

    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if text:
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
        except ValueError:
            pass
    return default


def _queue_decimal_text(value: Decimal | None) -> str:
    """Render a share/price amount without trailing zeros (issue 152)."""

    if value is None:
        return "UNKNOWN"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _queue_ratio_percent_text(ratio: Decimal | None) -> str:
    """Render an A ratio as a two-decimal percent without trailing zeros."""

    if ratio is None:
        return "UNKNOWN"
    return _queue_decimal_text(
        (ratio * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def _queue_protection_level_buckets(
    protection: object,
    *,
    default_order_id: str = "",
) -> dict[str, dict[str, object]]:
    """Issue 167 D6: normalize any queue-protection payload into buckets.

    Three-tier fallback, and it never raises:
    - missing/invalid payload → ``{}`` (protection not enabled, the old
      ``.get`` guard semantics);
    - v2 ``levels`` mapping → per-price buckets; a bucket whose
      baseline_price or order id is unreadable is marked ``unknown`` alone
      (its group's other buckets and the group accounting keep working);
    - legacy scalar shape (baseline_price, no levels) → wrapped as one
      bucket so old sessions keep running; the next payload write lands in
      the v2 shape naturally.

    Keys are the canonical ``format(price, "f")`` texts; bucket lookups by
    price must compare ``baseline_price`` values, not keys.
    """

    if not isinstance(protection, Mapping):
        return {}
    raw_levels = protection.get("levels")
    if isinstance(raw_levels, Mapping):
        buckets: dict[str, dict[str, object]] = {}
        for raw_key, raw_value in raw_levels.items():
            key = str(raw_key)
            if not isinstance(raw_value, Mapping):
                buckets[key] = {
                    "state": "unknown",
                    "reason_codes": ["level_payload_invalid"],
                }
                continue
            bucket = dict(raw_value)
            if (
                _maybe_decimal(bucket.get("baseline_price")) is None
                or not str(bucket.get("order_id") or "").strip()
            ):
                bucket["state"] = "unknown"
            buckets[key] = bucket
        return buckets
    price = _maybe_decimal(protection.get("baseline_price"))
    if price is None:
        return {}
    bucket = dict(protection)
    # Group-level keys never live inside a bucket: the v2 payload keeps the
    # outage counter at the top, and a stale legacy copy must not leak into
    # the wrapped bucket.
    bucket.pop("levels", None)
    bucket.pop("version", None)
    bucket.pop("data_failures", None)
    bucket.setdefault("order_id", default_order_id)
    return {format(price, "f"): bucket}


def _queue_group_failures_int(value: object) -> int:
    """Normalize the group-level outage counter to a durable JSON number."""

    parsed = _maybe_decimal(value)
    return int(parsed) if parsed is not None else 0


def queue_protection_status_view(protection: object) -> dict[str, object]:
    """Normalized v2 view of a queue-protection payload for read paths.

    ``levels`` carries the per-price buckets (D6 three-tier fallback); with
    exactly one bucket its scalar keys merge onto the view so the legacy
    single-bucket external contract keeps working unchanged.
    """

    levels = _queue_protection_level_buckets(protection)
    failures = (
        _maybe_decimal(protection.get("data_failures"))
        if isinstance(protection, Mapping)
        else None
    )
    view: dict[str, object] = (
        dict(next(iter(levels.values()))) if len(levels) == 1 else {}
    )
    view["version"] = 2
    view["data_failures"] = failures if failures is not None else 0
    view["levels"] = levels
    return view


def _report_boundary_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _lp_reward_terms(
    market_metadata: Mapping[str, object],
    reward_market: Mapping[str, object],
) -> tuple[object, object]:
    """Use fresh market rules, falling back to this read's reward rules."""

    reward_minimum = market_metadata.get("reward_min_size")
    if reward_minimum is None:
        reward_minimum = reward_market.get("rewards_min_size")
    reward_spread = market_metadata.get("reward_max_spread")
    if reward_spread is None:
        raw_spread = _maybe_decimal(reward_market.get("rewards_max_spread"))
        reward_spread = None if raw_spread is None else raw_spread / Decimal("100")
    return reward_minimum, reward_spread


def _lp_direction_fact(
    market_meta: Mapping[str, object],
    reward_market: Mapping[str, object],
    *,
    condition_id: str,
    token_id: str,
    outcome: str,
    reward_checked_at: object,
    reward_guidance_deadline: object,
    event_end_confirmation: object,
    history_summary: object,
    account: Mapping[str, object] | None,
) -> dict[str, object]:
    """Build one YES/NO direction fact from market and reward rows.

    Single copy of the direction-construction formula, shared by the
    round-start build and the issue-143 batch renewal, so both produce
    identical market fields (including the ``_lp_reward_terms`` minimum and
    spread resolution and the reward-catalog overlays) from the same code.
    """

    reward_minimum, reward_spread = _lp_reward_terms(market_meta, reward_market)
    market: dict[str, object] = {
        **dict(market_meta),
        "condition_id": condition_id,
        "token_id": token_id,
        "outcome": outcome,
        "reward_min_size": reward_minimum,
        "reward_max_spread": reward_spread,
    }
    if "rewards_min_size" in reward_market:
        market["_reward_catalog_min_size"] = reward_market.get("rewards_min_size")
    if "rewards_max_spread" in reward_market:
        market["_reward_catalog_max_spread"] = reward_market.get("rewards_max_spread")
    if market_meta.get("reward_min_size") is not None:
        market["_metadata_reward_min_size"] = market_meta.get("reward_min_size")
    if market_meta.get("reward_max_spread") is not None:
        market["_metadata_reward_max_spread"] = market_meta.get("reward_max_spread")
    direction: dict[str, object] = {
        "market": market,
        "reward_active": (
            reward_market.get("reward_active")
            if isinstance(reward_market.get("reward_active"), bool)
            else None
        ),
        "daily_pool_usd": reward_market.get("daily_pool_usd"),
        "reward_checked_at": reward_checked_at,
        "reward_guidance_deadline": reward_guidance_deadline,
        "event_end_confirmation": event_end_confirmation,
    }
    if isinstance(history_summary, Mapping):
        direction["history_summary"] = dict(history_summary)
    if account is not None and _has_market_order(account, market):
        direction["known_participation"] = True
    return direction


def _lp_metadata_stale_conditions(
    directions_by_condition: Mapping[str, Sequence[Mapping[str, object]]],
    condition_ids: Sequence[str],
    now: datetime,
) -> tuple[str, ...]:
    """Batch conditions whose metadata or fee receipt misses the 60s window."""

    stale: list[str] = []
    for condition_id in condition_ids:
        for direction in directions_by_condition.get(condition_id, ()):
            market = (
                direction.get("market") if isinstance(direction, Mapping) else None
            )
            if isinstance(market, Mapping) and (
                _candidate_source_expired(market.get("metadata_checked_at"), now)
                or _candidate_source_expired(market.get("fees_checked_at"), now)
            ):
                stale.append(condition_id)
                break
    return tuple(stale)


def _lp_reward_stale_conditions(
    directions_by_condition: Mapping[str, Sequence[Mapping[str, object]]],
    condition_ids: Sequence[str],
    now: datetime,
) -> tuple[str, ...]:
    """Batch conditions whose reward receipt misses the 60s window."""

    stale: list[str] = []
    for condition_id in condition_ids:
        for direction in directions_by_condition.get(condition_id, ()):
            if isinstance(direction, Mapping) and _candidate_source_expired(
                direction.get("reward_checked_at"), now
            ):
                stale.append(condition_id)
                break
    return tuple(stale)


def _lp_guidance_is_usable(value: object) -> bool:
    """Require every fact the UI needs before counting a direction as passed."""

    if not isinstance(value, Mapping):
        return False
    for field in (
        "condition_id",
        "token_id",
        "outcome",
        "price",
        "quantity",
        "required_capital",
        "estimated_exit_loss",
        "estimated_exit_loss_ratio",
        "checked_at",
        "expires_at",
    ):
        if value.get(field) in (None, ""):
            return False
    for field in (
        "price",
        "quantity",
        "required_capital",
        "estimated_exit_loss",
        "estimated_exit_loss_ratio",
    ):
        parsed = _maybe_decimal(value.get(field))
        if parsed is None or parsed < 0 or field in {"price", "quantity"} and parsed <= 0:
            return False
    try:
        checked = _timestamp(value.get("checked_at"), name="guidance_checked_at")
        expires = _timestamp(value.get("expires_at"), name="guidance_expiry")
    except ValueError:
        return False
    return expires > checked


def _candidate_source_expired(value: object, now: datetime) -> bool:
    """Return whether a candidate source receipt is missing or older than 60s."""

    try:
        checked_at = _timestamp(value, name="candidate_source_checked_at")
    except ValueError:
        return True
    age = Decimal(str((now - checked_at).total_seconds()))
    return age < 0 or age > LP_RECOMMENDATION_REFRESH_SECONDS


def _candidate_source_due(value: object, now: datetime) -> bool:
    """Return whether a source receipt needs a refresh (30s lead or expired)."""

    try:
        checked_at = _timestamp(value, name="candidate_source_checked_at")
    except ValueError:
        return True
    age = Decimal(str((now - checked_at).total_seconds()))
    return (
        age < 0
        or age >= LP_RECOMMENDATION_REFRESH_LEAD_SECONDS
        or age > LP_RECOMMENDATION_REFRESH_SECONDS
    )


def _candidate_head_source_values(cached: Mapping[str, object]) -> list[object]:
    """Collect the head's source receipt stamps, oldest-age candidates only.

    Shared by the maintenance trigger and
    :meth:`PolymarketLPService.candidate_maintenance_wait_seconds` so both
    judge the same values with the same clock.
    """

    raw_account = cached.get("account")
    values: list[object] = [
        raw_account.get("checked_at") if isinstance(raw_account, Mapping) else None
    ]
    for direction in cached.get("directions", ()):
        if not isinstance(direction, Mapping):
            continue
        market = direction.get("market")
        if isinstance(market, Mapping):
            values.extend(
                [
                    market.get("metadata_checked_at"),
                    market.get("fees_checked_at"),
                ]
            )
        values.extend(
            [
                direction.get("reward_checked_at"),
                direction.get("book", {}).get("received_at")
                if isinstance(direction.get("book"), Mapping)
                else None,
            ]
        )
    return values


def _candidate_head_source_times(
    cached: Mapping[str, object], now: datetime
) -> tuple[list[datetime], bool]:
    """Return the parseable source stamps and whether any source is expired."""

    source_times: list[datetime] = []
    for value in _candidate_head_source_values(cached):
        try:
            checked_at = _timestamp(value, name="candidate_source_checked_at")
        except ValueError:
            continue
        if checked_at > now:
            continue
        source_times.append(checked_at)
    any_expired = any(
        _candidate_source_expired(value, now)
        for value in _candidate_head_source_values(cached)
    )
    return source_times, any_expired


def _lp_direction_estimate(
    direction: Mapping[str, object],
    guidance: Mapping[str, object],
    *,
    now: datetime,
) -> dict[str, object]:
    """Run the 5% target-share estimator for one evaluated direction.

    Shared by the batch scan and the 60-second maintenance path so both
    publish the same estimate for the same book.  The inputs mirror
    ``evaluate_lp_entry``: the direction's own book (complementary YES/NO
    mirrors are never summed), its reward rules and pool, and the guidance
    price (the live best bid the trial would quote).
    """

    market = direction.get("market")
    book = direction.get("book")
    if not isinstance(market, Mapping) or not isinstance(book, Mapping):
        return {"state": "unknown", "reason_codes": ["market_facts_unknown"]}
    return estimate_lp_target_share_yield(
        book,
        price=cast(Decimal, guidance.get("price")),
        reward_min_size=cast(Decimal, market.get("reward_min_size")),
        reward_max_spread=cast(Decimal, market.get("reward_max_spread")),
        daily_pool_usd=cast(Decimal, direction.get("daily_pool_usd")),
        now=now,
    )


def _direction_estimate_raw(result: Mapping[str, object]) -> Decimal | None:
    """Return a direction's unrounded estimated yield, if it has one."""

    estimate = result.get("estimate")
    if not isinstance(estimate, Mapping) or estimate.get("state") != "known":
        return None
    return _maybe_decimal(estimate.get("yield_pct_per_hour"))


def _apply_row_estimate_fields(
    row: dict[str, object], estimate: Mapping[str, object] | None
) -> None:
    """Project one estimate onto a published candidate row.

    Unknown estimates stay UNKNOWN on every field — never zero, and never a
    fallback to the whole-pool optimistic upper bound.
    """

    if isinstance(estimate, Mapping):
        row["estimate_state"] = (
            "known" if estimate.get("state") == "known" else "unknown"
        )
        row["estimated_yield_raw"] = estimate.get("yield_pct_per_hour")
        row["estimated_yield_pct_per_hour"] = estimate.get(
            "yield_pct_per_hour_display"
        )
        row["estimated_target_quantity"] = estimate.get("target_quantity")
        row["estimated_target_capital_usd"] = estimate.get("target_capital_usd")
        row["estimated_hourly_reward_usd"] = estimate.get("hourly_reward_usd")
        checked_at = estimate.get("checked_at")
        row["estimate_checked_at"] = (
            checked_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
            if isinstance(checked_at, datetime)
            else None
        )
    else:
        row["estimate_state"] = "unknown"
        row["estimated_yield_raw"] = None
        row["estimated_yield_pct_per_hour"] = None
        row["estimated_target_quantity"] = None
        row["estimated_target_capital_usd"] = None
        row["estimated_hourly_reward_usd"] = None
        row["estimate_checked_at"] = None
    row["estimate_updated"] = True


def _candidate_row_updated_at(row: Mapping[str, object]) -> datetime | None:
    """Parse one pool row's estimate judgment time (issue #157)."""

    value = row.get("updated_at")
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return _timestamp(value, name="candidate_updated_at")
        except ValueError:
            return None
    return None


def _candidate_row_expires_at(row: Mapping[str, object]) -> datetime | None:
    """Parse one pool row's validity deadline (issue #157)."""

    value = row.get("expires_at")
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return _timestamp(value, name="candidate_expires_at")
        except ValueError:
            return None
    return None


def _pool_published_value(value: object) -> object:
    """Normalize one pool row to the published (JSON-like) shape.

    Published candidate rows have always been JSON-round-tripped through
    the durable store, which renders Decimals as strings and datetimes as
    ISO stamps; the rolling pool applies the same normalization at record
    time so in-memory readers and the HTTP projection see one shape.
    """

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Mapping):
        return {str(key): _pool_published_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_pool_published_value(item) for item in value]
    return value


def _candidate_pool_row_expired(row: Mapping[str, object], now: datetime) -> bool:
    """A pool row leaves the valid set once the current clock passes its
    expiry; reads never extend it (issue #157)."""

    expires_at = _candidate_row_expires_at(row)
    return expires_at is None or now >= expires_at


def _candidate_yield_sort_key(
    row: Mapping[str, object],
) -> tuple[int, Decimal, int, Decimal, str]:
    """Published-table order (issue #157): estimated target-share yield
    descending first; UNKNOWN estimates rank after known ones — never as
    zero; the estimate judgment time (newest first) breaks those ties and
    the condition id keeps the order stable.  Competition and actual
    capital left the ranking key: they never reshuffle displayed rows.
    """

    raw_yield = _maybe_decimal(row.get("estimated_yield_raw"))
    yield_key = (0, -raw_yield) if raw_yield is not None else (1, Decimal("0"))
    updated_at = _candidate_row_updated_at(row)
    if updated_at is not None:
        updated_key = (0, -Decimal(str(updated_at.timestamp())))
    else:
        updated_key = (1, Decimal("0"))
    identity = str(row.get("condition_id") or row.get("market_id") or "")
    return (*yield_key, *updated_key, identity)


def _new_candidate_funnel_totals() -> dict[str, object]:
    """Continuous (not per-round) funnel counters for the rolling pool."""

    return {
        "checked": 0,
        "passed": 0,
        "rejected": 0,
        "unknown": 0,
        "batches": 0,
        "backup_read": 0,
        "unchecked": None,
    }


def _lp_funnel_conditions() -> dict[str, object]:
    """Return the user-facing rules applied by every LP funnel batch."""

    return {
        "read": {
            "来源": "奖励目录与市场资料",
            "完整性": "奖励目录全量读取；部分结果可参与筛选，缺失按 UNKNOWN 处理，不阻塞漏斗",
        },
        "base": {
            "奖励": "奖励启用且日奖池>0",
            "市场": "接受订单",
            "参与": "没有已知订单或持仓",
            "摘要": "24h 摘要有效且振幅不超过1¢",
            "事件": "开始前30分钟、进行中、结束后1h冷却不参与",
        },
        "sort": {
            "主排序": "假设每小时收益上限（日奖池÷(24×最低参考占资)）降序；乐观上限仅决定查询顺序，不是预计收益",
            "并列": "官方竞争按密度分档筛选，仅保留轻（[1,30)）、中（[30,300)）两档；重度（≥300）、过薄（<1）、零竞争、无数据整市场排除并计数；库存回退值标「旧」（3 小时窗、无年龄上限）；两档内部并列时按竞争值升序",
            "再排": "参考占资升序；兜底 market_id 升序",
            "备用": "参考价超 1 小时或缺失的进备用队列，按日奖池降序",
        },
        "trial": {
            "展示批": "前 9 个正常候选 + 备用队列补足，不足再以正常队列后续候选补足（最多 10 个）",
            "核验": "每轮仅队首读取实时盘口核验（实时值优先，缺失或无买档保持「待验证」），其余行待验证",
            "超可用": "最低占资超可用资金（实时值优先、缺失用参考值）的候选复核删行、不递补，计入排除数",
            "上限": 10,
            "缺口": "不足 10 个如实展示实际数量与原因；不宣称全市场收益前十",
        },
    }


def expiration_for_review(review_at: datetime, *, now: datetime | None = None) -> int:
    """Bind one GTD expiration to an absolute review deadline.

    The SDK requires the expiration itself to be at least three minutes out;
    the extra minute after review keeps that requirement explicit and prevents
    a restart from rolling the deadline forward.
    """

    current = (now or _now_utc()).astimezone(UTC)
    review = review_at.astimezone(UTC)
    expiration = review + timedelta(seconds=GTD_REVIEW_BUFFER_SECONDS)
    if review <= current or expiration <= current + timedelta(seconds=SDK_MIN_EXPIRATION_SECONDS):
        raise ValueError("review_at_too_soon")
    return int(expiration.timestamp())


class PolymarketLPService:
    """Own one fixed-price opening and its durable exit lifecycle."""

    def __init__(
        self,
        store: PredictionArbitrageStore,
        exchange: object,
        *,
        clock: Callable[[], datetime] = _now_utc,
        owner_lock: object | None = None,
        mutation_guard: Callable[..., bool] | None = None,
    ) -> None:
        self.store = store
        self.exchange = exchange
        self.clock = clock
        self.owner_lock = owner_lock
        self._mutation_guard = mutation_guard
        self._protection_notifier: Callable[..., object] | None = None
        self._mutex = threading.RLock()
        self._reward_refresh_lock = threading.Lock()
        self._price_history_refresh_lock = threading.Lock()
        self._report_lock = threading.Lock()
        # Issue #146: the full scan and the head maintenance run on
        # independent threads; separate locks let a long scan proceed while
        # maintenance refreshes the published head (and vice versa) without
        # either kind overlapping itself.
        self._candidate_scan_lock = threading.Lock()
        self._candidate_maintenance_lock = threading.Lock()
        self._candidate_state_lock = threading.RLock()
        self._competition_lock = threading.Lock()
        self._competition_state: dict[str, object] = {}
        self._preparation_lock = threading.RLock()
        self._sample_target_lock = threading.Lock()
        self._sample_targets: tuple[tuple[str, str], ...] = ()
        self._sample_target_version = 0
        self._candidate_attempted_at: datetime | None = None
        self._candidate_maintenance_failures = 0
        self._candidate_maintenance_last_finished_at: datetime | None = None
        self._candidate_qualification_facts: dict[str, object] = {}
        # Issue #157 rolling pool: valid estimates keyed by condition_id, a
        # per-market rotation schedule, and continuous funnel counters.
        # Publications rewrite pool rows one by one; there is no whole-round
        # snapshot or publication generation anymore.
        self._candidate_pool: dict[str, dict[str, object]] = {}
        self._candidate_rotation: dict[str, dict[str, object]] = {}
        self._candidate_funnel_totals: dict[str, object] = (
            _new_candidate_funnel_totals()
        )
        self._candidate_queue_funnel: dict[str, object] = {}
        self._candidate_queue_state: dict[str, object] | None = None
        # Issue #157 review R6: the current queue's condition ids, kept next
        # to the queue funnel so pending can count untried members directly
        # instead of subtracting a rotation that outlives the queue.
        self._candidate_queue_condition_ids: tuple[str, ...] = ()
        self._candidate_pool_last_saved_at: datetime | None = None
        self._prepared_inputs: dict[str, object] | None = None
        self._prepared_inputs_version = 0
        self._candidate_snapshot: dict[str, object] = {
            "state": "unknown",
            "complete": False,
            "scanning": False,
            "candidates": [],
            "recommendations": [],
            "selected_results": [],
            "checked_at": None,
            "last_success_at": None,
            "last_attempt_at": None,
            "candidate_rows_fresh": False,
            "missing_metadata_condition_ids": [],
            "missing_book_token_ids": [],
            "catalog_complete": False,
            "funnel": {},
            "selected_market_ids": [],
            "candidate_retention_reason": "background_candidates_retired",
            "candidate_valid_count": 0,
            "candidate_pending_count": 0,
            "candidate_failed_recent_count": 0,
            "stale": False,
        }
        self._preparation: dict[str, object] | None = None
        self._restore_preparation()
        self._restore_candidate_snapshot()

    def set_mutation_guard(self, guard: Callable[..., bool] | None) -> None:
        """Attach the existing execution breaker to exchange writes."""

        self._mutation_guard = guard

    def set_protection_notifier(self, callback: Callable[..., object] | None) -> None:
        """Attach the two-channel (feishu + xiaoai) protection notifier."""

        self._protection_notifier = callback

    def _publish_prepared_inputs(
        self,
        catalog: Mapping[str, object],
        metadata: Mapping[str, object],
        *,
        state: str,
    ) -> None:
        raw_markets = catalog.get("markets")
        if (
            isinstance(raw_markets, (list, tuple))
            and not raw_markets
            and not (
                catalog.get("state") == "known"
                and catalog.get("complete") is True
            )
        ):
            return
        # Copy and encode before taking the publication lock.  A large catalog
        # must never make candidate readers wait for the network-sized copy.
        prepared = {
            "catalog": deepcopy(dict(catalog)),
            "metadata": deepcopy(dict(metadata)),
            "state": state,
        }
        with self._candidate_state_lock:
            self._prepared_inputs = prepared
            # Issue #157: prepared inputs changed — the cached exploration
            # queues and direction facts are stale and get rebuilt lazily.
            self._prepared_inputs_version += 1
            self._candidate_queue_state = None

    def _prepared_input_snapshot(self) -> dict[str, object] | None:
        with self._candidate_state_lock:
            prepared = self._prepared_inputs
        return deepcopy(prepared) if isinstance(prepared, Mapping) else None

    def _preparation_priority_order(
        self, condition_ids: Sequence[str]
    ) -> tuple[str, ...]:
        """Order preparation from durable/session exposure to background."""

        ordered = tuple(
            dict.fromkeys(
                str(value).strip() for value in condition_ids if str(value).strip()
            )
        )
        if not ordered:
            return ()
        rank = {condition_id: 2 for condition_id in ordered}
        position_markers = (
            "position",
            "position_size",
            "open_order",
            "open_orders",
            "open_order_count",
            "exposure",
            "in_position",
            "has_open_orders",
        )
        with self._candidate_state_lock:
            snapshot = deepcopy(self._candidate_snapshot)
            facts = deepcopy(self._candidate_qualification_facts)
        active_session_conditions: set[str] = set()
        active_sessions_reader = getattr(self.store, "lp_active_sessions", None)
        if callable(active_sessions_reader):
            try:
                active_sessions = active_sessions_reader()
            except Exception:
                active_sessions = None
            if isinstance(active_sessions, (list, tuple)):
                for active_session in active_sessions:
                    if not isinstance(active_session, Mapping):
                        continue
                    raw_condition = active_session.get("condition_id")
                    if not isinstance(raw_condition, str):
                        payload = active_session.get("payload")
                        if isinstance(payload, Mapping):
                            raw_condition = payload.get("condition_id")
                    if isinstance(raw_condition, str) and raw_condition.strip() in rank:
                        active_session_conditions.add(raw_condition.strip())
        pool_rows: tuple[Mapping[str, object], ...] = tuple()
        with self._candidate_state_lock:
            pool_rows = tuple(
                row
                for row in self._candidate_pool.values()
                if isinstance(row, Mapping)
            )
        for field in ("selected_results", "recommendations", "candidates"):
            rows: object = snapshot.get(field)
            if field == "candidates" and pool_rows:
                # Issue #157: the rolling pool is the live row source; its
                # rows join the persisted selected_results for ranking.
                rows = pool_rows
            if not isinstance(rows, (list, tuple)):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                condition_id = str(row.get("condition_id") or "").strip()
                if condition_id not in rank:
                    continue
                if any(
                    key in row
                    and row.get(key) not in (None, False, 0, "", (), [], {})
                    for key in position_markers
                ):
                    rank[condition_id] = 0
                elif rank[condition_id] > 1:
                    rank[condition_id] = 1
        if isinstance(facts, Mapping):
            for condition_id, fact in facts.items():
                condition = str(condition_id).strip()
                if condition not in rank or not isinstance(fact, Mapping):
                    continue
                account = fact.get("account")
                if not isinstance(account, Mapping):
                    continue
                positions = account.get("positions")
                open_orders = account.get("open_orders")
                if (
                    isinstance(positions, (list, tuple))
                    and positions
                    or isinstance(open_orders, (list, tuple))
                    and open_orders
                ):
                    rank[condition] = 0
        for active_session_condition in active_session_conditions:
            rank[active_session_condition] = 0
        order_index = {condition_id: index for index, condition_id in enumerate(ordered)}
        return tuple(
            sorted(ordered, key=lambda condition_id: (rank[condition_id], order_index[condition_id]))
        )

    @staticmethod
    def _new_preparation_state() -> dict[str, object]:
        return {
            "state": "idle",
            "stage": "history",
            "generation": 1,
            "attempt": 0,
            "failure_count": 0,
            "paused": False,
            "alert_attempted": False,
            "alert_state": None,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_failure_at": None,
            "last_progress_at": None,
            "next_retry_at": None,
            "next_probe_at": None,
            "last_probe_at": None,
            "last_probe_state": None,
            "last_probe_stage": None,
            "probe_states": {},
            "probe_cursor": 0,
            "retry_after_seconds": None,
            "last_error_chain": (),
            "last_error_status": None,
            "last_error_category": None,
            "fault_started_at": None,
            "fault_alert_attempts": 0,
            "fault_alert_state": None,
            "fault_alert_next_at": None,
            "fault_alert_sent_at": None,
            "recovery_alert_state": None,
            "recovery_alert_attempts": 0,
            "recovery_alert_next_at": None,
            "recovery_alert_claimed_at": None,
            "recovery_alert_sent_at": None,
            "completed_count": 0,
            "total_count": 0,
            "metadata_completed_count": 0,
            "metadata_total_count": 0,
            "last_error": None,
        }

    def _restore_preparation(self) -> None:
        reader = getattr(self.store, "lp_preparation", None)
        saved: object = None
        if callable(reader):
            try:
                saved = reader()
            except Exception:
                saved = None
        with self._preparation_lock:
            if isinstance(saved, Mapping):
                state = self._new_preparation_state()
                state.update(deepcopy(dict(saved)))
                self._preparation = state
            else:
                self._preparation = self._new_preparation_state()

    def preparation_snapshot(self) -> dict[str, object]:
        """Return the small durable preparation projection."""

        reader = getattr(self.store, "lp_preparation", None)
        saved: object = None
        if callable(reader):
            try:
                saved = reader()
            except Exception:
                saved = None
        with self._preparation_lock:
            if isinstance(saved, Mapping):
                state = self._new_preparation_state()
                state.update(deepcopy(dict(saved)))
                self._preparation = state
            result = deepcopy(self._preparation or self._new_preparation_state())
        summary_reader = getattr(self.store, "lp_preparation_item_summary", None)
        if callable(summary_reader):
            try:
                summary = summary_reader(limit=5)
            except Exception:
                summary = None
            if isinstance(summary, Mapping):
                result.update(deepcopy(dict(summary)))
        return result

    def _save_preparation(
        self,
        updates: Mapping[str, object],
        *,
        expected_generation: int | None = None,
    ) -> dict[str, object]:
        with self._preparation_lock:
            current = self.preparation_snapshot()
            merged = {**current, **dict(updates)}
            if expected_generation is not None and "generation" not in updates:
                merged["generation"] = expected_generation
            writer = getattr(self.store, "lp_save_preparation", None)
            reader = getattr(self.store, "lp_preparation", None)
            existing = reader() if callable(reader) else None
            store_expected = (
                expected_generation if isinstance(existing, Mapping) else None
            )
            saved = (
                writer(merged, expected_generation=store_expected)
                if callable(writer)
                else merged
            )
            if isinstance(saved, Mapping):
                self._preparation = deepcopy(dict(saved))
                return deepcopy(dict(saved))
            latest = self.preparation_snapshot()
            self._preparation = latest
            return latest

    def _claim_fault_alert_if_due(
        self, state: Mapping[str, object]
    ) -> dict[str, object] | None:
        """Claim one delayed transient-fault notification for this episode."""

        if state.get("fault_alert_state") in {"claimed", "sent"}:
            return None
        started = state.get("fault_started_at")
        try:
            started_at = _timestamp(started, name="fault_started_at")
        except ValueError:
            return None
        try:
            next_at = _timestamp(
                state.get("fault_alert_next_at"), name="fault_alert_next_at"
            )
        except ValueError:
            next_at = started_at + timedelta(seconds=300)
        now = self._now().astimezone(UTC)
        if now < next_at:
            return None
        generation = state.get("generation")
        if type(generation) is not int or generation < 1:
            generation = 1
        attempts = state.get("fault_alert_attempts")
        attempts = attempts if type(attempts) is int and attempts >= 0 else 0
        claimed = self._save_preparation(
            {
                "fault_alert_attempts": attempts + 1,
                "fault_alert_state": "claimed",
                "fault_alert_next_at": None,
            },
            expected_generation=generation,
        )
        claimed["fault_alert_claimed_now"] = True
        return claimed

    def _claim_recovery_alert_if_validated(
        self,
        state: Mapping[str, object],
        *,
        outcome: str,
    ) -> dict[str, object] | None:
        """Claim recovery notification only after an affected full read wins."""

        if outcome != "success" or state.get("state") != "ready":
            return None
        if state.get("fault_alert_state") != "sent":
            return None
        if state.get("recovery_alert_state") in {"claimed", "sent"}:
            return None
        try:
            fault_started = _timestamp(
                state.get("fault_started_at"), name="fault_started_at"
            )
            last_success = _timestamp(
                state.get("last_success_at"), name="last_success_at"
            )
        except ValueError:
            return None
        if last_success < fault_started:
            return None
        if state.get("recovery_alert_state") == "failed":
            try:
                next_at = _timestamp(
                    state.get("recovery_alert_next_at"), name="recovery_alert_next_at"
                )
            except ValueError:
                next_at = None
            if next_at is not None and self._now().astimezone(UTC) < next_at:
                return None
        generation = state.get("generation")
        if type(generation) is not int or generation < 1:
            generation = 1
        attempts = state.get("recovery_alert_attempts")
        attempts = attempts if type(attempts) is int and attempts >= 0 else 0
        claimed = self._save_preparation(
            {
                "recovery_alert_attempts": attempts + 1,
                "recovery_alert_state": "claimed",
                "recovery_alert_next_at": None,
                "recovery_alert_claimed_at": self._now().astimezone(UTC),
            },
            expected_generation=generation,
        )
        claimed["recovery_alert_claimed_now"] = True
        return claimed

    def claim_due_preparation_recovery_alert(self) -> dict[str, object] | None:
        """Claim a due validated recovery notice without reading the exchange."""

        state = self.preparation_snapshot()
        if state.get("recovery_alert_state") != "failed":
            return None
        claimed = self._claim_recovery_alert_if_validated(state, outcome="success")
        if not isinstance(claimed, Mapping):
            return None
        return self._preparation_result(
            claimed,
            outcome="success",
            display_state="ready",
        )

    def _preparation_result(
        self,
        state: Mapping[str, object],
        *,
        outcome: str,
        reason: str | None = None,
        display_state: str | None = None,
        alert_pending: bool = False,
    ) -> dict[str, object]:
        claimed_fault = self._claim_fault_alert_if_due(state)
        if isinstance(claimed_fault, Mapping):
            state = claimed_fault
            alert_pending = True
        claimed_recovery = self._claim_recovery_alert_if_validated(
            state, outcome=outcome
        )
        if isinstance(claimed_recovery, Mapping):
            state = claimed_recovery
        result: dict[str, object] = {
            "state": display_state or str(state.get("state") or "unknown"),
            "preparation_outcome": outcome,
            "preparation": deepcopy(dict(state)),
            "target_count": int(state.get("total_count") or 0),
            "updated_count": 0,
            "unknown_count": 0,
            "request_count": 0,
        }
        summary_reader = getattr(self.store, "lp_preparation_item_summary", None)
        if callable(summary_reader):
            try:
                summary = summary_reader(limit=5)
            except Exception:
                summary = None
            if isinstance(summary, Mapping):
                result["preparation"].update(deepcopy(dict(summary)))
        if reason:
            result["reason"] = reason
        if alert_pending or state.get("fault_alert_claimed_now") is True:
            result["alert_pending"] = True
        if state.get("recovery_alert_claimed_now") is True:
            result["recovery_alert_pending"] = True
        return result

    @staticmethod
    def _safe_error_type(value: object) -> str:
        text = str(value or "unknown_error")
        return text if text.replace("_", "").isalnum() and text[0:1].isalpha() else "unknown_error"

    @classmethod
    def _preparation_error_details(
        cls, value: object
    ) -> tuple[str, bool, tuple[str, ...], int | None, str]:
        """Normalize safe adapter facts for retry policy and operator status."""

        raw_chain: list[object] = []
        status: int | None = None
        if isinstance(value, Mapping):
            raw_chain_value = value.get("error_chain", value.get("error_types"))
            if isinstance(raw_chain_value, Sequence) and not isinstance(
                raw_chain_value, (str, bytes)
            ):
                raw_chain.extend(raw_chain_value)
            raw_chain.append(
                value.get("error_type", value.get("error", value.get("reason")))
            )
            raw_status = value.get("status")
            if type(raw_status) is int and 100 <= raw_status <= 599:
                status = raw_status
            elif isinstance(raw_status, str) and raw_status.isdigit():
                parsed_status = int(raw_status)
                if 100 <= parsed_status <= 599:
                    status = parsed_status
        else:
            raw_chain.append(value)

        chain_values: list[str] = []
        for raw in raw_chain:
            safe = cls._safe_error_type(raw)
            if safe != "unknown_error" and safe not in chain_values:
                chain_values.append(safe)
        chain = tuple(chain_values)
        if not chain:
            chain = ("unknown_error",)
        joined = " ".join(chain).casefold()
        operator_markers = (
            "auth",
            "cert",
            "ssl",
            "credential",
            "config",
            "schema",
            "forbidden",
            "unauthor",
            "requestrejected",
            "invalid",
        )
        operator_blocker = any(marker in joined for marker in operator_markers)
        automatically_recoverable = False
        if status is not None:
            if status in {401, 403} or 400 <= status < 500 and status != 429:
                operator_blocker = True
            elif status == 429 or 500 <= status <= 599:
                automatically_recoverable = True
            else:
                automatically_recoverable = False
        else:
            code = chain[0].casefold()
            automatically_recoverable = code in {
                "timeout",
                "timeouterror",
                "transporterror",
                "connectionerror",
                "connectionreseterror",
                "readerror",
                "incompleteread",
                "proxyerror",
                "network",
                "unavailable",
                "ratelimiterror",
                "http429",
                "status429",
                "http500",
                "http502",
                "http503",
                "http504",
                "history_catalog_incomplete",
            } or code.startswith("market_read_") and any(
                marker in code
                for marker in (
                    "transport",
                    "timeout",
                    "connection",
                    "proxy",
                    "readerror",
                    "ratelimit",
                    "status5",
                    "http5",
                )
            )
        automatically_recoverable = automatically_recoverable and not operator_blocker
        category = (
            "operator_attention"
            if operator_blocker or not automatically_recoverable
            else "transient"
        )
        return chain[0], automatically_recoverable, chain, status, category

    @classmethod
    def _automatic_recovery_error(cls, value: object) -> bool:
        """Return whether a safe read result can be retried automatically."""

        return cls._preparation_error_details(value)[1]

    @staticmethod
    def _retry_after_deadline(
        now: datetime, value: object
    ) -> tuple[datetime | None, int | float | None]:
        """Convert safe adapter pacing facts into a durable absolute deadline."""

        retry_seconds: int | float | None = None
        retry_at: datetime | None = None
        if isinstance(value, Mapping):
            raw_seconds = value.get("retry_after_seconds")
            if isinstance(raw_seconds, (int, float)) and not isinstance(
                raw_seconds, bool
            ) and math.isfinite(float(raw_seconds)) and raw_seconds >= 0:
                retry_seconds = raw_seconds
            if retry_seconds is None and value.get("retry_after_at") is not None:
                try:
                    retry_at = _timestamp(
                        value.get("retry_after_at"), name="retry_after_at"
                    )
                except ValueError:
                    retry_at = None
        if retry_seconds is not None:
            return now + timedelta(seconds=float(retry_seconds)), retry_seconds
        if retry_at is not None and retry_at >= now:
            delta = max(0.0, (retry_at - now).total_seconds())
            return retry_at, int(delta) if delta.is_integer() else delta
        return None, None

    def _run_preparation_probe(
        self,
        now: datetime,
        preparation: Mapping[str, object],
        *,
        stop_event: threading.Event | None = None,
        stage: str | None = None,
        condition_ids: Sequence[str] = (),
    ) -> tuple[dict[str, object], bool]:
        """Run one bounded dependency probe when its durable deadline is due.

        The boolean reports a healthy transition from an unhealthy probe.  A
        transition may wake one full preparation attempt before its normal
        backoff deadline, while repeated healthy probes leave that deadline
        intact so a failing catalog cannot turn into a scan loop.
        """

        try:
            next_probe_at = _timestamp(
                preparation.get("next_probe_at"), name="next_probe_at"
            )
        except ValueError:
            next_probe_at = now
        if now < next_probe_at:
            return deepcopy(dict(preparation)), False

        generation = preparation.get("generation")
        generation = generation if type(generation) is int and generation >= 1 else 1
        scoped_condition_ids = tuple(
            str(value).strip() for value in condition_ids if str(value).strip()
        )
        probe_condition_ids = tuple(
            str(value).strip() for value in condition_ids if str(value).strip()
        )
        next_probe_cursor = preparation.get("probe_cursor")
        if type(next_probe_cursor) is not int or next_probe_cursor < 0:
            next_probe_cursor = 0
        if stage == "history" and probe_condition_ids:
            selected_index = next_probe_cursor % len(probe_condition_ids)
            probe_condition_ids = (probe_condition_ids[selected_index],)
            next_probe_cursor = (selected_index + 1) % len(scoped_condition_ids)
        selected_condition_id = (
            probe_condition_ids[0] if len(probe_condition_ids) == 1 else None
        )
        raw_probe_states = preparation.get("probe_states")
        probe_states: dict[str, dict[str, str]] = {}
        if isinstance(raw_probe_states, Mapping):
            for raw_stage, raw_states in raw_probe_states.items():
                if not isinstance(raw_stage, str) or not isinstance(raw_states, Mapping):
                    continue
                probe_states[raw_stage] = {
                    str(condition_id): str(state)
                    for condition_id, state in raw_states.items()
                    if str(condition_id).strip() and isinstance(state, str)
                }
        probe_stage = str(stage or preparation.get("stage") or "unknown")
        if selected_condition_id is not None:
            previous_probe_state = probe_states.get(probe_stage, {}).get(
                selected_condition_id, ""
            )
        else:
            previous_probe_state = str(preparation.get("last_probe_state") or "")
        reader = getattr(self.exchange, "lp_preparation_probe", None)
        probe: Mapping[str, object]
        if not callable(reader):
            probe = {"state": "unsupported", "complete": False}
        else:
            try:
                try:
                    value = reader(
                        stop_event=stop_event,
                        stage=stage,
                        condition_ids=probe_condition_ids,
                    )
                except TypeError:
                    # Existing exchange doubles and adapters may still expose
                    # the original rewards-only probe signature.
                    value = reader(stop_event=stop_event)
                probe = value if isinstance(value, Mapping) else {}
            except Exception as exc:
                probe = {
                    "state": "unknown",
                    "complete": False,
                    "error_type": type(exc).__name__,
                }

        healthy = probe.get("state") == "healthy" and probe.get("complete") is True
        probe_state = "healthy" if healthy else str(probe.get("state") or "unknown")
        healthy_transition = healthy and previous_probe_state != "healthy"
        retry_after = probe.get("retry_after_seconds")
        retry_after_seconds: int | float | None = None
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
            if retry_after >= 0 and math.isfinite(float(retry_after)):
                retry_after_seconds = retry_after
        if retry_after_seconds is None:
            try:
                retry_at = _timestamp(
                    probe.get("retry_after_at"), name="retry_after_at"
                )
            except ValueError:
                retry_at = None
            if retry_at is not None:
                retry_delta = (retry_at - now).total_seconds()
                if retry_delta >= 0 and math.isfinite(retry_delta):
                    retry_after_seconds = retry_delta
        probe_interval = max(60, int(retry_after_seconds or 0))
        updates: dict[str, object] = {
            "last_probe_at": now,
            "last_probe_state": probe_state,
            "last_probe_stage": stage or preparation.get("stage"),
            "probe_cursor": next_probe_cursor,
            "next_probe_at": now + timedelta(seconds=probe_interval),
            "retry_after_seconds": retry_after_seconds,
        }
        if selected_condition_id is not None:
            stage_states = dict(probe_states.get(probe_stage, {}))
            if scoped_condition_ids:
                allowed_condition_ids = set(scoped_condition_ids)
                stage_states = {
                    condition_id: state
                    for condition_id, state in stage_states.items()
                    if condition_id in allowed_condition_ids
                }
            stage_states[selected_condition_id] = probe_state
            probe_states[probe_stage] = stage_states
            updates["probe_states"] = probe_states
        if retry_after_seconds is not None:
            try:
                retry_at = _timestamp(
                    preparation.get("next_retry_at"), name="next_retry_at"
                )
            except ValueError:
                retry_at = now
            retry_after_at = now + timedelta(seconds=retry_after_seconds)
            if retry_after_at > retry_at:
                # Server pacing applies to full reads as well as cheap probes;
                # retain the existing backoff when it is already later.
                updates["next_retry_at"] = retry_after_at
        if healthy_transition:
            if retry_after_seconds is None:
                updates["next_retry_at"] = now
            updates["state"] = "waiting_retry"
            updates["paused"] = False
            waker = getattr(self.store, "lp_wake_preparation_retries", None)
            if callable(waker) and probe_condition_ids:
                try:
                    waker(
                        condition_ids=probe_condition_ids,
                        stage=stage,
                        now=now,
                        generation=generation,
                    )
                except Exception:
                    # A probe must never turn into a data-path failure merely
                    # because durable wake-up is unavailable.
                    pass
        saved = self._save_preparation(updates, expected_generation=generation)
        return saved, healthy_transition

    @classmethod
    def _safe_selected_reward_reasons(
        cls, value: Mapping[str, object]
    ) -> list[str]:
        """Keep upstream selected-reward provenance without raw transport text."""

        reasons: list[str] = []
        raw_codes = value.get("reason_codes")
        if isinstance(raw_codes, str):
            raw_codes = (raw_codes,)
        if isinstance(raw_codes, Sequence) and not isinstance(raw_codes, (str, bytes)):
            for raw_code in raw_codes:
                code = cls._safe_error_type(raw_code)
                if code != "unknown_error" and code not in reasons:
                    reasons.append(code)
        error_type = value.get("error_type")
        if error_type is not None:
            safe_error = cls._safe_error_type(error_type)
            if safe_error != "unknown_error":
                code = f"reward_error_{safe_error}"
                if code not in reasons:
                    reasons.append(code)
        status = value.get("status")
        if type(status) is int and 100 <= status <= 599:
            code = f"reward_http_status_{status}"
            if code not in reasons:
                reasons.append(code)
        elif isinstance(status, str):
            safe_status = cls._safe_error_type(status)
            if safe_status != "unknown_error":
                code = f"reward_status_{safe_status}"
                if code not in reasons:
                    reasons.append(code)
        return reasons

    def _begin_preparation(self, now: datetime) -> dict[str, object]:
        current = self.preparation_snapshot()
        generation = current.get("generation")
        generation = generation if type(generation) is int and generation >= 1 else 1
        attempt = current.get("attempt")
        attempt = attempt if type(attempt) is int and attempt >= 0 else 0
        failures = current.get("failure_count")
        failures = failures if type(failures) is int and failures >= 0 else 0
        return self._save_preparation(
            {
                "state": "preparing",
                "stage": "catalog",
                "attempt": attempt + 1,
                "last_attempt_at": now,
                "next_retry_at": None,
                "next_probe_at": None,
                "last_error": None,
                "alert_attempted": False,
                "alert_state": None,
            },
            expected_generation=generation,
        )

    def _preparation_failure(
        self,
        now: datetime,
        *,
        stage: str,
        error_type: object,
    ) -> dict[str, object]:
        current = self.preparation_snapshot()
        generation = current.get("generation")
        generation = generation if type(generation) is int and generation >= 1 else 1
        failures = current.get("failure_count")
        failures = failures if type(failures) is int and failures >= 0 else 0
        failures += 1
        safe_error, recoverable, error_chain, error_status, error_category = (
            self._preparation_error_details(error_type)
        )
        paused = not recoverable
        delay_index = min(
            max(failures - 1, 0), len(_LP_PREPARATION_RETRY_DELAYS_SECONDS) - 1
        )
        retry_at = (
            None
            if paused
            else now
            + timedelta(seconds=_LP_PREPARATION_RETRY_DELAYS_SECONDS[delay_index])
        )
        probe_at = (
            None if paused else now + timedelta(seconds=60)
        )
        retry_after_at, retry_after_seconds = self._retry_after_deadline(
            now, error_type
        )
        if not paused and retry_after_at is not None:
            retry_at = max(retry_at, retry_after_at)  # type: ignore[arg-type]
            probe_at = max(probe_at, retry_after_at)  # type: ignore[arg-type]
        fault_started_at: object = current.get("fault_started_at")
        fault_alert_state = current.get("fault_alert_state")
        try:
            prior_fault_started = _timestamp(
                fault_started_at, name="fault_started_at"
            )
        except ValueError:
            prior_fault_started = None
        try:
            prior_success = _timestamp(
                current.get("last_success_at"), name="last_success_at"
            )
        except ValueError:
            prior_success = None
        if prior_fault_started is None or (
            prior_success is not None and prior_success >= prior_fault_started
        ):
            fault_started_at = now
            fault_alert_state = None
            fault_alert_attempts = 0
            fault_alert_next_at: object = now + timedelta(seconds=300)
            fault_alert_sent_at: object = None
            recovery_alert_state: object = None
            recovery_alert_claimed_at: object = None
            recovery_alert_sent_at: object = None
        else:
            fault_alert_attempts = current.get("fault_alert_attempts")
            if type(fault_alert_attempts) is not int or fault_alert_attempts < 0:
                fault_alert_attempts = 0
            fault_alert_next_at = current.get("fault_alert_next_at")
            fault_alert_sent_at = current.get("fault_alert_sent_at")
            recovery_alert_state = current.get("recovery_alert_state")
            recovery_alert_claimed_at = current.get("recovery_alert_claimed_at")
            recovery_alert_sent_at = current.get("recovery_alert_sent_at")
        state = self._save_preparation(
            {
                "state": "paused" if paused else "waiting_retry",
                "stage": stage,
                "failure_count": failures,
                "paused": paused,
                "last_failure_at": now,
                "last_error": safe_error,
                "last_error_chain": error_chain,
                "last_error_status": error_status,
                "last_error_category": error_category,
                "next_retry_at": retry_at,
                "next_probe_at": probe_at,
                "retry_after_seconds": retry_after_seconds,
                "fault_started_at": fault_started_at,
                "fault_alert_attempts": fault_alert_attempts,
                "fault_alert_state": fault_alert_state,
                "fault_alert_next_at": fault_alert_next_at,
                "fault_alert_sent_at": fault_alert_sent_at,
                "recovery_alert_state": recovery_alert_state,
                "recovery_alert_claimed_at": recovery_alert_claimed_at,
                "recovery_alert_sent_at": recovery_alert_sent_at,
                "alert_attempted": False if not paused else current.get("alert_attempted") is True,
                "alert_state": None if not paused else current.get("alert_state"),
            },
            expected_generation=generation,
        )
        if paused and state.get("alert_attempted") is not True:
            claimer = getattr(self.store, "lp_claim_preparation_alert", None)
            claimed = claimer(expected_generation=generation) if callable(claimer) else state
            if isinstance(claimed, Mapping):
                with self._preparation_lock:
                    self._preparation = deepcopy(dict(claimed))
                state = deepcopy(dict(claimed))
        return state

    def _ensure_fault_episode(self, now: datetime) -> dict[str, object]:
        """Start a coalesced fault episode for persistent market failures."""

        current = self.preparation_snapshot()
        try:
            started = _timestamp(
                current.get("fault_started_at"), name="fault_started_at"
            )
        except ValueError:
            started = None
        try:
            succeeded = _timestamp(
                current.get("last_success_at"), name="last_success_at"
            )
        except ValueError:
            succeeded = None
        if started is not None and (succeeded is None or succeeded < started):
            return {}
        return {
            "fault_started_at": now,
            "fault_alert_attempts": 0,
            "fault_alert_state": None,
            "fault_alert_next_at": now + timedelta(seconds=300),
            "fault_alert_sent_at": None,
            "recovery_alert_state": None,
            "recovery_alert_attempts": 0,
            "recovery_alert_next_at": None,
            "recovery_alert_claimed_at": None,
            "recovery_alert_sent_at": None,
        }

    def _clear_unacknowledged_fault_episode(self) -> dict[str, object]:
        """Drop a transient fault that recovered before its notice was sent."""

        current = self.preparation_snapshot()
        if current.get("fault_alert_state") in {"sent", "claimed", "failed"}:
            return {}
        return {
            "fault_started_at": None,
            "fault_alert_attempts": 0,
            "fault_alert_state": None,
            "fault_alert_next_at": None,
            "fault_alert_sent_at": None,
            "recovery_alert_state": None,
            "recovery_alert_attempts": 0,
            "recovery_alert_next_at": None,
            "recovery_alert_claimed_at": None,
            "recovery_alert_sent_at": None,
        }

    def recover_preparation(self) -> dict[str, object]:
        """Explicitly re-arm a paused preparation cycle."""

        current = self.preparation_snapshot()
        item_recoverer = getattr(self.store, "lp_recover_preparation_items", None)
        recovered_items = item_recoverer() if callable(item_recoverer) else ()
        recovered_condition_ids = [
            str(item.get("condition_id"))
            for item in recovered_items
            if isinstance(item, Mapping)
            and str(item.get("condition_id") or "").strip()
        ]
        if current.get("paused") is not True and not recovered_condition_ids:
            return {**current, "recovered_condition_ids": []}
        generation = current.get("generation")
        generation = generation if type(generation) is int and generation >= 1 else 1
        recovered_state = (
            "partial"
            if recovered_condition_ids and current.get("state") != "paused"
            else "ready"
        )
        state = self._save_preparation(
            {
                "state": recovered_state,
                "stage": "catalog",
                "generation": generation + 1,
                "attempt": 0,
                "failure_count": 0,
                "paused": False,
                "alert_attempted": False,
                "alert_state": None,
                "last_error": None,
                "next_retry_at": None,
                "next_probe_at": None,
                "last_probe_at": None,
                "last_probe_state": None,
                "retry_after_seconds": None,
            },
            expected_generation=generation,
        )
        state["recovered_condition_ids"] = recovered_condition_ids
        return state

    def _claim_preparation_item_alerts(self) -> dict[str, object] | None:
        claimer = getattr(self.store, "lp_claim_preparation_item_alerts", None)
        if not callable(claimer):
            return None
        try:
            claimed = claimer(limit=5)
        except Exception:
            return None
        return dict(claimed) if isinstance(claimed, Mapping) else None

    def finish_preparation_alert(self, *, generation: int, success: bool) -> dict[str, object] | None:
        """Record the result of the already-claimed operator notification."""

        current = self.preparation_snapshot()
        if current.get("fault_alert_state") == "claimed":
            attempts = current.get("fault_alert_attempts")
            attempts = attempts if type(attempts) is int and attempts >= 1 else 1
            if success:
                updates = {
                    "fault_alert_state": "sent",
                    "fault_alert_next_at": None,
                    "fault_alert_sent_at": self._now().astimezone(UTC),
                    # Wake one affected full read so a validated recovery
                    # notice can follow this incident delivery.
                    "next_probe_at": self._now().astimezone(UTC)
                    + timedelta(seconds=60),
                }
            else:
                delay = _LP_PREPARATION_RETRY_DELAYS_SECONDS[
                    min(attempts - 1, len(_LP_PREPARATION_RETRY_DELAYS_SECONDS) - 1)
                ]
                updates = {
                    "fault_alert_state": "failed",
                    "fault_alert_next_at": self._now().astimezone(UTC)
                    + timedelta(seconds=delay),
                }
            saved_fault = self._save_preparation(
                updates, expected_generation=generation
            )
            if isinstance(saved_fault, Mapping):
                return dict(saved_fault)

        item_finisher = getattr(self.store, "lp_finish_preparation_item_alerts", None)
        if callable(item_finisher):
            try:
                item_finisher(success=success)
            except Exception:
                pass
        writer = getattr(self.store, "lp_finish_preparation_alert", None)
        if not callable(writer):
            return None
        saved = writer(generation=generation, success=success)
        if isinstance(saved, Mapping):
            with self._preparation_lock:
                self._preparation = deepcopy(dict(saved))
            return dict(saved)
        return None

    def finish_preparation_recovery(
        self, *, generation: int, success: bool
    ) -> dict[str, object] | None:
        """Record delivery of one validated recovery notification."""

        current = self.preparation_snapshot()
        if current.get("recovery_alert_state") != "claimed":
            return current
        attempts = current.get("recovery_alert_attempts")
        attempts = attempts if type(attempts) is int and attempts >= 1 else 1
        if success:
            next_at: object = None
        else:
            delay = _LP_PREPARATION_RETRY_DELAYS_SECONDS[
                min(attempts - 1, len(_LP_PREPARATION_RETRY_DELAYS_SECONDS) - 1)
            ]
            next_at = self._now().astimezone(UTC) + timedelta(seconds=delay)
        saved = self._save_preparation(
            {
                "recovery_alert_state": "sent" if success else "failed",
                "recovery_alert_next_at": next_at,
                "recovery_alert_sent_at": self._now().astimezone(UTC)
                if success
                else current.get("recovery_alert_sent_at"),
            },
            expected_generation=generation,
        )
        return saved if isinstance(saved, Mapping) else None

    def candidate_snapshot(self) -> dict[str, object]:
        """Project the rolling candidate pool without external reads.

        Issue #157: every valid pool row is one successful estimate with a
        five-minute validity computed against the current clock; rows are
        never degraded or cleared because a whole snapshot aged.  The top
        ten by the yield ranking key are published and the head is always
        the current recommendation — when it leaves the pool the next row
        takes its place automatically.
        """

        with self._candidate_state_lock:
            pool = deepcopy(self._candidate_pool)
            snapshot = deepcopy(self._candidate_snapshot)
            funnel = deepcopy(self._candidate_funnel_totals)
            queue_funnel = deepcopy(self._candidate_queue_funnel)
            queue_condition_ids = self._candidate_queue_condition_ids
            rotation = deepcopy(self._candidate_rotation)
            competition_state = deepcopy(self._competition_state)
        from .polymarket_lp_views import LP_TRIAL_CANDIDATE_LIMIT

        now = self._now()
        valid_rows = [
            row
            for row in pool.values()
            if isinstance(row, Mapping) and not _candidate_pool_row_expired(row, now)
        ]
        valid_rows.sort(key=_candidate_yield_sort_key)
        published = [
            deepcopy(dict(row)) for row in valid_rows[:LP_TRIAL_CANDIDATE_LIMIT]
        ]
        valid_count = len(valid_rows)
        failed_count = sum(
            1 for row in valid_rows if row.get("refresh_failed") is True
        )
        # Issue #157 review R6: pending counts the current queue members
        # without a rotation attempt stamp directly.  The rotation outlives
        # the queue (markets leave, restarts restore it), so subtracting its
        # size from the queue total could underreport — even to zero — while
        # untried members remain.
        pending = 0
        for condition_id in queue_condition_ids:
            entry = rotation.get(condition_id)
            if (
                not isinstance(entry, Mapping)
                or entry.get("last_attempt_at") is None
            ):
                pending += 1
        # Scheduler hint for the exploration loop: zero while any queue
        # market is still untried or any tried market is out of backoff,
        # otherwise the seconds until the earliest backoff expiry.
        next_batch_wait = 0.0
        if pending <= 0:
            waits: list[float] = []
            for entry in rotation.values():
                if not isinstance(entry, Mapping):
                    continue
                failures = entry.get("failures")
                if not isinstance(failures, int) or failures <= 0:
                    waits = []
                    break
                last_attempt_at = _candidate_row_updated_at(
                    {"updated_at": entry.get("last_attempt_at")}
                )
                if last_attempt_at is None:
                    waits = []
                    break
                backoff = _CANDIDATE_MAINTENANCE_BACKOFF_SECONDS[
                    min(
                        failures - 1,
                        len(_CANDIDATE_MAINTENANCE_BACKOFF_SECONDS) - 1,
                    )
                ]
                remaining = (
                    last_attempt_at
                    + timedelta(seconds=float(backoff))
                    - now
                ).total_seconds()
                waits.append(max(0.0, remaining))
            if waits:
                next_batch_wait = min(waits)
        base_state = str(snapshot.get("state") or "")
        if snapshot.get("scanning") is True:
            snapshot_state = "scanning"
        elif base_state in {"ready", "incomplete"}:
            snapshot_state = base_state
        elif published or snapshot.get("last_success_at"):
            # A healthy rolling pool that simply has no valid rows right
            # now stays ready with an honestly empty table.
            snapshot_state = "ready"
        else:
            snapshot_state = "unknown"
        last_success_at = snapshot.get("last_success_at")
        funnel_view = {
            # Structural defaults first, so a not-yet-built queue funnel
            # still publishes the shape Hermes and the page expect.
            "reasons": {"read": [], "base": [], "sort": [], "trial": []},
            "budget": {"available_capital": None},
            "excluded": {"competition_empty": 0, "over_available": 0},
            **queue_funnel,
            **funnel,
            "trial": valid_count,
            "unchecked": pending,
            "stop_reason": snapshot.get("stop_reason"),
            "gap_reason": None,
            "conditions": _lp_funnel_conditions(),
            "competition_state": competition_state.get("state") or "unknown",
            "competition_not_updated": list(
                competition_state.get("not_updated") or ()
            ),
        }
        projection = {
            "state": snapshot_state,
            "complete": snapshot.get("complete") is True,
            "scanning": snapshot.get("scanning") is True,
            "candidates": published,
            "recommendations": [deepcopy(published[0])] if published else [],
            "selected_results": deepcopy(published),
            # Published stamps keep the durable (ISO string) shape that the
            # whole-snapshot contract always had via its store round trip.
            "checked_at": _pool_published_value(snapshot.get("checked_at")),
            "last_success_at": _pool_published_value(last_success_at),
            "last_attempt_at": _pool_published_value(
                snapshot.get("last_attempt_at")
            ),
            "candidate_rows_fresh": bool(published),
            "missing_metadata_condition_ids": snapshot.get(
                "missing_metadata_condition_ids", []
            ),
            "missing_book_token_ids": snapshot.get("missing_book_token_ids", []),
            "catalog_complete": snapshot.get("catalog_complete") is True,
            "event_end_confirmations": snapshot.get("event_end_confirmations", {}),
            "retention_reason": snapshot.get("retention_reason"),
            "funnel": funnel_view,
            "selected_market_ids": [
                str(row.get("market_id") or "") for row in published
            ],
            "candidate_retention_reason": "background_candidates_retired",
            "candidate_valid_count": valid_count,
            "candidate_pending_count": pending,
            "candidate_failed_recent_count": failed_count,
            "next_batch_wait_seconds": next_batch_wait,
            "stale": False,
            "maintenance_consecutive_failures": snapshot.get(
                "maintenance_consecutive_failures", 0
            ),
            "maintenance_next_attempt_at": snapshot.get(
                "maintenance_next_attempt_at"
            ),
            "maintenance_diagnostics": snapshot.get(
                "maintenance_diagnostics"
            ),
        }
        # Keep preparation lifecycle state adjacent to the cached candidate
        # projection.  It is a small durable row, so readers can show a
        # pending/retry/paused reason without re-running the external funnel.
        projection["preparation"] = self.preparation_snapshot()
        return projection

    def candidate_maintenance_wait_seconds(self) -> float | None:
        """Return seconds until maintenance may run again (issue #146).

        Inside a failure backoff window this is the remaining backoff;
        otherwise it is the time until the oldest head source reaches the
        30-second lead. Every numeric result clamps to [1.0, 300.0] so the
        scheduler can never spin; ``None`` means there is nothing to
        maintain (no head, no facts, or no source stamps).
        """

        with self._candidate_state_lock:
            qualification_facts = deepcopy(self._candidate_qualification_facts)
            failures = self._candidate_maintenance_failures
            last_finished_at = self._candidate_maintenance_last_finished_at
            pool = deepcopy(self._candidate_pool)
        now = self._now()
        floor = _CANDIDATE_SCHEDULER_WAIT_FLOOR_SECONDS
        ceiling = _CANDIDATE_SCHEDULER_WAIT_CEILING_SECONDS

        if failures > 0 and last_finished_at is not None:
            backoff = _CANDIDATE_MAINTENANCE_BACKOFF_SECONDS[
                min(failures - 1, 2)
            ]
            remaining = (
                last_finished_at + timedelta(seconds=float(backoff)) - now
            ).total_seconds()
            if remaining > 0:
                return min(max(remaining, floor), ceiling)
        # Issue #157: the maintained rows are the valid pool rows (the
        # currently displayed top ten), not a stored whole-snapshot table.
        valid_rows = [
            row
            for row in pool.values()
            if isinstance(row, Mapping) and not _candidate_pool_row_expired(row, now)
        ]
        selected_rows = sorted(
            valid_rows, key=_candidate_yield_sort_key
        )[:_LP_CANDIDATE_BATCH_SIZE]
        if not selected_rows:
            return None
        # Issue #138 round 2: maintenance refreshes the whole published
        # table, so the wait is governed by the oldest source receipt
        # across every published row's facts.
        source_times: list[datetime] = []
        for row in selected_rows:
            condition_id = str(row.get("condition_id") or "").strip()
            cached = qualification_facts.get(condition_id)
            if not isinstance(cached, Mapping):
                continue
            for value in _candidate_head_source_values(cached):
                try:
                    checked_at = _timestamp(value, name="candidate_source_checked_at")
                except ValueError:
                    # An unparseable stamp is degenerate: retry on the floor.
                    return floor
                if checked_at > now:
                    # A future stamp is degenerate: retry on the floor.
                    return floor
                source_times.append(checked_at)
        if not source_times:
            return None
        wait_seconds = (
            min(source_times)
            + timedelta(seconds=float(LP_RECOMMENDATION_REFRESH_LEAD_SECONDS))
            - now
        ).total_seconds()
        return min(max(wait_seconds, floor), ceiling)

    def _publish_sample_targets(
        self, targets: Sequence[tuple[str, str]]
    ) -> None:
        unique = tuple(dict.fromkeys(targets))
        with self._sample_target_lock:
            if unique != self._sample_targets:
                self._sample_targets = unique
                self._sample_target_version += 1

    def sample_candidate_books(
        self, *, stop_event: threading.Event | None = None
    ) -> dict[str, object]:
        """Persist one receipt-stamped BBO sample for every observed reward token."""

        with self._sample_target_lock:
            targets = self._sample_targets
            version = self._sample_target_version
        if stop_event is not None and stop_event.is_set():
            return {"state": "cancelled", "sampled_count": 0}
        if not targets:
            return {"state": "unknown", "sampled_count": 0}

        reader = getattr(self.exchange, "lp_order_books", None)
        if not callable(reader):
            return {"state": "unknown", "sampled_count": 0}
        token_ids = tuple(dict.fromkeys(token_id for _, token_id in targets))
        batches = tuple(
            token_ids[offset : offset + _LP_BOOK_SAMPLE_BATCH_SIZE]
            for offset in range(0, len(token_ids), _LP_BOOK_SAMPLE_BATCH_SIZE)
        )
        books: dict[str, object] = {}
        if len(batches) == 1:
            try:
                result = reader(batches[0], stop_event=stop_event)
            except Exception:
                return {"state": "unknown", "sampled_count": 0}
            if isinstance(result, Mapping):
                books.update(
                    (token, book)
                    for token, book in result.items()
                    if isinstance(token, str)
                )
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(
                max_workers=min(_LP_BOOK_SAMPLE_MAX_CONCURRENCY, len(batches)),
                thread_name_prefix="prediction-lp-book-sampler",
            ) as executor:
                futures = {
                    executor.submit(reader, batch, stop_event=stop_event): batch
                    for batch in batches
                }
                for future in as_completed(futures):
                    try:
                        result = future.result()
                    except Exception:
                        continue
                    if isinstance(result, Mapping):
                        books.update(
                            (token, book)
                            for token, book in result.items()
                            if isinstance(token, str)
                        )
        if stop_event is not None and stop_event.is_set():
            return {"state": "cancelled", "sampled_count": 0}

        now = self._now()
        samples: list[dict[str, object]] = []
        for condition_id, token_id in targets:
            book = books.get(token_id)
            if not isinstance(book, Mapping) or book.get("token_id") != token_id:
                continue
            observed_condition = book.get("condition_id")
            if observed_condition not in (None, "", condition_id):
                continue
            try:
                received_at = _timestamp(
                    book.get("received_at"), name="book_received_at"
                )
                bids = _levels(book.get("bids"), "bids")
                asks = _levels(book.get("asks"), "asks")
            except ValueError:
                continue
            if received_at > now or not bids or not asks:
                continue
            bid_price, bid_size = max(bids, key=lambda level: level[0])
            ask_price, ask_size = min(asks, key=lambda level: level[0])
            if bid_price >= ask_price or ask_price > Decimal("1"):
                continue
            samples.append(
                {
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "received_at": received_at,
                    "source_timestamp": book.get("source_timestamp"),
                    "best_bid_price": bid_price,
                    "best_bid_size": bid_size,
                    "best_ask_price": ask_price,
                    "best_ask_size": ask_size,
                }
            )

        with self._sample_target_lock:
            if version != self._sample_target_version or targets != self._sample_targets:
                return {"state": "superseded", "sampled_count": 0}
        if stop_event is not None and stop_event.is_set():
            return {"state": "cancelled", "sampled_count": 0}
        recorder = getattr(self.store, "lp_record_book_samples", None)
        if not callable(recorder):
            return {"state": "unknown", "sampled_count": 0}
        try:
            recorded = recorder(samples, now=now)
        except Exception:
            return {"state": "unknown", "sampled_count": 0}
        return {
            "state": "recorded" if recorded else "unknown",
            "sampled_count": recorded,
            "target_count": len(targets),
        }

    def refresh_price_history(
        self,
        *,
        stop_event: threading.Event | None = None,
        manual_recovery: bool = False,
    ) -> dict[str, object]:
        """Refresh bounded 24-hour price summaries for the light-screen range.

        This hourly path prepares history only. Candidate refreshes consume the
        stored summaries and never call the history endpoint themselves.
        Network work is issued in batches of twenty with at most four active
        requests, and each completed batch is persisted before the next group
        is allowed to retain its response.
        """

        if type(manual_recovery) is not bool:
            raise ValueError("manual_recovery must be a boolean")
        if not self._price_history_refresh_lock.acquire(blocking=False):
            return self._preparation_result(
                self.preparation_snapshot(), outcome="busy", display_state="busy"
            )
        preparation_owner_acquired = False
        try:
            owner_acquirer = getattr(
                self.store, "lp_try_acquire_preparation_owner", None
            )
            owner_releaser = getattr(
                self.store, "lp_release_preparation_owner", None
            )
            if callable(owner_acquirer):
                preparation_owner_acquired = bool(owner_acquirer())
                if not preparation_owner_acquired:
                    return self._preparation_result(
                        self.preparation_snapshot(),
                        outcome="busy",
                        display_state="busy",
                    )
            interrupted_normalizer = getattr(
                self.store, "lp_normalize_interrupted_preparation_items", None
            )
            if callable(interrupted_normalizer):
                try:
                    interrupted_normalizer()
                except Exception:
                    pass
            now = self._now().astimezone(UTC)
            preparation = self.preparation_snapshot()
            legacy_migrator = getattr(
                self.store, "lp_migrate_legacy_preparation", None
            )
            if callable(legacy_migrator):
                try:
                    migrated = legacy_migrator()
                except Exception:
                    migrated = None
                if isinstance(migrated, Mapping):
                    restored = self._new_preparation_state()
                    restored.update(deepcopy(dict(migrated)))
                    with self._preparation_lock:
                        self._preparation = restored
                    preparation = self.preparation_snapshot()
            preparation_item_reader = getattr(self.store, "lp_preparation_items", None)
            existing_preparation_items = (
                preparation_item_reader()
                if callable(preparation_item_reader)
                else ()
            )
            has_partial_items = any(
                isinstance(item, Mapping)
                and str(item.get("condition_id") or "").strip()
                for item in existing_preparation_items
            )
            if manual_recovery:
                if preparation.get("paused") is not True:
                    return self._preparation_result(
                        preparation, outcome="ignored", display_state="unknown"
                    )
                preparation = self.recover_preparation()
            elif preparation.get("paused") is True:
                return self._preparation_result(
                    preparation,
                    outcome="paused",
                    reason="preparation_paused",
                    display_state="unknown",
                )
            elif has_partial_items and preparation.get("next_retry_at") is not None:
                waiting_items = [
                    item
                    for item in existing_preparation_items
                    if isinstance(item, Mapping)
                    and str(item.get("condition_id") or "").strip()
                    and item.get("state") == "waiting_retry"
                    and item.get("paused") is not True
                ]
                retry_times: list[datetime] = []
                for item in waiting_items:
                    try:
                        retry_at = _timestamp(
                            item.get("next_retry_at"), name="next_retry_at"
                        )
                    except ValueError:
                        continue
                    retry_times.append(retry_at)
                retry_at = min(retry_times) if retry_times else now
                if waiting_items and now < retry_at:
                    probe_stage = str(
                        waiting_items[0].get("stage")
                        or preparation.get("last_probe_stage")
                        or preparation.get("stage")
                        or "history"
                    )
                    probe_ids = tuple(
                        str(item.get("condition_id"))
                        for item in waiting_items
                        if str(item.get("condition_id") or "").strip()
                    )
                    preparation, probe_recovered = self._run_preparation_probe(
                        now,
                        preparation,
                        stop_event=stop_event,
                        stage=probe_stage,
                        condition_ids=probe_ids,
                    )
                    if not probe_recovered:
                        return self._preparation_result(
                            preparation,
                            outcome="waiting_retry",
                            reason="retry_not_due",
                            display_state="partial",
                        )
            elif (
                not has_partial_items
                and (
                    preparation.get("state") == "waiting_retry"
                    or preparation.get("next_retry_at") is not None
                )
            ):
                try:
                    retry_at = _timestamp(
                        preparation.get("next_retry_at"), name="next_retry_at"
                    )
                except ValueError:
                    retry_at = now
                if now < retry_at:
                    preparation, probe_recovered = self._run_preparation_probe(
                        now, preparation, stop_event=stop_event
                    )
                    if probe_recovered:
                        try:
                            retry_at = _timestamp(
                                preparation.get("next_retry_at"),
                                name="next_retry_at",
                            )
                        except ValueError:
                            retry_at = now
                    else:
                        return self._preparation_result(
                            preparation,
                            outcome="waiting_retry",
                            reason="retry_not_due",
                            display_state="unknown",
                        )
                else:
                    preparation, _ = self._run_preparation_probe(
                        now, preparation, stop_event=stop_event
                    )
                if now < retry_at:
                    return self._preparation_result(
                        preparation,
                        outcome="waiting_retry",
                        reason="retry_not_due",
                        display_state="unknown",
                    )
            preparation = self._begin_preparation(now)
            if preparation.get("state") == "paused":
                return self._preparation_result(
                preparation,
                outcome="paused",
                reason="preparation_attempt_budget_exhausted",
                display_state="unknown",
                alert_pending=preparation.get("alert_claimed_now") is True,
            )
            generation = preparation.get("generation")
            generation = generation if type(generation) is int and generation >= 1 else 1
            preparation_retry_claimer = getattr(
                self.store, "lp_claim_preparation_retries", None
            )
            preparation_failure_writer = getattr(
                self.store, "lp_record_preparation_failure", None
            )
            preparation_clearer = getattr(self.store, "lp_clear_preparation_items", None)
            claimed_condition_ids: set[str] = set()
            preparation_items: dict[str, Mapping[str, object]] = {}
            if callable(preparation_item_reader):
                preparation_items = {
                    str(item.get("condition_id")): item
                    for item in preparation_item_reader()
                    if isinstance(item, Mapping)
                    and str(item.get("condition_id") or "").strip()
                }
            window_end = now.replace(second=0, microsecond=0)
            window_start = window_end - _LP_PRICE_HISTORY_WINDOW
            end_ts = int(window_end.timestamp())
            catalog_reader = getattr(self.exchange, "lp_reward_catalog", None)
            metadata_reader = getattr(self.exchange, "lp_market_metadata", None)
            history_reader = getattr(self.exchange, "lp_price_history", None)
            if not callable(catalog_reader):
                failed = self._preparation_failure(
                    self._now(), stage="catalog", error_type="history_readers_unavailable"
                )
                return self._preparation_result(
                    failed,
                    outcome="failure",
                    reason="history_readers_unavailable",
                    display_state="unknown",
                    alert_pending=failed.get("alert_claimed_now") is True,
                )
            if stop_event is not None and stop_event.is_set():
                return self._preparation_result(
                    preparation, outcome="cancelled", display_state="cancelled"
                )

            catalog_failure: tuple[str, str] | None = None
            catalog_failure_error: object | None = None
            try:
                catalog = catalog_reader(stop_event=stop_event)
                if stop_event is not None and stop_event.is_set():
                    return self._preparation_result(
                        preparation, outcome="cancelled", display_state="cancelled"
                    )
                if not isinstance(catalog, Mapping):
                    catalog_failure_error = "history_catalog_unknown"
                    raise ValueError("history_catalog_unknown")
                safe_catalog_error = self._safe_error_type(catalog.get("error_type"))
                if safe_catalog_error == "unknown_error":
                    safe_catalog_error = "history_catalog_unknown"
                catalog_error_details: dict[str, object] = {
                    "error_type": safe_catalog_error
                }
                for field in (
                    "error_chain",
                    "error_types",
                    "status",
                    "retry_after_seconds",
                    "retry_after_at",
                ):
                    value = catalog.get(field)
                    if value is not None:
                        catalog_error_details[field] = value
                raw_markets = catalog.get("markets")
                if not isinstance(raw_markets, (list, tuple)):
                    catalog_failure_error = catalog_error_details
                    raise ValueError("history_catalog_unknown")
                if catalog.get("state") != "known":
                    catalog_failure_error = catalog_error_details
                    raise ValueError("history_catalog_unknown")
                market_rows = [row for row in raw_markets if isinstance(row, Mapping)]
                if catalog.get("complete") is not True:
                    if not market_rows:
                        catalog_failure_error = catalog_error_details
                        raise ValueError("history_catalog_unknown")
                    catalog_failure = (
                        "catalog",
                        safe_catalog_error
                        if safe_catalog_error != "history_catalog_unknown"
                        else "history_catalog_incomplete",
                    )
                condition_ids = tuple(
                    dict.fromkeys(
                        str(row.get("condition_id") or "").strip()
                        for row in market_rows
                        if str(row.get("condition_id") or "").strip()
                    )
                )
                priority_condition_ids = self._preparation_priority_order(condition_ids)
                priority_index = {
                    condition_id: index
                    for index, condition_id in enumerate(priority_condition_ids)
                }
                metadata_condition_ids: list[str] = []
                for condition_id in priority_condition_ids:
                    item = preparation_items.get(condition_id)
                    if item is None:
                        metadata_condition_ids.append(condition_id)
                        continue
                    item_state = str(item.get("state") or "")
                    if bool(item.get("paused")) or (
                        item_state == "retrying"
                        and condition_id not in claimed_condition_ids
                    ):
                        continue
                    item_retry_at = item.get("next_retry_at")
                    if item_state == "waiting_retry" and item_retry_at:
                        try:
                            if now < _timestamp(item_retry_at, name="next_retry_at"):
                                continue
                        except ValueError:
                            continue
                    metadata_condition_ids.append(condition_id)
                metadata_condition_ids_tuple = tuple(metadata_condition_ids)
                if catalog_failure is not None and not condition_ids:
                    catalog_failure_error = catalog_error_details
                    raise ValueError("history_catalog_unknown")
                if callable(preparation_retry_claimer):
                    retryable_conditions = tuple(
                        dict.fromkeys(
                            str(item.get("condition_id") or "")
                            for item in preparation_items.values()
                            if isinstance(item, Mapping)
                            and str(item.get("condition_id") or "").strip()
                            and item.get("stage") == "metadata"
                            and str(item.get("condition_id") or "") in condition_ids
                            and item.get("state") == "waiting_retry"
                            and item.get("next_retry_at") is not None
                            and _timestamp(
                                item.get("next_retry_at"), name="next_retry_at"
                            )
                            <= now
                        )
                    )
                    claimed_condition_ids = {
                        str(item.get("condition_id"))
                        for item in preparation_retry_claimer(
                            now=now, condition_ids=retryable_conditions
                        )
                        if isinstance(item, Mapping)
                        and str(item.get("condition_id") or "").strip()
                    }
                    if callable(preparation_item_reader):
                        preparation_items = {
                            str(item.get("condition_id")): item
                            for item in preparation_item_reader()
                            if isinstance(item, Mapping)
                            and str(item.get("condition_id") or "").strip()
                        }
            except Exception as exc:
                error_type = catalog_failure_error or type(exc).__name__
                failed = self._preparation_failure(
                    self._now(), stage="catalog", error_type=error_type
                )
                return self._preparation_result(
                    failed,
                    outcome="failure",
                    reason=str(failed.get("last_error") or "unknown_error"),
                    display_state="unknown",
                    alert_pending=failed.get("alert_claimed_now") is True,
                )
            metadata_batch_reader = getattr(
                self.exchange, "lp_market_metadata_batch", None
            )
            if not callable(metadata_reader) and not callable(metadata_batch_reader):
                failed = self._preparation_failure(
                    self._now(), stage="metadata", error_type="history_readers_unavailable"
                )
                return self._preparation_result(
                    failed,
                    outcome="failure",
                    reason="history_readers_unavailable",
                    display_state="unknown",
                    alert_pending=failed.get("alert_claimed_now") is True,
                )
            metadata_value: Mapping[str, object] | None = None
            metadata_confirmed_absent: set[str] = set()
            metadata_failures: dict[str, str] = {}
            metadata_failure_facts: dict[str, Mapping[str, object]] = {}
            successful_metadata_retry_conditions: set[str] = set()
            if callable(metadata_batch_reader):
                metadata_by_condition: dict[str, object] = {}
                self._save_preparation(
                    {
                        "stage": "metadata",
                        "metadata_completed_count": 0,
                        "metadata_total_count": len(metadata_condition_ids_tuple),
                    },
                    expected_generation=generation,
                )

                def batch_failure_code(value: object) -> str:
                    if isinstance(value, Mapping):
                        raw_status = value.get("status")
                        if type(raw_status) is int:
                            return f"status{raw_status}"
                        raw_chain = value.get("error_chain", value.get("error_types"))
                        if isinstance(raw_chain, Sequence) and not isinstance(
                            raw_chain, (str, bytes)
                        ):
                            for chain_value in raw_chain:
                                code = self._safe_error_type(chain_value)
                                if any(
                                    marker in code.casefold()
                                    for marker in ("cert", "ssl", "auth", "forbidden")
                                ):
                                    return code
                        for field in (
                            "error_type",
                            "error",
                            "reason",
                            "code",
                            "type",
                        ):
                            if field in value:
                                return self._safe_error_type(value.get(field))
                        return "metadata_batch_failed"
                    return self._safe_error_type(value)

                for offset in range(
                    0, len(metadata_condition_ids_tuple), _LP_METADATA_BATCH_SIZE
                ):
                    if stop_event is not None and stop_event.is_set():
                        return self._preparation_result(
                            self.preparation_snapshot(),
                            outcome="cancelled",
                            display_state="cancelled",
                        )
                    metadata_batch_ids = metadata_condition_ids_tuple[
                        offset : offset + _LP_METADATA_BATCH_SIZE
                    ]
                    try:
                        batch_value = metadata_batch_reader(
                            metadata_batch_ids, stop_event=stop_event
                        )
                    except Exception as exc:
                        error_type = self._safe_error_type(type(exc).__name__)
                        metadata_failures.update(
                            {
                                condition_id: error_type
                                for condition_id in metadata_batch_ids
                            }
                        )
                        self._save_preparation(
                            {
                                "stage": "metadata",
                                "metadata_completed_count": len(
                                    (
                                        set(metadata_by_condition)
                                        - set(metadata_failures)
                                    )
                                    | metadata_confirmed_absent
                                ),
                                "metadata_total_count": len(metadata_condition_ids_tuple),
                                "last_progress_at": self._now(),
                            },
                            expected_generation=generation,
                        )
                        continue
                    if stop_event is not None and stop_event.is_set():
                        return self._preparation_result(
                            self.preparation_snapshot(),
                            outcome="cancelled",
                            display_state="cancelled",
                        )
                    if not isinstance(batch_value, Mapping):
                        metadata_failures.update(
                            {
                                condition_id: "metadata_batch_unknown"
                                for condition_id in metadata_batch_ids
                            }
                        )
                        continue
                    if str(batch_value.get("state") or "").lower() in {
                        "cancelled",
                        "canceled",
                    }:
                        return self._preparation_result(
                            self.preparation_snapshot(),
                            outcome="cancelled",
                            display_state="cancelled",
                        )
                    raw_markets = batch_value.get("markets")
                    if not isinstance(raw_markets, Mapping):
                        metadata_failures.update(
                            {
                                condition_id: "metadata_batch_unknown"
                                for condition_id in metadata_batch_ids
                            }
                        )
                        continue
                    requested = set(metadata_batch_ids)
                    returned_markets = {
                        str(key): value
                        for key, value in raw_markets.items()
                        if str(key) in requested and isinstance(value, Mapping)
                    }
                    raw_absent = batch_value.get("confirmed_absent_ids")
                    confirmed_absent = {
                        str(value)
                        for value in raw_absent
                        if isinstance(value, str) and value in requested
                    } if isinstance(raw_absent, Sequence) and not isinstance(raw_absent, (str, bytes)) else set()
                    raw_failed = batch_value.get("failed_ids")
                    if isinstance(raw_failed, Mapping):
                        failed_ids = {
                            str(key): value
                            for key, value in raw_failed.items()
                            if str(key) in requested
                        }
                    elif isinstance(raw_failed, Sequence) and not isinstance(raw_failed, (str, bytes)):
                        failed_ids = {str(value): "metadata_batch_failed" for value in raw_failed if str(value) in requested}
                    else:
                        failed_ids = {}
                    raw_failure_facts = batch_value.get("failure_facts")
                    raw_deferred = batch_value.get("deferred_ids")
                    deferred_ids = {
                        str(value)
                        for value in raw_deferred
                        if isinstance(value, str) and value in requested
                    } if isinstance(raw_deferred, Sequence) and not isinstance(raw_deferred, (str, bytes)) else set()
                    for condition_id, failure in failed_ids.items():
                        facts = (
                            raw_failure_facts.get(condition_id)
                            if isinstance(raw_failure_facts, Mapping)
                            else None
                        )
                        failure_value = facts if isinstance(facts, Mapping) else failure
                        metadata_failures[condition_id] = batch_failure_code(
                            failure_value
                        )
                        if isinstance(failure_value, Mapping):
                            metadata_failure_facts[condition_id] = dict(failure_value)
                    for condition_id in deferred_ids:
                        metadata_failures[condition_id] = "metadata_deferred"
                    resolved = (
                        set(returned_markets)
                        | confirmed_absent
                        | set(failed_ids)
                        | deferred_ids
                    )
                    metadata_confirmed_absent.update(
                        confirmed_absent - set(failed_ids) - deferred_ids
                    )
                    for condition_id in requested - resolved:
                        metadata_failures[condition_id] = "metadata_batch_incomplete"
                    metadata_by_condition.update(returned_markets)
                    self._save_preparation(
                        {
                            "stage": "metadata",
                            "metadata_completed_count": len(
                                (
                                    set(metadata_by_condition)
                                    - set(metadata_failures)
                                )
                                | metadata_confirmed_absent
                            ),
                            "metadata_total_count": len(metadata_condition_ids_tuple),
                            "last_progress_at": self._now(),
                        },
                        expected_generation=generation,
                    )
                metadata_value = metadata_by_condition
                if metadata_failures and callable(preparation_failure_writer):
                    failed_at = self._now().astimezone(UTC)
                    for condition_id, error_type in metadata_failures.items():
                        preparation_failure_writer(
                            condition_id,
                            generation=generation,
                            stage="metadata",
                            error=error_type,
                            failed_at=failed_at,
                            retry_after_seconds=(
                                metadata_failure_facts.get(condition_id, {}).get(
                                    "retry_after_seconds"
                                )
                            ),
                            retry_after_at=(
                                metadata_failure_facts.get(condition_id, {}).get(
                                    "retry_after_at"
                                )
                            ),
                        )
                    if callable(preparation_item_reader):
                        preparation_items = {
                            str(item.get("condition_id")): item
                            for item in preparation_item_reader()
                            if isinstance(item, Mapping)
                            and str(item.get("condition_id") or "").strip()
                        }
            else:
                try:
                    metadata_value = metadata_reader(
                        metadata_condition_ids_tuple, stop_event=stop_event
                    )  # type: ignore[misc]
                except Exception as exc:
                    failed = self._preparation_failure(
                        self._now(), stage="metadata", error_type=type(exc).__name__
                    )
                    return self._preparation_result(
                        failed,
                        outcome="failure",
                        reason=self._safe_error_type(type(exc).__name__),
                        display_state="unknown",
                        alert_pending=failed.get("alert_claimed_now") is True,
                    )
            if not isinstance(metadata_value, Mapping):
                failed = self._preparation_failure(
                    self._now(), stage="metadata", error_type="history_metadata_unknown"
                )
                return self._preparation_result(
                    failed,
                    outcome="failure",
                    reason="history_metadata_unknown",
                    display_state="unknown",
                    alert_pending=failed.get("alert_claimed_now") is True,
                )
            metadata_retry_completion_candidates = set(claimed_condition_ids)
            metadata_retry_completion_candidates.update(
                condition_id
                for condition_id, item in preparation_items.items()
                if item.get("state") == "waiting_retry"
                and item.get("retry_used") is False
                and item.get("paused") is False
            )
            successful_metadata_retry_conditions.update(
                condition_id
                for condition_id in metadata_retry_completion_candidates
                if condition_id in metadata_value
                and condition_id not in metadata_failures
                and isinstance(metadata_value[condition_id], Mapping)
                and metadata_value[condition_id].get("accepting_orders") is False
            )
            if not callable(history_reader):
                failed = self._preparation_failure(
                    self._now(), stage="history", error_type="history_readers_unavailable"
                )
                return self._preparation_result(
                    failed,
                    outcome="failure",
                    reason="history_readers_unavailable",
                    display_state="unknown",
                    alert_pending=failed.get("alert_claimed_now") is True,
                )

            targets: list[tuple[str, str]] = []
            for reward_market in market_rows:
                if reward_market.get("reward_active") is not True:
                    continue
                pool = _maybe_decimal(reward_market.get("daily_pool_usd"))
                if pool is None or pool <= 0:
                    continue
                condition_id = str(reward_market.get("condition_id") or "").strip()
                market = metadata_value.get(condition_id)
                if not isinstance(market, Mapping) or market.get("accepting_orders") is not True:
                    continue
                outcomes = market.get("outcomes")
                if not isinstance(outcomes, Mapping):
                    continue
                for raw_outcome in outcomes.values():
                    if not isinstance(raw_outcome, Mapping):
                        continue
                    token_id = str(raw_outcome.get("token_id") or "").strip()
                    if token_id:
                        targets.append((condition_id, token_id))
            targets = list(dict.fromkeys(targets))
            targets.sort(
                key=lambda identity: priority_index.get(identity[0], len(priority_index))
            )
            eligible_targets = tuple(targets)
            active_targets: list[tuple[str, str]] = []
            for identity in targets:
                condition_id = identity[0]
                item = preparation_items.get(condition_id)
                if item is None:
                    active_targets.append(identity)
                    continue
                state = str(item.get("state") or "")
                if bool(item.get("paused")) or (
                    state == "retrying" and condition_id not in claimed_condition_ids
                ):
                    continue
                next_retry_at = item.get("next_retry_at")
                if state == "waiting_retry" and next_retry_at:
                    try:
                        if now < _timestamp(next_retry_at, name="next_retry_at"):
                            continue
                    except ValueError:
                        continue
                active_targets.append(identity)
            targets = active_targets
            current_items = (
                preparation_item_reader()
                if callable(preparation_item_reader)
                else ()
            )
            catalog_confirmed_absent: set[str] = set()
            if catalog.get("state") == "known" and catalog.get("complete") is True:
                catalog_condition_ids = set(condition_ids)
                catalog_confirmed_absent = {
                    str(item.get("condition_id"))
                    for item in current_items
                    if isinstance(item, Mapping)
                    and str(item.get("condition_id") or "").strip()
                    and str(item.get("condition_id")) not in catalog_condition_ids
                    and item.get("state") == "waiting_retry"
                    and item.get("retry_used") is False
                    and item.get("paused") is False
                }
            clearable_condition_ids = (
                set(metadata_confirmed_absent)
                | catalog_confirmed_absent
                | successful_metadata_retry_conditions
            )
            if callable(preparation_clearer) and clearable_condition_ids:
                preparation_clearer(
                    clearable_condition_ids,
                    generation=generation,
                )
                current_items = (
                    preparation_item_reader()
                    if callable(preparation_item_reader)
                    else ()
                )
            if not targets:
                prepared_state = (
                    "known"
                    if catalog.get("state") == "known"
                    and catalog.get("complete") is True
                    else "partial"
                )
                self._publish_prepared_inputs(
                    catalog,
                    metadata_value,
                    state=prepared_state,
                )
                if catalog_failure is not None:
                    failed = self._preparation_failure(
                        self._now(),
                        stage=catalog_failure[0],
                        error_type=catalog_failure[1],
                    )
                    result = self._preparation_result(
                        failed,
                        outcome="failure",
                        reason=catalog_failure[1],
                        display_state=prepared_state,
                        alert_pending=failed.get("alert_claimed_now") is True,
                    )
                    result.update(
                        {
                            "checked_at": now,
                            "target_count": 0,
                            "updated_count": 0,
                            "unknown_count": 0,
                            "request_count": 0,
                        }
                    )
                    return result
                active_items = [
                    item
                    for item in current_items
                    if isinstance(item, Mapping)
                    and str(item.get("condition_id") or "").strip()
                ]
                if active_items:
                    retry_times: list[datetime] = []
                    waiting_retry = False
                    for item in active_items:
                        if item.get("state") != "waiting_retry":
                            continue
                        retry_at = item.get("next_retry_at")
                        if not retry_at:
                            continue
                        try:
                            retry_timestamp = _timestamp(
                                retry_at, name="next_retry_at"
                            )
                        except ValueError:
                            continue
                        retry_times.append(retry_timestamp)
                        waiting_retry = waiting_retry or now < retry_timestamp
                    next_retry_at = min(retry_times) if retry_times else None
                    probe_stage = next(
                        (
                            str(item.get("stage") or "history")
                            for item in active_items
                            if item.get("state") == "waiting_retry"
                        ),
                        None,
                    )
                    last_error = next(
                        (
                            str(item.get("error"))
                            for item in active_items
                            if item.get("error")
                        ),
                        None,
                    )
                    completed = self._save_preparation(
                        {
                            "state": "partial",
                            "stage": "metadata",
                            "failure_count": 0,
                            "paused": False,
                            "attempt": 0,
                            "last_success_at": None,
                            "last_progress_at": now,
                            "completed_count": 0,
                            "total_count": 0,
                            "next_retry_at": next_retry_at,
                            "next_probe_at": (
                                now + timedelta(seconds=60)
                                if waiting_retry
                                else None
                            ),
                            "last_probe_stage": probe_stage,
                            "last_error": last_error,
                            **self._ensure_fault_episode(now),
                        },
                        expected_generation=generation,
                    )
                    result = self._preparation_result(
                        completed,
                        outcome="waiting_retry" if waiting_retry else "failure",
                        reason=last_error,
                        display_state="partial",
                    )
                    result.update(
                        {
                            "checked_at": now,
                            "target_count": 0,
                            "updated_count": 0,
                            "unknown_count": 0,
                            "request_count": 0,
                            "errors": {
                                str(item.get("condition_id")): str(item.get("error"))
                                for item in active_items
                                if item.get("error")
                            },
                        }
                    )
                    item_alert = self._claim_preparation_item_alerts()
                    if item_alert is not None and item_alert.get("alert_pending") is True:
                        result["alert_pending"] = True
                        result["preparation"].update(deepcopy(dict(item_alert)))
                    return result
                completed = self._save_preparation(
                    {
                        "state": "ready",
                        "stage": "complete",
                        "failure_count": 0,
                        "paused": False,
                        "attempt": 0,
                        "last_success_at": now,
                        "last_progress_at": now,
                        "completed_count": 0,
                        "total_count": 0,
                        "next_retry_at": None,
                        "last_error": None,
                        **self._clear_unacknowledged_fault_episode(),
                    },
                    expected_generation=generation,
                )
                result = self._preparation_result(
                    completed, outcome="success", display_state=prepared_state
                )
                result.update(
                    {
                        "checked_at": now,
                        "target_count": 0,
                        "updated_count": 0,
                        "unknown_count": 0,
                        "request_count": 0,
                    }
                )
                return result

            self._publish_prepared_inputs(
                catalog, metadata_value, state="preparing"
            )
            self._save_preparation(
                {
                    "stage": "history",
                    "completed_count": 0,
                    "total_count": len(targets),
                    "last_progress_at": now,
                },
                expected_generation=generation,
            )

            summary_reader = getattr(self.store, "lp_price_history_summaries", None)
            sample_reader = getattr(self.store, "lp_price_history_samples_batch", None)
            summary_one = getattr(self.store, "lp_price_history_summary", None)
            samples_one = getattr(self.store, "lp_price_history_samples", None)
            batch_writer = getattr(self.store, "lp_save_price_history_batch", None)
            one_writer = getattr(self.store, "lp_save_price_history", None)
            identity_batches = tuple(
                tuple(targets[offset : offset + _LP_PRICE_HISTORY_BATCH_SIZE])
                for offset in range(0, len(targets), _LP_PRICE_HISTORY_BATCH_SIZE)
            )

            def cached_facts(
                identities: tuple[tuple[str, str], ...],
            ) -> tuple[dict[tuple[str, str], Mapping[str, object]], dict[tuple[str, str], list[dict[str, object]]]]:
                summaries: dict[tuple[str, str], Mapping[str, object]] = {}
                samples: dict[tuple[str, str], list[dict[str, object]]] = {}

                def reusable(summary: Mapping[str, object] | None) -> bool:
                    return bool(
                        summary
                        and str(summary.get("state") or "").lower()
                        in {"known", "ready", "eligible"}
                        and summary.get("checked_at") is not None
                    )

                if callable(summary_reader):
                    try:
                        value = summary_reader(identities, now=now)
                        if isinstance(value, Mapping):
                            summaries.update(
                                (key, item)
                                for key, item in value.items()
                                if isinstance(key, tuple) and isinstance(item, Mapping)
                            )
                    except Exception:
                        pass
                elif callable(summary_one):
                    for identity in identities:
                        try:
                            value = summary_one(*identity, now=now)
                        except Exception:
                            value = None
                        if isinstance(value, Mapping):
                            summaries[identity] = value
                sample_identities = tuple(
                    identity
                    for identity in identities
                    if not reusable(summaries.get(identity))
                )
                if callable(sample_reader) and sample_identities:
                    try:
                        value = sample_reader(sample_identities)
                        if isinstance(value, Mapping):
                            samples.update(
                                (key, [dict(item) for item in rows if isinstance(item, Mapping)])
                                for key, rows in value.items()
                                if isinstance(key, tuple) and isinstance(rows, (list, tuple))
                            )
                    except Exception:
                        pass
                elif callable(samples_one):
                    for identity in sample_identities:
                        try:
                            value = samples_one(*identity)
                        except Exception:
                            value = []
                        if isinstance(value, (list, tuple)):
                            samples[identity] = [dict(item) for item in value if isinstance(item, Mapping)]
                return summaries, samples

            def request_batch(
                identities: tuple[tuple[str, str], ...],
                start_ts: int,
            ) -> tuple[tuple[tuple[str, str], ...], object, str | None]:
                token_ids = tuple(token for _, token in identities)
                if stop_event is not None and stop_event.is_set():
                    return identities, None, "cancelled"
                try:
                    value = history_reader(
                        token_ids,
                        start_ts=start_ts,
                        end_ts=end_ts,
                        fidelity=1,
                        stop_event=stop_event,
                    )
                    return identities, value, None
                except Exception as exc:
                    return identities, None, type(exc).__name__

            def parse_samples(
                rows: object,
            ) -> tuple[dict[int, dict[str, object]], str | None]:
                if not isinstance(rows, (list, tuple)):
                    return {}, "history_missing"
                parsed: dict[int, dict[str, object]] = {}
                for row in rows:
                    if not isinstance(row, Mapping):
                        return {}, "history_values_invalid"
                    stamp = row.get("t", row.get("timestamp"))
                    price = _maybe_decimal(row.get("p", row.get("price")))
                    if type(stamp) is not int or price is None or price < 0 or price > 1:
                        return {}, "history_values_invalid"
                    parsed[stamp] = {"t": stamp, "p": price}
                return parsed, None

            def write_rows(rows: list[dict[str, object]]) -> None:
                if not rows:
                    return
                if callable(batch_writer):
                    batch_writer(rows, generation=generation)
                    return
                if callable(one_writer):
                    for row in rows:
                        one_writer(
                            str(row["condition_id"]),
                            str(row["token_id"]),
                            row["samples"],
                            row["summary"],
                        )

            updated_count = 0
            unknown_count = 0
            request_count = 0
            errors: dict[str, str] = {}
            operational_failure: tuple[str, str] | None = None
            history_retry_deadline: datetime | None = None
            market_failures: dict[
                str, tuple[str, str, str | None, Mapping[str, object] | None]
            ] = {}
            recorded_failure_conditions: set[str] = set()
            successful_conditions: set[str] = set(metadata_confirmed_absent)
            benign_history_errors = {
                "cancelled",
                "history_insufficient",
                "history_missing",
                "history_values_unknown",
                "history_window_incomplete",
            }
            completed_identities: set[tuple[str, str]] = set()
            successful_identities: set[tuple[str, str]] = set()
            updated_identities: set[tuple[str, str]] = set()
            unknown_identities: set[tuple[str, str]] = set()
            target_identities_by_condition_lists: dict[str, list[tuple[str, str]]] = {}
            for identity in eligible_targets:
                target_identities_by_condition_lists.setdefault(identity[0], []).append(identity)
            target_identities_by_condition = {
                condition_id: tuple(identities)
                for condition_id, identities in target_identities_by_condition_lists.items()
            }
            queued_batches = list(identity_batches)

            def persist_new_market_failures() -> None:
                if not callable(preparation_failure_writer):
                    return
                failed_at = self._now().astimezone(UTC)
                for condition_id, (
                    stage,
                    error_type,
                    token_id,
                    retry_facts,
                ) in market_failures.items():
                    if condition_id in recorded_failure_conditions:
                        continue
                    preparation_failure_writer(
                        condition_id,
                        generation=generation,
                        stage=stage,
                        error=error_type,
                        failed_at=failed_at,
                        token_id=token_id,
                        retry_after_seconds=(
                            retry_facts.get("retry_after_seconds")
                            if isinstance(retry_facts, Mapping)
                            else None
                        ),
                        retry_after_at=(
                            retry_facts.get("retry_after_at")
                            if isinstance(retry_facts, Mapping)
                            else None
                        ),
                    )
                    recorded_failure_conditions.add(condition_id)

            def dispatch_due_metadata(
                condition_ids: tuple[str, ...],
            ) -> tuple[tuple[str, str], ...]:
                """Resolve due metadata before claiming its history retry."""

                nonlocal metadata_value
                requested = tuple(
                    dict.fromkeys(
                        condition_id
                        for condition_id in condition_ids
                        if condition_id
                    )
                )
                requested = tuple(
                    sorted(
                        requested,
                        key=lambda condition_id: priority_index.get(
                            condition_id, len(priority_index)
                        ),
                    )
                )
                if not requested:
                    return ()
                returned_markets: dict[str, Mapping[str, object]] = {}
                failed_ids: dict[str, object] = {}
                raw_failure_facts: object = None

                def failure_code(value: object) -> str:
                    if isinstance(value, Mapping):
                        raw_status = value.get("status")
                        if type(raw_status) is int:
                            return f"status{raw_status}"
                        raw_chain = value.get("error_chain", value.get("error_types"))
                        if isinstance(raw_chain, Sequence) and not isinstance(
                            raw_chain, (str, bytes)
                        ):
                            for chain_value in raw_chain:
                                code = self._safe_error_type(chain_value)
                                if any(
                                    marker in code.casefold()
                                    for marker in ("cert", "ssl", "auth", "forbidden")
                                ):
                                    return code
                        for field in (
                            "error_type",
                            "error",
                            "reason",
                            "code",
                            "type",
                        ):
                            if field in value:
                                return self._safe_error_type(value.get(field))
                        return "metadata_batch_failed"
                    return self._safe_error_type(value)
                try:
                    if callable(metadata_batch_reader):
                        batch_value = metadata_batch_reader(
                            requested, stop_event=stop_event
                        )
                        if not isinstance(batch_value, Mapping):
                            raise ValueError("metadata_batch_unknown")
                        raw_markets = batch_value.get("markets")
                        if isinstance(raw_markets, Mapping):
                            returned_markets = {
                                str(key): value
                                for key, value in raw_markets.items()
                                if str(key) in requested and isinstance(value, Mapping)
                            }
                        raw_failed = batch_value.get("failed_ids")
                        if isinstance(raw_failed, Mapping):
                            failed_ids.update(
                                {
                                    str(key): value
                                    for key, value in raw_failed.items()
                                    if str(key) in requested
                                }
                            )
                        elif isinstance(raw_failed, Sequence) and not isinstance(
                            raw_failed, (str, bytes)
                        ):
                            failed_ids.update(
                                {
                                    str(value): "metadata_batch_failed"
                                    for value in raw_failed
                                    if str(value) in requested
                                }
                            )
                        raw_failure_facts = batch_value.get("failure_facts")
                        raw_absent = batch_value.get("confirmed_absent_ids")
                        confirmed_absent = (
                            {
                                str(value)
                                for value in raw_absent
                                if isinstance(value, str) and value in requested
                            }
                            if isinstance(raw_absent, Sequence)
                            and not isinstance(raw_absent, (str, bytes))
                            else set()
                        )
                        confirmed_absent -= set(failed_ids)
                        successful_conditions.update(confirmed_absent)
                    elif callable(metadata_reader):
                        raw_value = metadata_reader(requested, stop_event=stop_event)
                        if isinstance(raw_value, Mapping):
                            returned_markets = {
                                str(key): value
                                for key, value in raw_value.items()
                                if str(key) in requested and isinstance(value, Mapping)
                            }
                    else:
                        failed_ids = {
                            condition_id: "history_readers_unavailable"
                            for condition_id in requested
                        }
                except Exception as exc:
                    failed_ids = {
                        condition_id: self._safe_error_type(type(exc).__name__)
                        for condition_id in requested
                    }

                retry_identities: list[tuple[str, str]] = []
                updated_metadata = dict(metadata_value)
                reward_by_condition = {
                    str(row.get("condition_id") or ""): row
                    for row in market_rows
                    if isinstance(row, Mapping)
                }
                for condition_id in requested:
                    failure = failed_ids.get(condition_id)
                    facts = (
                        raw_failure_facts.get(condition_id)
                        if isinstance(raw_failure_facts, Mapping)
                        else None
                    )
                    failure_value = facts if isinstance(facts, Mapping) else failure
                    if isinstance(failure_value, Mapping):
                        metadata_failure_facts[condition_id] = dict(failure_value)
                    market = returned_markets.get(condition_id)
                    if not isinstance(market, Mapping):
                        failed_ids.setdefault(
                            condition_id, "metadata_batch_incomplete"
                        )
                        failure_value = (
                            failed_ids[condition_id]
                            if failure_value is None
                            else failure_value
                        )
                        market_failures.setdefault(
                            condition_id,
                            (
                                "metadata",
                                failure_code(failure_value),
                                None,
                                (
                                    dict(failure_value)
                                    if isinstance(failure_value, Mapping)
                                    else None
                                ),
                            ),
                        )
                        continue
                    reward_market = reward_by_condition.get(condition_id)
                    pool = (
                        _maybe_decimal(reward_market.get("daily_pool_usd"))
                        if isinstance(reward_market, Mapping)
                        else None
                    )
                    outcomes = market.get("outcomes")
                    if (
                        not isinstance(reward_market, Mapping)
                        or reward_market.get("reward_active") is not True
                        or pool is None
                        or pool <= 0
                        or market.get("accepting_orders") is not True
                        or not isinstance(outcomes, Mapping)
                    ):
                        market_failures.setdefault(
                            condition_id,
                            (
                                "metadata",
                                "metadata_market_unusable",
                                None,
                                None,
                            ),
                        )
                        continue
                    identities = tuple(
                        (condition_id, str(raw_outcome.get("token_id") or "").strip())
                        for raw_outcome in outcomes.values()
                        if isinstance(raw_outcome, Mapping)
                        and str(raw_outcome.get("token_id") or "").strip()
                    )
                    if not identities:
                        market_failures.setdefault(
                            condition_id,
                            (
                                "metadata",
                                "metadata_market_unusable",
                                None,
                                None,
                            ),
                        )
                        continue
                    updated_metadata[condition_id] = market
                    target_identities_by_condition[condition_id] = identities
                    retry_identities.extend(identities)
                if retry_identities:
                    metadata_value = updated_metadata
                    self._publish_prepared_inputs(
                        catalog, metadata_value, state="preparing"
                    )
                return tuple(retry_identities)

            while queued_batches:
                if stop_event is not None and stop_event.is_set():
                    break
                if callable(preparation_retry_claimer):
                    retry_now = self._now()
                    retryable_items = (
                        preparation_item_reader()
                        if callable(preparation_item_reader)
                        else ()
                    )
                    claimable_conditions = tuple(
                        dict.fromkeys(
                            str(item.get("condition_id") or "")
                            for item in retryable_items
                            if isinstance(item, Mapping)
                            and str(item.get("condition_id") or "").strip()
                            and item.get("state") == "waiting_retry"
                            and item.get("next_retry_at") is not None
                            and (
                                str(item.get("condition_id") or "")
                                in target_identities_by_condition
                                or (
                                    item.get("stage") == "metadata"
                                    and str(item.get("condition_id") or "")
                                    in condition_ids
                                )
                            )
                            and (
                                _timestamp(
                                    item.get("next_retry_at"),
                                    name="next_retry_at",
                                )
                                <= retry_now
                            )
                        )
                    )
                    due_retries = preparation_retry_claimer(
                        now=retry_now,
                        condition_ids=claimable_conditions,
                    )
                    due_metadata_ids = tuple(
                        str(item.get("condition_id") or "")
                        for item in due_retries
                        if isinstance(item, Mapping)
                        and item.get("stage") == "metadata"
                    )
                    metadata_retry_identities = dispatch_due_metadata(due_metadata_ids)
                    if metadata_retry_identities:
                        targets.extend(metadata_retry_identities)
                        targets[:] = list(dict.fromkeys(targets))
                        for offset in range(
                            0,
                            len(metadata_retry_identities),
                            _LP_PRICE_HISTORY_BATCH_SIZE,
                        ):
                            queued_batches.insert(
                                offset // _LP_PRICE_HISTORY_BATCH_SIZE,
                                tuple(
                                    metadata_retry_identities[
                                        offset : offset + _LP_PRICE_HISTORY_BATCH_SIZE
                                    ]
                                ),
                            )
                    retry_identities = tuple(
                        identity
                        for item in due_retries
                        if isinstance(item, Mapping)
                        and item.get("stage") != "metadata"
                        for identity in target_identities_by_condition.get(
                            str(item.get("condition_id") or ""), ()
                        )
                    )
                    if retry_identities:
                        targets.extend(retry_identities)
                        targets[:] = list(dict.fromkeys(targets))
                        retry_identity_set = set(retry_identities)
                        queued_batches = [
                            tuple(
                                identity
                                for identity in batch
                                if identity not in retry_identity_set
                            )
                            for batch in queued_batches
                        ]
                        queued_batches = [batch for batch in queued_batches if batch]
                        retry_batches = [
                            tuple(retry_identities[offset : offset + _LP_PRICE_HISTORY_BATCH_SIZE])
                            for offset in range(
                                0, len(retry_identities), _LP_PRICE_HISTORY_BATCH_SIZE
                            )
                        ]
                        queued_batches[0:0] = retry_batches
                        for item in due_retries:
                            if isinstance(item, Mapping):
                                recorded_failure_conditions.discard(
                                    str(item.get("condition_id") or "")
                                )
                batch_group = tuple(
                    queued_batches[:_LP_PRICE_HISTORY_MAX_CONCURRENCY]
                )
                del queued_batches[:_LP_PRICE_HISTORY_MAX_CONCURRENCY]
                cache_by_batch: dict[
                    tuple[tuple[str, str], ...],
                    tuple[
                        dict[tuple[str, str], Mapping[str, object]],
                        dict[tuple[str, str], list[dict[str, object]]],
                    ],
                ] = {}
                request_group: list[tuple[tuple[tuple[str, str], ...], int]] = []
                for identity_batch in batch_group:
                    summaries, cached_samples = cached_facts(identity_batch)
                    missing_identities: list[tuple[str, str]] = []
                    for identity in identity_batch:
                        summary = summaries.get(identity)
                        state = str(summary.get("state") or "").lower() if summary else ""
                        if (
                            state in {"known", "ready", "eligible"}
                            and summary is not None
                            and summary.get("checked_at") is not None
                        ):
                            completed_identities.add(identity)
                            successful_identities.add(identity)
                        else:
                            missing_identities.append(identity)
                    completed_count = len(completed_identities)
                    if not missing_identities:
                        continue
                    request_identities = tuple(missing_identities)
                    cache_by_batch[request_identities] = (summaries, cached_samples)
                    starts: list[int] = []
                    for identity in request_identities:
                        rows = cached_samples.get(identity, [])
                        stamps = [
                            row.get("t")
                            for row in rows
                            if isinstance(row.get("t"), int)
                        ]
                        starts.append(
                            max(
                                int(window_start.timestamp()),
                                max(stamps) - int(_LP_PRICE_HISTORY_OVERLAP.total_seconds())
                                if stamps
                                else int(window_start.timestamp()),
                            )
                        )
                    request_group.append((request_identities, min(starts)))
                self._save_preparation(
                    {
                        "stage": "history",
                        "completed_count": completed_count,
                        "total_count": len(targets),
                        "last_progress_at": self._now(),
                    },
                    expected_generation=generation,
                )
                if not request_group:
                    continue
                with ThreadPoolExecutor(
                    max_workers=min(_LP_PRICE_HISTORY_MAX_CONCURRENCY, len(request_group)),
                    thread_name_prefix="prediction-lp-history",
                ) as executor:
                    futures = {
                        executor.submit(request_batch, identities, start_ts): (identities, start_ts)
                        for identities, start_ts in request_group
                    }
                    for future in as_completed(futures):
                        identities, start_ts = futures[future]
                        del start_ts
                        request_count += 1
                        try:
                            returned_identities, payload, error = future.result()
                        except Exception as exc:
                            returned_identities, payload, error = identities, None, type(exc).__name__
                        if error is not None and error != "cancelled":
                            operational_failure = (
                                "history", self._safe_error_type(error)
                            )
                        summaries, cached_samples = cache_by_batch[identities]
                        history_map = payload.get("history") if isinstance(payload, Mapping) else None
                        if not isinstance(history_map, Mapping):
                            history_map = {}
                        payload_errors = payload.get("errors") if isinstance(payload, Mapping) else None
                        if not isinstance(payload_errors, Mapping):
                            payload_errors = {}
                        retry_facts = (
                            {
                                field: payload[field]
                                for field in ("retry_after_seconds", "retry_after_at")
                                if isinstance(payload, Mapping)
                                and payload.get(field) is not None
                            }
                            if isinstance(payload, Mapping)
                            else {}
                        )
                        payload_retry_deadline, _ = self._retry_after_deadline(
                            now, payload
                        )
                        if payload_retry_deadline is not None and (
                            history_retry_deadline is None
                            or payload_retry_deadline > history_retry_deadline
                        ):
                            history_retry_deadline = payload_retry_deadline
                        for payload_error in payload_errors.values():
                            if (
                                isinstance(payload_error, str)
                                and payload_error not in benign_history_errors
                            ):
                                operational_failure = (
                                    "history",
                                    self._safe_error_type(payload_error),
                                )
                                break
                        rows_to_write: list[dict[str, object]] = []
                        for condition_id, token_id in returned_identities:
                            identity = (condition_id, token_id)
                            prior = dict(summaries.get(identity, {}))
                            previous_rows = cached_samples.get(identity, [])
                            raw_rows = history_map.get(token_id)
                            if error is None and isinstance(raw_rows, (list, tuple)):
                                new_points, parse_error = parse_samples(raw_rows)
                            else:
                                upstream_error = payload_errors.get(token_id)
                                parse_error = (
                                    upstream_error
                                    if isinstance(upstream_error, str) and upstream_error
                                    else error or "history_missing"
                                )
                                new_points = {}
                            merged, merge_error = parse_samples(previous_rows)
                            reason = parse_error or merge_error
                            failure_code: str | None = None
                            if error is not None and error != "cancelled":
                                failure_code = error
                            else:
                                payload_error = payload_errors.get(token_id)
                                if (
                                    isinstance(payload_error, str)
                                    and payload_error not in benign_history_errors
                                ):
                                    failure_code = payload_error
                                elif reason is not None and reason != "cancelled":
                                    # A missing or incomplete market history
                                    # is still an isolated market failure.  It
                                    # must return to its low-frequency retry
                                    # queue rather than leaving a claimed
                                    # owner lease in retrying forever.
                                    failure_code = reason
                            if failure_code is not None:
                                market_failures.setdefault(
                                    condition_id,
                                    (
                                        "history",
                                        self._safe_error_type(failure_code),
                                        token_id,
                                        retry_facts or None,
                                    ),
                                )
                            if reason is None:
                                merged.update(new_points)
                                bounded = {
                                    stamp: row
                                    for stamp, row in merged.items()
                                    if int(window_start.timestamp()) <= stamp <= end_ts
                                }
                                if len(bounded) < 2:
                                    reason = "history_insufficient"
                                else:
                                    first_stamp = min(bounded)
                                    last_stamp = max(bounded)
                                    if first_stamp > int(window_start.timestamp()) + 60 or last_stamp < end_ts - 60:
                                        reason = "history_window_incomplete"
                            if reason is None:
                                successful_identities.add(identity)
                                successful_conditions.add(condition_id)
                            if reason is None:
                                updated_identities.add(identity)
                                unknown_identities.discard(identity)
                                errors.pop(token_id, None)
                                prices = [row["p"] for row in bounded.values()]
                                latest_sample = bounded[max(bounded)]
                                summary: dict[str, object] = {
                                    "state": "known",
                                    "amplitude": max(prices) - min(prices),
                                    "checked_at": now,
                                    "window_start": window_start,
                                    "window_end": window_end,
                                "sample_count": len(bounded),
                                "latest_midpoint": latest_sample["p"],
                                "valid_until": now + timedelta(hours=24),
                                "last_attempt_at": now,
                                "preparation_generation": generation,
                            }
                                samples_for_store = [bounded[stamp] for stamp in sorted(bounded)]
                            else:
                                unknown_identities.add(identity)
                                updated_identities.discard(identity)
                                errors[token_id] = reason
                                samples_for_store = previous_rows
                                summary = prior
                                summary["last_attempt_at"] = now
                                summary["last_error"] = reason
                                if not summary:
                                    summary = {
                                        "state": "unknown",
                                        "checked_at": None,
                                        "last_attempt_at": now,
                                        "reason": reason,
                                    }
                                elif summary.get("checked_at") is None:
                                    summary["state"] = "unknown"
                                summary["preparation_generation"] = generation
                            rows_to_write.append(
                                {
                                    "condition_id": condition_id,
                                    "token_id": token_id,
                                    "samples": samples_for_store,
                                    "summary": summary,
                                }
                            )
                        write_rows(rows_to_write)
                        completed_identities.update(returned_identities)
                        completed_count = len(completed_identities)
                        updated_count = len(updated_identities)
                        unknown_count = len(unknown_identities)
                        self._save_preparation(
                            {
                                "stage": "history",
                                "completed_count": completed_count,
                                "total_count": len(targets),
                                "last_progress_at": self._now(),
                            },
                            expected_generation=generation,
                        )
                        del rows_to_write, payload, history_map, future
                persist_new_market_failures()
                for condition_id in tuple(market_failures):
                    if not any(
                        identity in unknown_identities
                        for identity in target_identities_by_condition.get(condition_id, ())
                    ):
                        market_failures.pop(condition_id, None)
                        recorded_failure_conditions.discard(condition_id)
                del cache_by_batch, request_group
            for condition_id, identities in target_identities_by_condition.items():
                if identities and all(
                    identity in successful_identities for identity in identities
                ):
                    successful_conditions.add(condition_id)
            persist_new_market_failures()
            if (
                operational_failure is not None
                and operational_failure[0] == "history"
                and not market_failures
                and not unknown_identities
            ):
                operational_failure = None
            if callable(preparation_clearer) and successful_conditions:
                preparation_clearer(
                    (
                        condition_id
                        for condition_id in successful_conditions
                        if condition_id not in market_failures
                    ),
                    generation=generation,
                )
            if stop_event is not None and stop_event.is_set():
                return self._preparation_result(
                    self.preparation_snapshot(),
                    outcome="cancelled",
                    display_state="cancelled",
                )
            usable_identities = successful_identities - unknown_identities
            unresolved_identities = unknown_identities - successful_identities
            state = (
                "known"
                if usable_identities and not unresolved_identities
                else "partial"
                if usable_identities
                else "unknown"
            )
            self._publish_prepared_inputs(catalog, metadata_value, state=state)
            if operational_failure is None:
                operational_failure = catalog_failure
            if operational_failure is not None:
                stage, error_type = operational_failure
                if market_failures and callable(preparation_item_reader):
                    current_items = preparation_item_reader()
                    retry_times = [
                        item.get("next_retry_at")
                        for item in current_items
                        if isinstance(item, Mapping) and item.get("next_retry_at")
                    ]
                    probe_stage = next(
                        (
                            str(item.get("stage") or stage)
                            for item in current_items
                            if isinstance(item, Mapping)
                            and item.get("state") == "waiting_retry"
                        ),
                        stage,
                    )
                    partial = self._save_preparation(
                        {
                            "state": "partial",
                            "stage": stage,
                            "failure_count": 0,
                            "paused": False,
                            "attempt": 0,
                            "last_success_at": None,
                            "last_progress_at": self._now(),
                            "completed_count": completed_count,
                            "total_count": len(targets),
                            "next_retry_at": min(retry_times) if retry_times else None,
                            "next_probe_at": max(
                                self._now() + timedelta(seconds=60),
                                history_retry_deadline
                                if history_retry_deadline is not None
                                else self._now() + timedelta(seconds=60),
                            ),
                            "last_probe_stage": probe_stage,
                            "last_error": error_type,
                            **self._ensure_fault_episode(self._now().astimezone(UTC)),
                        },
                        expected_generation=generation,
                    )
                    result = self._preparation_result(
                        partial,
                        outcome="failure",
                        reason=error_type,
                        display_state=state,
                    )
                    result.update(
                        {
                            "target_count": len(targets),
                            "updated_count": updated_count,
                            "unknown_count": unknown_count,
                            "request_count": request_count,
                            "checked_at": now,
                            "errors": errors,
                        }
                    )
                    item_alert = self._claim_preparation_item_alerts()
                    if item_alert is not None and item_alert.get("alert_pending") is True:
                        result["alert_pending"] = True
                        result["preparation"].update(deepcopy(dict(item_alert)))
                    return result
                failed = self._preparation_failure(
                    self._now(), stage=stage, error_type=error_type
                )
                result = self._preparation_result(
                    failed,
                    outcome="failure",
                    reason=error_type,
                    display_state=state,
                    alert_pending=failed.get("alert_claimed_now") is True,
                )
                result.update(
                    {
                        "target_count": len(targets),
                        "updated_count": updated_count,
                        "unknown_count": unknown_count,
                        "request_count": request_count,
                        "checked_at": now,
                        "errors": errors,
                    }
                )
                return result
            remaining_items = (
                preparation_item_reader() if callable(preparation_item_reader) else []
            )
            final_state = "partial" if remaining_items else "ready"
            waiting_retry = False
            next_retry_at: object | None = None
            probe_stage: str | None = None
            for item in remaining_items:
                if not isinstance(item, Mapping) or item.get("state") != "waiting_retry":
                    continue
                if probe_stage is None:
                    probe_stage = str(item.get("stage") or "history")
                retry_at = item.get("next_retry_at")
                if retry_at:
                    try:
                        retry_timestamp = _timestamp(retry_at, name="next_retry_at")
                        waiting_retry = now < retry_timestamp
                        if waiting_retry and (
                            next_retry_at is None
                            or retry_timestamp < _timestamp(
                                next_retry_at, name="next_retry_at"
                            )
                        ):
                            next_retry_at = retry_timestamp
                    except ValueError:
                        waiting_retry = True
            completed = self._save_preparation(
                {
                    "state": final_state,
                    "stage": "complete",
                    "failure_count": 0,
                    "paused": False,
                    "attempt": 0,
                    "last_progress_at": now,
                    "completed_count": len(targets),
                    "total_count": len(targets),
                    "next_retry_at": next_retry_at,
                    "next_probe_at": (
                        now + timedelta(seconds=60) if waiting_retry else None
                    ),
                    "last_probe_stage": probe_stage,
                    "last_error": None,
                    **({"last_success_at": now} if final_state == "ready" else {}),
                    **(
                        self._clear_unacknowledged_fault_episode()
                        if final_state == "ready"
                        else {}
                    ),
                    **(
                        self._ensure_fault_episode(now)
                        if final_state == "partial"
                        else {}
                    ),
                },
                expected_generation=generation,
            )
            result = self._preparation_result(
                completed,
                outcome="waiting_retry" if waiting_retry else "success",
                reason="retry_not_due" if waiting_retry else None,
                display_state=state if final_state == "ready" else final_state,
            )
            result.update(
                {
                    "target_count": len(targets),
                    "updated_count": updated_count,
                    "unknown_count": unknown_count,
                    "request_count": request_count,
                    "checked_at": now,
                    "errors": errors,
                }
            )
            return result
        finally:
            if preparation_owner_acquired:
                owner_releaser = getattr(
                    self.store, "lp_release_preparation_owner", None
                )
                if callable(owner_releaser):
                    owner_releaser()
            self._price_history_refresh_lock.release()

    def _restore_candidate_snapshot(
        self, saved: Mapping[str, object] | None = None
    ) -> None:
        """Restore the rolling pool, rotation schedule, and confirmations.

        Issue #157: pool rows keep their original ``updated_at`` and
        ``expires_at``; reads judge expiry against the current clock, so a
        row saved before a restart ages out exactly when it would have
        without the restart, and reading never extends its validity.
        """

        if saved is None:
            reader = getattr(self.store, "lp_screening_snapshot", None)
            if not callable(reader):
                return
            try:
                saved = reader()
            except Exception:
                return
        if not isinstance(saved, Mapping):
            return
        pool = saved.get("pool")
        if not isinstance(pool, Mapping) or not pool:
            # Pre-pool snapshots carry whole-round rows without per-row
            # judgment times.  They cannot be presented as valid estimates
            # under the rolling contract, so the pool starts empty — but the
            # row metadata is still kept: preparation prioritization (issue
            # #152 era) consumes the persisted selected_results markers.
            with self._candidate_state_lock:
                self._candidate_attempted_at = None
                # state/complete are whole-round claims: a legacy snapshot
                # must not present itself as a valid result, so only the
                # prioritization inputs are restored.
                for key in (
                    "selected_results",
                    "recommendations",
                    "event_end_confirmations",
                ):
                    if key in saved:
                        self._candidate_snapshot[key] = deepcopy(saved[key])
                self._candidate_snapshot["scanning"] = False
            return
        restored_pool: dict[str, dict[str, object]] = {}
        for condition_id, row in pool.items():
            key = str(condition_id or "").strip()
            if key and isinstance(row, Mapping):
                restored_pool[key] = dict(row)
        raw_rotation = saved.get("rotation")
        restored_rotation: dict[str, dict[str, object]] = {}
        if isinstance(raw_rotation, Mapping):
            for condition_id, entry in raw_rotation.items():
                key = str(condition_id or "").strip()
                if key and isinstance(entry, Mapping):
                    restored_rotation[key] = dict(entry)
        with self._candidate_state_lock:
            self._candidate_pool = restored_pool
            self._candidate_rotation = restored_rotation
            confirmations = saved.get("event_end_confirmations")
            if isinstance(confirmations, Mapping):
                self._candidate_snapshot["event_end_confirmations"] = deepcopy(
                    dict(confirmations)
                )
            self._candidate_snapshot["scanning"] = False
            self._candidate_snapshot["last_attempt_at"] = saved.get(
                "last_attempt_at"
            )
            try:
                self._candidate_attempted_at = _timestamp(
                    saved.get("last_attempt_at"), name="last_attempt_at"
                )
            except ValueError:
                self._candidate_attempted_at = None

    def _candidate_queue_state_build(
        self, *, stop_event: threading.Event | None = None
    ) -> dict[str, object] | None:
        """Build (or reuse) the exploration queues from prepared inputs.

        Issue #157: the base-filter queues and the direction facts behind
        them are cached in memory keyed by the prepared-inputs version and
        rebuilt only when those inputs change — batches rotate through the
        cached order without re-reading the catalog.  Returns ``None``
        while preparation is pending or the catalog is unusable.
        """

        # The available budget feeds the over-available exclusion, so the
        # reservation signature joins the cache key: a new active
        # reservation invalidates the cached queues.
        reservations = self._candidate_reservations()
        reservation_signature = tuple(
            (str(entry.get("order_id")), str(entry.get("amount")))
            for entry in reservations
        )
        with self._candidate_state_lock:
            version = self._prepared_inputs_version
            cached = self._candidate_queue_state
        if (
            cached is not None
            and cached.get("version") == version
            and cached.get("reservation_signature") == reservation_signature
        ):
            return cached
        prepared = self._prepared_input_snapshot()
        if not isinstance(prepared, Mapping):
            return None
        catalog = prepared.get("catalog")
        metadata_value = prepared.get("metadata")
        if not isinstance(catalog, Mapping):
            return None
        raw_markets = (
            catalog.get("markets") if isinstance(catalog, Mapping) else None
        )
        if not isinstance(raw_markets, (list, tuple)):
            return None
        market_rows = [dict(row) for row in raw_markets if isinstance(row, Mapping)]
        if not isinstance(metadata_value, Mapping):
            return None
        metadata_by_condition = {
            str(key): value
            for key, value in metadata_value.items()
            if isinstance(key, str) and isinstance(value, Mapping)
        }
        catalog_reader = getattr(self.exchange, "lp_reward_catalog", None)
        account_reader = getattr(
            self.exchange, "lp_account_snapshot_shared", None
        )
        if not callable(account_reader):
            account_reader = getattr(self.exchange, "lp_account_snapshot", None)
        if not callable(catalog_reader) or not callable(account_reader):
            return None
        condition_ids = tuple(
            dict.fromkeys(
                str(row.get("condition_id") or "").strip()
                for row in market_rows
                if str(row.get("condition_id") or "").strip()
            )
        )
        missing_metadata_condition_ids = tuple(
            condition_id
            for condition_id in condition_ids
            if condition_id not in metadata_by_condition
        )
        account: Mapping[str, object] | None = None
        if market_rows:
            # An empty catalog needs no budget facts at all.
            try:
                account_value = account_reader()
            except Exception:
                account_value = None
            if (
                isinstance(account_value, Mapping)
                and account_value.get("authenticated") is True
            ):
                account = account_value
        checked_at = self._now()
        cache_batch_reader = getattr(
            self.store, "lp_price_history_summaries", None
        )
        cache_reader = getattr(self.store, "lp_price_history_summary", None)
        saved_screening = getattr(
            self.store, "lp_screening_snapshot", lambda: None
        )()
        saved_confirmations = (
            saved_screening.get("event_end_confirmations", {})
            if isinstance(saved_screening, Mapping)
            else {}
        )
        event_end_confirmations = (
            deepcopy(dict(saved_confirmations))
            if isinstance(saved_confirmations, Mapping)
            else {}
        )
        direction_facts: list[dict[str, object]] = []
        complete = (
            isinstance(catalog, Mapping)
            and catalog.get("state") == "known"
            and catalog.get("complete") is True
            and not missing_metadata_condition_ids
        )
        cache_identities = tuple(
            (
                condition_id,
                str(outcome.get("token_id") or "").strip(),
            )
            for reward_market in market_rows
            for condition_id in (
                str(reward_market.get("condition_id") or "").strip(),
            )
            for market_meta in (metadata_by_condition.get(condition_id),)
            if isinstance(market_meta, Mapping)
            and isinstance(market_meta.get("outcomes"), Mapping)
            for outcome in cast(
                Mapping[object, object], market_meta["outcomes"]
            ).values()
            if isinstance(outcome, Mapping)
            and str(outcome.get("token_id") or "").strip()
        )
        cached_summaries: Mapping[tuple[str, str], Mapping[str, object]] = {}
        if callable(cache_batch_reader):
            try:
                batch_value = cache_batch_reader(cache_identities, now=checked_at)
            except Exception:
                batch_value = {}
            if isinstance(batch_value, Mapping):
                cached_summaries = batch_value
        reward_market_by_condition: dict[str, Mapping[str, object]] = {}
        for reward_market in market_rows:
            condition_id = str(reward_market.get("condition_id") or "").strip()
            market_meta = metadata_by_condition.get(condition_id)
            if market_meta is None:
                complete = False
                continue
            raw_outcomes = market_meta.get("outcomes")
            if not isinstance(raw_outcomes, Mapping):
                complete = False
                continue
            reward_minimum, reward_spread = _lp_reward_terms(
                market_meta,
                reward_market,
            )
            if reward_minimum is None or reward_spread is None:
                complete = False
            confirmation = event_end_confirmations.get(condition_id)
            reward_deadline = self._reward_guidance_deadline(reward_market)
            reward_market_by_condition[condition_id] = reward_market
            for outcome_key, raw_outcome in raw_outcomes.items():
                if str(outcome_key).lower() not in {"yes", "no"} or not isinstance(
                    raw_outcome, Mapping
                ):
                    continue
                token_id = str(raw_outcome.get("token_id") or "").strip()
                if not token_id:
                    complete = False
                    continue
                summary: Mapping[str, object] | None = None
                cache_key = (condition_id, token_id)
                cached_summary = cached_summaries.get(cache_key)
                if isinstance(cached_summary, Mapping):
                    summary = cached_summary
                elif not callable(cache_batch_reader) and callable(cache_reader):
                    try:
                        cached = cache_reader(
                            condition_id, token_id, now=checked_at
                        )
                    except Exception:
                        cached = None
                    if isinstance(cached, Mapping):
                        summary = cached
                direction_facts.append(
                    _lp_direction_fact(
                        market_meta,
                        reward_market,
                        condition_id=condition_id,
                        token_id=token_id,
                        outcome=str(raw_outcome.get("label") or outcome_key).upper(),
                        reward_checked_at=catalog.get("checked_at"),
                        reward_guidance_deadline=reward_deadline,
                        event_end_confirmation=confirmation,
                        history_summary=summary,
                        account=account,
                    )
                )

        from .polymarket_lp_views import lp_trial_candidates

        available_facts: dict[str, object] = {}
        if account is not None:
            adjusted_account = _account_after_reservations(
                account, self._candidate_reservations()
            )
            if isinstance(adjusted_account, Mapping):
                adjusted_balance = _maybe_decimal(
                    adjusted_account.get("balance")
                )
                adjusted_allowance = _maybe_decimal(
                    adjusted_account.get("allowance")
                )
                if adjusted_balance is not None and adjusted_allowance is not None:
                    available_facts["available_capital"] = min(
                        adjusted_balance, adjusted_allowance
                    )
        trial = lp_trial_candidates(
            direction_facts,
            competition=self._competition_entries(),
            account_budget_facts=available_facts,
            now=checked_at,
            competition_round_checked_at=(
                self._competition_state.get("round_checked_at")
            ),
        )
        queue_normal = [
            dict(row)
            for row in (trial.get("queue_normal") or ())
            if isinstance(row, Mapping)
        ]
        queue_backup = [
            dict(row)
            for row in (trial.get("queue_backup") or ())
            if isinstance(row, Mapping)
        ]
        directions_by_condition: dict[str, list[Mapping[str, object]]] = {}
        for direction in direction_facts:
            if not isinstance(direction, Mapping):
                continue
            market = direction.get("market")
            if not isinstance(market, Mapping):
                continue
            direction_condition = str(market.get("condition_id") or "").strip()
            if direction_condition:
                directions_by_condition.setdefault(
                    direction_condition, []
                ).append(direction)
        trial_funnel = dict(trial.get("funnel") or {})
        queue_funnel = {
            key: deepcopy(trial_funnel.get(key))
            for key in (
                "read",
                "base",
                "sort",
                "competition_known",
                "competition_unknown",
                "excluded",
                "competition_light",
                "competition_mid",
                "competition_round_checked_at",
                "normal_queue_count",
                "backup_queue_count",
                "reference_price_unknown",
                "compared_range",
                "reasons",
                "budget",
            )
            if key in trial_funnel
        }
        queue_funnel["compared_range"] = deepcopy(
            trial.get("compared_range") or {}
        )
        queue_funnel["queue_total"] = len(queue_normal) + len(queue_backup)
        queue_condition_ids: dict[str, None] = {}
        for candidate in (*queue_normal, *queue_backup):
            if not isinstance(candidate, Mapping):
                continue
            condition_id = str(candidate.get("condition_id") or "").strip()
            if condition_id:
                queue_condition_ids[condition_id] = None
        # Keep the published (JSON-like) funnel shape, as the whole-scan
        # snapshot did through its durable-store round trip.
        queue_funnel = _pool_published_value(queue_funnel)
        state = {
            "version": version,
            "reservation_signature": reservation_signature,
            "queue_normal": queue_normal,
            "queue_backup": queue_backup,
            "directions_by_condition": directions_by_condition,
            "metadata_by_condition": metadata_by_condition,
            "reward_market_by_condition": reward_market_by_condition,
            "catalog_checked_at": catalog.get("checked_at"),
            "catalog_is_known": (
                isinstance(catalog, Mapping)
                and catalog.get("state") == "known"
            ),
            "catalog_complete": (
                isinstance(catalog, Mapping)
                and catalog.get("complete") is True
            ),
            "complete": complete,
            "event_end_confirmations": event_end_confirmations,
            "missing_metadata_condition_ids": missing_metadata_condition_ids,
            "queue_funnel": queue_funnel,
            "compared_range": dict(trial.get("compared_range") or {}),
            "evaluation_account": deepcopy(dict(account))
            if isinstance(account, Mapping)
            else None,
            "built_at": checked_at,
        }
        with self._candidate_state_lock:
            current = self._candidate_queue_state
            if (
                current is None
                or current.get("version") != version
                or current.get("reservation_signature")
                != reservation_signature
            ):
                self._candidate_queue_state = state
                self._candidate_queue_funnel = dict(queue_funnel)
                self._candidate_queue_condition_ids = tuple(
                    queue_condition_ids
                )
            return self._candidate_queue_state or state

    def _candidate_rotation_entry(
        self, condition_id: str
    ) -> dict[str, object]:
        with self._candidate_state_lock:
            entry = self._candidate_rotation.get(condition_id)
        if isinstance(entry, Mapping):
            return dict(entry)
        return {"last_attempt_at": None, "failures": 0}

    def _candidate_rotation_due(
        self, condition_id: str, *, now: datetime
    ) -> bool:
        """Whether a tried market's failure backoff has elapsed (issue #157).

        The ladder stays 60/120/300 seconds per market and a success resets
        it; deterministic rejections and successes never wait.
        """

        entry = self._candidate_rotation_entry(condition_id)
        failures = entry.get("failures")
        last_attempt_at = _candidate_row_updated_at(
            {"updated_at": entry.get("last_attempt_at")}
        )
        if last_attempt_at is None:
            return True
        if not isinstance(failures, int) or failures <= 0:
            return True
        backoff = _CANDIDATE_MAINTENANCE_BACKOFF_SECONDS[
            min(failures - 1, len(_CANDIDATE_MAINTENANCE_BACKOFF_SECONDS) - 1)
        ]
        return now >= last_attempt_at + timedelta(seconds=float(backoff))

    def _select_candidate_batch(
        self,
        queue_state: Mapping[str, object],
        *,
        now: datetime,
    ) -> list[tuple[Mapping[str, object], str]]:
        """Pick the next exploration batch (issue #157 rotation order).

        Markets never tried come first in the base queue order (normal then
        backup); the rest follow by oldest attempt time with markets inside
        their failure backoff skipped.  The batch never exceeds
        ``_LP_CANDIDATE_BATCH_SIZE`` markets.
        """

        ordered: list[tuple[Mapping[str, object], str]] = []
        seen: set[str] = set()
        for queue in ("queue_normal", "queue_backup"):
            for candidate in queue_state.get(queue) or ():
                if not isinstance(candidate, Mapping):
                    continue
                condition_id = str(
                    candidate.get("condition_id") or ""
                ).strip()
                if not condition_id or condition_id in seen:
                    continue
                seen.add(condition_id)
                ordered.append((candidate, condition_id))
        untried: list[tuple[Mapping[str, object], str]] = []
        due: list[tuple[datetime, Mapping[str, object], str]] = []
        with self._candidate_state_lock:
            rotation = deepcopy(self._candidate_rotation)
        for candidate, condition_id in ordered:
            entry = rotation.get(condition_id)
            last_attempt_at = (
                _candidate_row_updated_at(
                    {"updated_at": (entry or {}).get("last_attempt_at")}
                )
                if isinstance(entry, Mapping)
                else None
            )
            if last_attempt_at is None:
                untried.append((candidate, condition_id))
                continue
            if not self._candidate_rotation_due(condition_id, now=now):
                continue
            due.append((last_attempt_at, candidate, condition_id))
        due.sort(key=lambda item: item[0])
        batch = [*untried, *[(candidate, cid) for _, candidate, cid in due]]
        return batch[:_LP_CANDIDATE_BATCH_SIZE]

    def _renew_batch_shared_facts(
        self,
        queue_state: MutableMapping[str, object],
        condition_ids: tuple[str, ...],
        *,
        stop_event: threading.Event | None,
    ) -> Mapping[str, object] | None:
        """Renew expired shared facts for one batch (issue #143 repair 2).

        The shared facts (metadata+fees, reward catalog, account) come from
        the cached queue state, but a rolling batch runs long after that
        build, so every batch re-checks their age on its own clock and
        renews each expired class once — targeted at that batch's
        conditions only.  A failed renewal keeps the previous facts and is
        not retried within the batch.
        """

        directions_by_condition = cast(
            dict[str, list[Mapping[str, object]]],
            queue_state["directions_by_condition"],
        )
        metadata_by_condition = cast(
            dict[str, Mapping[str, object]],
            queue_state["metadata_by_condition"],
        )
        reward_market_by_condition = cast(
            dict[str, Mapping[str, object]],
            queue_state["reward_market_by_condition"],
        )
        evaluation_account = queue_state.get("evaluation_account")
        evaluation_account = (
            deepcopy(dict(evaluation_account))
            if isinstance(evaluation_account, Mapping)
            else None
        )
        now = self._now()
        fresh_metadata: dict[str, Mapping[str, object]] = {}
        stale_metadata_ids = _lp_metadata_stale_conditions(
            directions_by_condition, condition_ids, now
        )
        if stale_metadata_ids:
            reader = getattr(self.exchange, "lp_market_metadata_fresh", None)
            if not callable(reader):
                reader = getattr(self.exchange, "lp_market_metadata", None)
            raw_metadata: object = None
            if callable(reader):
                try:
                    raw_metadata = reader(
                        stale_metadata_ids, stop_event=stop_event
                    )
                except TypeError:
                    try:
                        raw_metadata = reader(stale_metadata_ids)
                    except Exception:
                        raw_metadata = None
                except Exception:
                    raw_metadata = None
            metadata_rows = (
                raw_metadata if isinstance(raw_metadata, Mapping) else {}
            )
            for condition_id in stale_metadata_ids:
                row = metadata_rows.get(condition_id)
                if not isinstance(row, Mapping):
                    continue
                observed_at = self._now()
                if _candidate_source_expired(
                    row.get("metadata_checked_at"), observed_at
                ) or _candidate_source_expired(
                    row.get("fees_checked_at"), observed_at
                ):
                    continue
                fresh_metadata[condition_id] = row
            metadata_by_condition.update(fresh_metadata)

        fresh_rewards: dict[str, Mapping[str, object]] = {}
        fresh_reward_stamps: dict[str, object] = {}
        stale_reward_ids = _lp_reward_stale_conditions(
            directions_by_condition, condition_ids, now
        )
        if stale_reward_ids:
            catalog_reader = getattr(self.exchange, "lp_reward_catalog", None)
            raw_reward: object = None
            if callable(catalog_reader):
                try:
                    raw_reward = catalog_reader(
                        condition_ids=stale_reward_ids, stop_event=stop_event
                    )
                except TypeError:
                    try:
                        raw_reward = catalog_reader(
                            condition_ids=stale_reward_ids
                        )
                    except Exception:
                        raw_reward = None
                except Exception:
                    raw_reward = None
            raw_rows = (
                raw_reward.get("markets")
                if isinstance(raw_reward, Mapping)
                else None
            )
            reward_rows_by_id: dict[str, Mapping[str, object]] = {}
            if isinstance(raw_rows, Mapping):
                reward_rows_by_id = {
                    str(key): value
                    for key, value in raw_rows.items()
                    if isinstance(value, Mapping)
                }
            elif isinstance(raw_rows, (list, tuple)):
                for row in raw_rows:
                    if isinstance(row, Mapping):
                        reward_rows_by_id.setdefault(
                            str(row.get("condition_id") or ""), row
                        )
            observed_at = self._now()
            for condition_id in stale_reward_ids:
                row = reward_rows_by_id.get(condition_id)
                if (
                    not isinstance(row, Mapping)
                    or row.get("state") == "unknown"
                ):
                    continue
                reward_stamp = row.get("reward_checked_at")
                if reward_stamp is None and isinstance(raw_reward, Mapping):
                    reward_stamp = raw_reward.get("checked_at")
                if _candidate_source_expired(reward_stamp, observed_at):
                    continue
                fresh_rewards[condition_id] = row
                fresh_reward_stamps[condition_id] = reward_stamp

        account_reader = getattr(
            self.exchange, "lp_account_snapshot_shared", None
        )
        if not callable(account_reader):
            account_reader = getattr(self.exchange, "lp_account_snapshot", None)
        if evaluation_account is None or _candidate_source_expired(
            evaluation_account.get("checked_at"), now
        ):
            if callable(account_reader):
                try:
                    refreshed_account = account_reader()
                except Exception:
                    refreshed_account = None
                if (
                    isinstance(refreshed_account, Mapping)
                    and refreshed_account.get("authenticated") is True
                    and not _candidate_source_expired(
                        refreshed_account.get("checked_at"), self._now()
                    )
                ):
                    evaluation_account = deepcopy(dict(refreshed_account))
        queue_state["evaluation_account"] = (
            deepcopy(dict(evaluation_account))
            if isinstance(evaluation_account, Mapping)
            else None
        )

        renewed_ids = tuple(dict.fromkeys((*fresh_metadata, *fresh_rewards)))
        for condition_id in renewed_ids:
            market_meta = (
                fresh_metadata[condition_id]
                if condition_id in fresh_metadata
                else metadata_by_condition.get(condition_id)
            )
            reward_row = (
                fresh_rewards[condition_id]
                if condition_id in fresh_rewards
                else reward_market_by_condition.get(condition_id)
            )
            if (
                not isinstance(market_meta, Mapping)
                or not isinstance(reward_row, Mapping)
            ):
                continue
            reward_deadline = self._reward_guidance_deadline(reward_row)
            rebuilt: list[Mapping[str, object]] = []
            for direction in directions_by_condition.get(condition_id, ()):
                if not isinstance(direction, Mapping):
                    continue
                market = direction.get("market")
                if not isinstance(market, Mapping):
                    continue
                outcome = str(market.get("outcome") or "").upper()
                token_id = str(market.get("token_id") or "").strip()
                if outcome not in {"YES", "NO"} or not token_id:
                    continue
                rebuilt.append(
                    _lp_direction_fact(
                        market_meta,
                        reward_row,
                        condition_id=condition_id,
                        token_id=token_id,
                        outcome=outcome,
                        reward_checked_at=(
                            fresh_reward_stamps[condition_id]
                            if condition_id in fresh_reward_stamps
                            else direction.get("reward_checked_at")
                        ),
                        reward_guidance_deadline=reward_deadline,
                        event_end_confirmation=direction.get(
                            "event_end_confirmation"
                        ),
                        history_summary=direction.get("history_summary"),
                        account=evaluation_account,
                    )
                )
            if rebuilt:
                directions_by_condition[condition_id] = rebuilt
        return evaluation_account

    def _candidate_pool_record_success(
        self,
        condition_id: str,
        row: Mapping[str, object],
        *,
        judged_at: datetime,
        facts: Mapping[str, object] | None = None,
    ) -> bool:
        """Store one successful estimate in the pool (issue #157).

        One-line write protection: the new result wins only when its
        judgment time is at or after the stored row's ``updated_at`` — a
        late-arriving older evaluation never overwrites a newer one.  A
        success also resets the market's rotation failure ladder.
        """

        stored = _pool_published_value(dict(row))
        stored["updated_at"] = _iso(judged_at)
        stored["expires_at"] = _iso(
            judged_at
            + timedelta(seconds=float(LP_CANDIDATE_VALIDITY_SECONDS))
        )
        stored["refresh_failed"] = False
        with self._candidate_state_lock:
            existing = self._candidate_pool.get(condition_id)
            if existing is not None:
                existing_updated = _candidate_row_updated_at(existing)
                if existing_updated is not None and existing_updated > judged_at:
                    return False
            self._candidate_pool[condition_id] = stored
            if facts is not None:
                self._candidate_qualification_facts[condition_id] = deepcopy(
                    dict(facts)
                )
            entry = self._candidate_rotation.get(condition_id)
            rotation = dict(entry) if isinstance(entry, Mapping) else {}
            rotation["last_attempt_at"] = _iso(judged_at)
            rotation["failures"] = 0
            self._candidate_rotation[condition_id] = rotation
        return True

    def _candidate_pool_record_failure(
        self, condition_id: str, *, attempted_at: datetime
    ) -> None:
        """Keep the stored row on a failed re-estimate (issue #157).

        The row keeps its values until its original ``expires_at``, is
        marked ``refresh_failed``, and its ``updated_at`` never moves; the
        market's rotation failure ladder advances (60/120/300s).  A failure
        judged before the stored row's ``updated_at`` is a late arrival: it
        neither mislabels the newer row nor advances the ladder (issue #157
        review R4).
        """

        with self._candidate_state_lock:
            existing = self._candidate_pool.get(condition_id)
            if existing is not None:
                existing_updated = _candidate_row_updated_at(existing)
                if (
                    existing_updated is not None
                    and existing_updated > attempted_at
                ):
                    return
                existing["refresh_failed"] = True
            entry = self._candidate_rotation.get(condition_id)
            rotation = dict(entry) if isinstance(entry, Mapping) else {}
            rotation["last_attempt_at"] = _iso(attempted_at)
            failures = rotation.get("failures")
            rotation["failures"] = (
                failures + 1 if isinstance(failures, int) else 1
            )
            self._candidate_rotation[condition_id] = rotation

    def _candidate_pool_record_rejection(
        self, condition_id: str, *, judged_at: datetime
    ) -> None:
        """Remove a deterministically rejected market from the pool and put
        it back at the rotation tail (issue #157).

        A rejection judged before the stored row's ``updated_at`` is a late
        arrival: the newer row keeps its place in the pool (issue #157
        review R4)."""

        with self._candidate_state_lock:
            existing = self._candidate_pool.get(condition_id)
            if existing is not None:
                existing_updated = _candidate_row_updated_at(existing)
                if (
                    existing_updated is not None
                    and existing_updated > judged_at
                ):
                    return
            self._candidate_pool.pop(condition_id, None)
            entry = self._candidate_rotation.get(condition_id)
            rotation = dict(entry) if isinstance(entry, Mapping) else {}
            rotation["last_attempt_at"] = _iso(judged_at)
            rotation["failures"] = 0
            self._candidate_rotation[condition_id] = rotation

    def _candidate_pool_prune_expired_locked(
        self, now: datetime
    ) -> list[str]:
        """Evict expired pool rows with their qualification facts (R5).

        Called with ``_candidate_state_lock`` held from the publication
        path: every publish drops the rows the current clock has expired so
        the in-memory pool, its persisted payload, and a restart restore
        cannot grow without bound.  Reads stay read-only and never mutate
        the pool.  Returns the evicted condition ids.
        """

        expired = [
            condition_id
            for condition_id, row in self._candidate_pool.items()
            if _candidate_pool_row_expired(row, now)
        ]
        for condition_id in expired:
            self._candidate_pool.pop(condition_id, None)
            self._candidate_qualification_facts.pop(condition_id, None)
        return expired

    def _save_candidate_pool(self, *, force: bool = False) -> None:
        """Persist the pool with the five-second save throttle (issue #157).

        The payload carries the pool rows with their original timestamps,
        the rotation schedule, and the event-end confirmations.  The save
        arbiter keeps the newest ``scan_started_at``, which for pool saves
        is the publish time.
        """

        writer = getattr(self.store, "lp_save_screening_snapshot", None)
        if not callable(writer):
            return
        now = self._now()
        with self._candidate_state_lock:
            last = self._candidate_pool_last_saved_at
            if not force and last is not None and Decimal(
                str((now - last).total_seconds())
            ) < _LP_CANDIDATE_SNAPSHOT_SAVE_MIN_INTERVAL_SECONDS:
                return
            snapshot = deepcopy(self._candidate_snapshot)
            payload = {
                "pool_version": 2,
                "scan_started_at": _iso(now),
                "state": "ready" if self._candidate_pool else "unknown",
                "complete": True,
                "pool": deepcopy(self._candidate_pool),
                "rotation": deepcopy(self._candidate_rotation),
                "event_end_confirmations": deepcopy(
                    self._candidate_snapshot.get(
                        "event_end_confirmations", {}
                    )
                ),
                "last_attempt_at": snapshot.get("last_attempt_at"),
                "checked_at": snapshot.get("checked_at"),
                "last_success_at": snapshot.get("last_success_at"),
            }
            self._candidate_pool_last_saved_at = now
        try:
            writer(payload)
        except Exception:
            with self._candidate_state_lock:
                self._candidate_pool_last_saved_at = None

    def _finish_candidate_scan(
        self,
        *,
        attempted_at: datetime | None = None,
        checked_at: datetime | None = None,
        success: bool = False,
        scanning: bool = False,
        complete: bool | None = None,
        state: str | None = None,
        notes: Mapping[str, object] | None = None,
        missing_book_token_ids: Sequence[str] | None = None,
    ) -> dict[str, object]:
        """Publish the rolling pool incrementally and persist it (issue #157).

        Shared by the exploration and maintenance paths: the pool rows were
        already rewritten one by one, so this only refreshes the published
        metadata, reprojects the snapshot, and saves through the throttled
        pool writer.
        """

        now = self._now()
        attempted = attempted_at or now
        with self._candidate_state_lock:
            # Issue #157 review R5: a publication first evicts every row the
            # current clock has expired, together with its qualification
            # facts, before the refreshed metadata is published and saved.
            self._candidate_pool_prune_expired_locked(now)
            snapshot = deepcopy(self._candidate_snapshot)
            snapshot["scanning"] = scanning
            snapshot["last_attempt_at"] = attempted
            if checked_at is not None:
                snapshot["checked_at"] = checked_at
            if success:
                snapshot["last_success_at"] = checked_at or attempted
            if complete is not None:
                snapshot["complete"] = complete
            if state is not None:
                snapshot["state"] = state
            if notes:
                for key, value in notes.items():
                    snapshot[key] = deepcopy(value)
            # Issue #157 review R3/R7: a publication always carries its own
            # batch's semantics — a batch without a stop note or retention
            # reason clears any stale one instead of reporting it forever.
            snapshot["stop_reason"] = (
                deepcopy(notes.get("stop_reason")) if notes else None
            )
            snapshot["retention_reason"] = (
                deepcopy(notes.get("retention_reason")) if notes else None
            )
            if missing_book_token_ids is not None:
                snapshot["missing_book_token_ids"] = list(
                    dict.fromkeys(missing_book_token_ids)
                )
            self._candidate_snapshot = snapshot
        projection = self.candidate_snapshot()
        self._save_candidate_pool()
        return projection

    def refresh_candidates(
        self,
        *,
        stop_event: threading.Event | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        """Process one rolling exploration batch (issue #157).

        Each call picks the next batch from the base-filter queues — markets
        never tried come first in queue order, the rest rotate by oldest
        attempt — reads the whole batch's books in one call (at most ten
        markets / 20 tokens), qualifies every market in both directions on
        the batch's renewed shared facts, and publishes the batch's results
        into the pool incrementally.  There is no round window, market cap,
        or whole-table publication anymore; a concurrent call returns the
        current snapshot with ``scanning`` set.
        """

        del force  # force only skips the runtime scheduler's wait now.
        if not self._candidate_scan_lock.acquire(blocking=False):
            snapshot = self.candidate_snapshot()
            snapshot["scanning"] = True
            return snapshot
        try:
            attempted_at = self._now()
            with self._candidate_state_lock:
                self._candidate_attempted_at = attempted_at
                self._candidate_snapshot = {
                    **self._candidate_snapshot,
                    "scanning": True,
                    "last_attempt_at": attempted_at,
                }
            if stop_event is not None and stop_event.is_set():
                return self._finish_candidate_scan(
                    attempted_at=attempted_at,
                    notes={"stop_reason": "scan_cancelled"},
                )
            queue_state = self._candidate_queue_state_build(
                stop_event=stop_event
            )
            if queue_state is None:
                return self._finish_candidate_scan(
                    attempted_at=attempted_at,
                    notes={
                        "stop_reason": "preparation_pending",
                        "retention_reason": "catalog_preparation_pending",
                    },
                )
            batch = self._select_candidate_batch(
                queue_state, now=self._now()
            )
            if not batch:
                queue_total = (queue_state.get("queue_funnel") or {}).get(
                    "queue_total"
                )
                if queue_total == 0:
                    # A complete catalog whose base filter keeps nothing is
                    # a valid, honestly empty pool.
                    return self._finish_candidate_scan(
                        attempted_at=attempted_at,
                        complete=queue_state.get("complete") is True,
                        state=(
                            "ready"
                            if queue_state.get("complete") is True
                            else "incomplete"
                        ),
                    )
                return self._finish_candidate_scan(attempted_at=attempted_at)
            books_reader = getattr(self.exchange, "lp_order_books", None)
            batch_tokens: list[str] = []
            directions_by_condition = cast(
                dict[str, list[Mapping[str, object]]],
                queue_state["directions_by_condition"],
            )
            for _candidate, condition_id in batch:
                for direction in directions_by_condition.get(condition_id, ()):
                    if (
                        isinstance(direction, Mapping)
                        and isinstance(direction.get("market"), Mapping)
                        and str(
                            direction["market"].get("outcome") or ""
                        ).upper()
                        in {"YES", "NO"}
                    ):
                        market = direction["market"]
                        token_id = str(market.get("token_id") or "").strip()
                        if token_id and token_id not in batch_tokens:
                            batch_tokens.append(token_id)
            # Issue #143 decision 5 (kept for #157): without a usable
            # account fact the batch is not consumed at all — zero book
            # reads, the stored rows stay for read-only display, and the
            # next batch or a manual refresh retries.
            evaluation_account = queue_state.get("evaluation_account")
            if not isinstance(evaluation_account, Mapping) or (
                _candidate_source_expired(
                    evaluation_account.get("checked_at"), self._now()
                )
            ):
                account_reader = getattr(
                    self.exchange, "lp_account_snapshot_shared", None
                )
                if not callable(account_reader):
                    account_reader = getattr(
                        self.exchange, "lp_account_snapshot", None
                    )
                refreshed_account: object = None
                if callable(account_reader):
                    try:
                        refreshed_account = account_reader()
                    except Exception:
                        refreshed_account = None
                if (
                    isinstance(refreshed_account, Mapping)
                    and refreshed_account.get("authenticated") is True
                    and not _candidate_source_expired(
                        refreshed_account.get("checked_at"), self._now()
                    )
                ):
                    evaluation_account = refreshed_account
                    queue_state["evaluation_account"] = deepcopy(
                        dict(refreshed_account)
                    )
                else:
                    evaluation_account = None
                    return self._finish_candidate_scan(
                        attempted_at=attempted_at,
                        notes={
                            "stop_reason": "account_unavailable",
                            "retention_reason": "account_unavailable",
                        },
                    )
            try:
                candidate_books = (
                    books_reader(tuple(batch_tokens), stop_event=stop_event)
                    if callable(books_reader)
                    else {}
                )
            except Exception:
                candidate_books = {}
            evaluation_account = self._renew_batch_shared_facts(
                queue_state,
                tuple(dict.fromkeys(cid for _c, cid in batch)),
                stop_event=stop_event,
            )
            # Issue #157: the batch judgment time is captured before the
            # reservations read — the last read between the renewed facts
            # and publication — so a publication that interleaves here with
            # a later judgment time wins the one-line write protection.
            evaluation_now = self._now()
            reservations = self._candidate_reservations()
            totals = deepcopy(self._candidate_funnel_totals)
            queue_funnel = deepcopy(
                queue_state.get("queue_funnel") or {}
            )
            missing_book_token_ids: list[str] = []
            unknown_reasons: list[dict[str, object]] = []
            checked = passed = rejected = unknown = 0
            backup_read = 0
            for candidate, condition_id in batch:
                checked += 1
                if candidate.get("queue") == "backup":
                    backup_read += 1
                market_directions = [
                    direction
                    for direction in directions_by_condition.get(
                        condition_id, ()
                    )
                    if isinstance(direction, Mapping)
                    and isinstance(direction.get("market"), Mapping)
                    and str(direction["market"].get("outcome") or "").upper()
                    in {"YES", "NO"}
                ]
                direction_results: dict[str, dict[str, object]] = {}
                eligible_directions: list[dict[str, object]] = []
                qualified_directions: list[dict[str, object]] = []
                for direction in market_directions:
                    market = direction.get("market")
                    if not isinstance(market, Mapping):
                        continue
                    outcome = str(market.get("outcome") or "").upper()
                    token_id = str(market.get("token_id") or "").strip()
                    book = (
                        candidate_books.get(token_id)
                        if isinstance(candidate_books, Mapping)
                        else None
                    )
                    qualified_direction = {**dict(direction)}
                    if not isinstance(book, Mapping):
                        missing_book_token_ids.append(token_id)
                        evaluated: Mapping[str, object] = {
                            "state": "unknown",
                            "reason_codes": ["book_unknown"],
                            "guidance": None,
                        }
                    else:
                        qualified_direction["book"] = deepcopy(dict(book))
                        evaluated = evaluate_lp_entry(
                            qualified_direction,
                            account=evaluation_account or {},
                            now=evaluation_now,
                            reservations=reservations,
                            candidate=True,
                        )
                    qualified_directions.append(qualified_direction)
                    result = {
                        "token_id": token_id,
                        "outcome": outcome,
                        "state": evaluated.get("state", "unknown"),
                        "eligible": evaluated.get("state") == "eligible",
                        "reason_codes": list(
                            evaluated.get("reason_codes", ())
                        ),
                        "guidance": evaluated.get("guidance"),
                    }
                    if isinstance(result["guidance"], Mapping):
                        for field in (
                            "price",
                            "quantity",
                            "required_capital",
                            "estimated_exit_loss",
                            "estimated_exit_loss_ratio",
                            "checked_at",
                        ):
                            if field in result["guidance"]:
                                result[field] = result["guidance"][field]
                    direction_results[outcome] = result
                    if result["eligible"] is True:
                        eligible_directions.append(result)
                        if isinstance(result["guidance"], Mapping):
                            result["estimate"] = _lp_direction_estimate(
                                qualified_direction,
                                result["guidance"],
                                now=evaluation_now,
                            )

                def _direction_yield_key(
                    result: Mapping[str, object]
                ) -> tuple[Decimal, str]:
                    raw = _direction_estimate_raw(result)
                    return (
                        -(
                            raw
                            if raw is not None
                            else Decimal("-Infinity")
                        ),
                        str(result.get("token_id") or ""),
                    )

                selected_direction = (
                    min(eligible_directions, key=_direction_yield_key)
                    if eligible_directions
                    else None
                )
                row_state = (
                    "eligible"
                    if selected_direction is not None
                    else "unknown"
                    if any(
                        result.get("state") == "unknown"
                        for result in direction_results.values()
                    )
                    else "rejected"
                )
                if row_state == "eligible":
                    passed += 1
                elif row_state == "rejected":
                    rejected += 1
                else:
                    unknown += 1
                    unknown_code = next(
                        (
                            str(code)
                            for result in direction_results.values()
                            if result.get("state") == "unknown"
                            for code in (
                                result.get("reason_codes") or ()
                            )
                            if str(code)
                        ),
                        "candidate_unknown",
                    )
                    unknown_reasons.append(
                        {
                            "market_id": str(
                                candidate.get("market_id") or ""
                            ),
                            "condition_id": condition_id,
                            "code": unknown_code,
                        }
                    )
                if row_state == "rejected":
                    self._candidate_pool_record_rejection(
                        condition_id, judged_at=evaluation_now
                    )
                    continue
                if row_state == "unknown":
                    self._candidate_pool_record_failure(
                        condition_id, attempted_at=evaluation_now
                    )
                    continue
                row = dict(candidate)
                row["directions"] = direction_results
                row["selected_direction"] = selected_direction
                row["state"] = row_state
                # Issue 158: publish the review deadline on the row so the
                # dashboard's LP entry modal can pass it through unchanged.
                from .polymarket_lp_views import _next_review_at

                row["review_at"] = _iso(_next_review_at(evaluation_now))
                row["verification"] = (
                    "verified"
                    if direction_results
                    and all(
                        result.get("state") in {"eligible", "rejected"}
                        for result in direction_results.values()
                    )
                    else "partial"
                )
                if isinstance(selected_direction, Mapping):
                    guidance = selected_direction.get("guidance")
                    if isinstance(guidance, Mapping):
                        row["realtime_price"] = guidance.get("price")
                        row["realtime_capital"] = guidance.get(
                            "required_capital"
                        )
                        row["realtime_checked_at"] = guidance.get(
                            "checked_at"
                        )
                        row["estimated_exit_loss"] = guidance.get(
                            "estimated_exit_loss"
                        )
                        row["estimated_exit_loss_ratio"] = guidance.get(
                            "estimated_exit_loss_ratio"
                        )
                selected_estimate = (
                    selected_direction.get("estimate")
                    if isinstance(selected_direction, Mapping)
                    else None
                )
                _apply_row_estimate_fields(row, selected_estimate)
                self._candidate_pool_record_success(
                    condition_id,
                    row,
                    judged_at=evaluation_now,
                    facts={
                        "directions": deepcopy(qualified_directions),
                        "account": deepcopy(dict(evaluation_account))
                        if isinstance(evaluation_account, Mapping)
                        else None,
                        "reservations": deepcopy(reservations),
                        "checked_at": queue_state.get("built_at"),
                    },
                )
            with self._candidate_state_lock:
                funnel_totals = deepcopy(self._candidate_funnel_totals)
                funnel_totals["checked"] = (
                    int(funnel_totals.get("checked") or 0) + checked
                )
                funnel_totals["passed"] = (
                    int(funnel_totals.get("passed") or 0) + passed
                )
                funnel_totals["rejected"] = (
                    int(funnel_totals.get("rejected") or 0) + rejected
                )
                funnel_totals["unknown"] = (
                    int(funnel_totals.get("unknown") or 0) + unknown
                )
                funnel_totals["batches"] = (
                    int(funnel_totals.get("batches") or 0) + 1
                )
                funnel_totals["backup_read"] = (
                    int(funnel_totals.get("backup_read") or 0) + backup_read
                )
                self._candidate_funnel_totals = funnel_totals
                queue_funnel_state = deepcopy(self._candidate_queue_funnel)
                if unknown_reasons:
                    reasons = queue_funnel_state.get("reasons")
                    if (
                        isinstance(reasons, Mapping)
                        and isinstance(reasons.get("trial"), list)
                    ):
                        reasons["trial"].extend(deepcopy(unknown_reasons))
                    self._candidate_queue_funnel = queue_funnel_state
            self._publish_sample_targets(())
            return self._finish_candidate_scan(
                attempted_at=attempted_at,
                checked_at=evaluation_now,
                success=passed > 0,
                complete=queue_state.get("complete") is True,
                state="ready" if queue_state.get("complete") is True else "incomplete",
                notes={
                    "catalog_complete": queue_state.get(
                        "catalog_complete"
                    ) is True,
                    "missing_metadata_condition_ids": list(
                        queue_state.get("missing_metadata_condition_ids")
                        or ()
                    ),
                },
                missing_book_token_ids=missing_book_token_ids,
            )
        except Exception:
            # Issue #157 review R3: a mid-batch failure publishes honestly —
            # the stuck ``scanning`` flag is cleared, the pool rows stay for
            # read-only display, and the failure carries the retention
            # reason the whole-scan path used to publish.  The next batch
            # retries from the unchanged rotation.
            logger.exception("candidate_refresh_failed")
            return self._finish_candidate_scan(
                notes={"retention_reason": "candidate_refresh_failed"},
            )
        finally:
            self._candidate_scan_lock.release()

    def refresh_candidate_recommendations(
        self, *, stop_event: threading.Event | None = None
    ) -> dict[str, object]:
        """Refresh the displayed pool rows on one batch book read (issue #157).

        The maintenance thread keeps the issue-#146 trigger — the oldest
        source age across the displayed rows with a 30-second lead, plus
        the 60/120/300s failure backoff — and the whole-table batch read
        framework (one batched book read plus one account, metadata, and
        targeted reward read per due source class).  Publication rewrites
        pool rows one by one: a refreshed row re-enters with its new
        judgment time, a failed row keeps its values marked
        ``refresh_failed`` until its original expiry, and a deterministic
        rejection leaves the pool at once.
        """

        if not self._candidate_maintenance_lock.acquire(blocking=False):
            snapshot = self.candidate_snapshot()
            snapshot["scanning"] = True
            return snapshot
        try:
            now = self._now()
            with self._candidate_state_lock:
                failures = self._candidate_maintenance_failures
                last_finished_at = self._candidate_maintenance_last_finished_at
                cached_facts = deepcopy(self._candidate_qualification_facts)
                pool = deepcopy(self._candidate_pool)
            # Issue #157: maintenance renews the currently displayed top ten
            # valid pool rows (whole-table framework, pool publication).
            valid_rows = [
                row
                for row in pool.values()
                if isinstance(row, Mapping)
                and not _candidate_pool_row_expired(row, now)
            ]
            valid_rows.sort(key=_candidate_yield_sort_key)
            selected_rows = valid_rows[:_LP_CANDIDATE_BATCH_SIZE]
            if not selected_rows:
                return self.candidate_snapshot()

            def live_directions(
                cached: Mapping[str, object],
            ) -> list[Mapping[str, object]]:
                return [
                    direction
                    for direction in cached.get("directions", ())
                    if isinstance(direction, Mapping)
                    and isinstance(direction.get("market"), Mapping)
                    and str(direction["market"].get("outcome") or "").upper()
                    in {"YES", "NO"}
                ]

            # Rows without cached qualification facts cannot be refreshed
            # this round: they keep their published values and rank behind
            # every refreshed row.
            row_facts: list[
                tuple[Mapping[str, object], Mapping[str, object] | None]
            ] = []
            for row in selected_rows:
                condition_id = str(row.get("condition_id") or "").strip()
                cached = cached_facts.get(condition_id)
                row_facts.append(
                    (row, cached if isinstance(cached, Mapping) else None)
                )
            refreshable = [
                (row, cached) for row, cached in row_facts if cached is not None
            ]
            if not refreshable:
                return self.candidate_snapshot()

            def row_account(cached: Mapping[str, object]) -> Mapping[str, object]:
                raw_account = cached.get("account")
                return raw_account if isinstance(raw_account, Mapping) else {}

            account_due = any(
                _candidate_source_due(row_account(cached).get("checked_at"), now)
                for _row, cached in refreshable
            )
            reward_due = any(
                _candidate_source_due(direction.get("reward_checked_at"), now)
                for _row, cached in refreshable
                for direction in live_directions(cached)
            )
            metadata_due = any(
                isinstance(direction.get("market"), Mapping)
                and (
                    _candidate_source_due(
                        direction["market"].get("metadata_checked_at"), now
                    )
                    or _candidate_source_due(
                        direction["market"].get("fees_checked_at"), now
                    )
                )
                for _row, cached in refreshable
                for direction in live_directions(cached)
            )
            token_ids = tuple(
                dict.fromkeys(
                    token
                    for _row, cached in refreshable
                    for token in (
                        str(direction["market"].get("token_id") or "").strip()
                        for direction in live_directions(cached)
                    )
                    if token
                )
            )
            book_due = any(
                _candidate_source_due(
                    direction.get("book", {}).get("received_at")
                    if isinstance(direction.get("book"), Mapping)
                    else None,
                    now,
                )
                for _row, cached in refreshable
                for direction in live_directions(cached)
            )
            source_times: list[datetime] = []
            source_expired = False
            for _row, cached in refreshable:
                times, expired = _candidate_head_source_times(cached, now)
                source_times.extend(times)
                source_expired = source_expired or expired
            # Issue #146 + #138: fire on the oldest source age across every
            # published row (30-second lead) or any already-expired source.
            lead_due = bool(source_times) and min(source_times) + timedelta(
                seconds=float(LP_RECOMMENDATION_REFRESH_LEAD_SECONDS)
            ) <= now
            if failures > 0 and last_finished_at is not None:
                # Issue #146: a failed maintenance attempt suppresses further
                # reads until its backoff window (60/120/300s, counted from
                # the attempt's finish) has elapsed.
                backoff = _CANDIDATE_MAINTENANCE_BACKOFF_SECONDS[
                    min(failures - 1, 2)
                ]
                if (
                    Decimal(str((now - last_finished_at).total_seconds()))
                    < backoff
                ):
                    return self.candidate_snapshot()
            if not lead_due and not source_expired:
                return self.candidate_snapshot()
            if stop_event is not None and stop_event.is_set():
                return self.candidate_snapshot()

            # Issue #146: expose how long each source read took so the
            # operator can see which source drove a refresh.
            read_seconds: dict[str, float] = {
                "account": 0.0,
                "metadata": 0.0,
                "reward": 0.0,
                "books": 0.0,
            }
            account_error: str | None = None
            account: Mapping[str, object] = {}
            if account_due:
                account_reader = getattr(
                    self.exchange, "lp_account_snapshot_shared", None
                )
                if not callable(account_reader):
                    account_reader = getattr(
                        self.exchange, "lp_account_snapshot", None
                    )
                read_started_at = self._now()
                try:
                    refreshed_account = (
                        account_reader() if callable(account_reader) else None
                    )
                except Exception:
                    refreshed_account = None
                read_seconds["account"] = float(
                    (self._now() - read_started_at).total_seconds()
                )
                if (
                    isinstance(refreshed_account, Mapping)
                    and not _candidate_source_expired(
                        refreshed_account.get("checked_at"), self._now()
                    )
                ):
                    account = deepcopy(dict(refreshed_account))
                else:
                    account_error = "account_unknown"
            else:
                # Every row shares the account-wide fact; reuse the freshest
                # cached receipt (not due means at least one is inside its
                # freshness window).
                for _row, cached in refreshable:
                    candidate_account = row_account(cached)
                    if candidate_account.get("checked_at"):
                        account = deepcopy(dict(candidate_account))
                        break
                if not account:
                    account_error = "account_unknown"

            refresh_conditions = tuple(
                dict.fromkeys(
                    str(row.get("condition_id") or "").strip()
                    for row, _cached in refreshable
                    if str(row.get("condition_id") or "").strip()
                )
            )
            metadata_error: str | None = None
            refreshed_metadata: dict[str, Mapping[str, object]] = {}
            if metadata_due:
                metadata_reader = getattr(
                    self.exchange, "lp_market_metadata_fresh", None
                )
                if not callable(metadata_reader):
                    metadata_reader = getattr(
                        self.exchange, "lp_market_metadata", None
                    )
                read_started_at = self._now()
                try:
                    raw_metadata = (
                        metadata_reader(refresh_conditions, stop_event=stop_event)
                        if callable(metadata_reader)
                        else None
                    )
                except TypeError:
                    try:
                        raw_metadata = (
                            metadata_reader(refresh_conditions)
                            if callable(metadata_reader)
                            else None
                        )
                    except Exception:
                        raw_metadata = None
                except Exception:
                    raw_metadata = None
                metadata_rows = (
                    raw_metadata.get("markets")
                    if isinstance(raw_metadata, Mapping)
                    and isinstance(raw_metadata.get("markets"), Mapping)
                    else raw_metadata
                )
                if isinstance(metadata_rows, Mapping):
                    for condition_id in refresh_conditions:
                        candidate_market = metadata_rows.get(condition_id)
                        if isinstance(candidate_market, Mapping):
                            refreshed_metadata[condition_id] = candidate_market
                metadata_checked_now = self._now()
                if not refreshed_metadata:
                    metadata_error = "market_metadata_unknown"
                else:
                    for market_row in refreshed_metadata.values():
                        if _candidate_source_expired(
                            market_row.get("metadata_checked_at"),
                            metadata_checked_now,
                        ):
                            metadata_error = "market_metadata_unknown"
                        elif _candidate_source_expired(
                            market_row.get("fees_checked_at"),
                            metadata_checked_now,
                        ):
                            metadata_error = "market_fees_unknown"
                read_seconds["metadata"] = float(
                    (self._now() - read_started_at).total_seconds()
                )

            reward_error: str | None = None
            refreshed_rewards: dict[str, Mapping[str, object]] = {}
            raw_reward: Mapping[str, object] | None = None
            if reward_due:
                reward_reader = getattr(self.exchange, "lp_reward_catalog", None)
                read_started_at = self._now()
                try:
                    raw_reward = (
                        reward_reader(
                            condition_ids=refresh_conditions,
                            stop_event=stop_event,
                        )
                        if callable(reward_reader)
                        else None
                    )
                except TypeError:
                    try:
                        raw_reward = (
                            reward_reader(condition_ids=refresh_conditions)
                            if callable(reward_reader)
                            else None
                        )
                    except Exception:
                        raw_reward = None
                except Exception:
                    raw_reward = None
                reward_rows = (
                    raw_reward.get("markets")
                    if isinstance(raw_reward, Mapping)
                    else None
                )
                if isinstance(reward_rows, Mapping):
                    for condition_id in refresh_conditions:
                        candidate_reward = reward_rows.get(condition_id)
                        if isinstance(candidate_reward, Mapping) and (
                            candidate_reward.get("state") != "unknown"
                        ):
                            refreshed_rewards[condition_id] = candidate_reward
                elif isinstance(reward_rows, (list, tuple)):
                    for reward_row in reward_rows:
                        if not isinstance(reward_row, Mapping):
                            continue
                        row_condition = str(reward_row.get("condition_id") or "")
                        if row_condition in refresh_conditions and reward_row.get(
                            "state"
                        ) != "unknown":
                            refreshed_rewards[row_condition] = reward_row
                for condition_id in refresh_conditions:
                    refreshed_reward_row = refreshed_rewards.get(condition_id)
                    reward_checked_at = (
                        refreshed_reward_row.get("reward_checked_at")
                        if isinstance(refreshed_reward_row, Mapping)
                        else None
                    )
                    if reward_checked_at is None and isinstance(raw_reward, Mapping):
                        reward_checked_at = raw_reward.get("checked_at")
                    if refreshed_reward_row is None or _candidate_source_expired(
                        reward_checked_at, self._now()
                    ):
                        reward_error = "reward_unknown"
                        break
                read_seconds["reward"] = float(
                    (self._now() - read_started_at).total_seconds()
                )

            books: Mapping[str, object] = {}
            books_failed = False
            missing_book_token_ids: list[str] = []
            if book_due and token_ids:
                books_reader = getattr(self.exchange, "lp_order_books", None)
                read_started_at = self._now()
                try:
                    raw_books = (
                        books_reader(token_ids, stop_event=stop_event)
                        if callable(books_reader)
                        else None
                    )
                except TypeError:
                    try:
                        raw_books = (
                            books_reader(token_ids)
                            if callable(books_reader)
                            else None
                        )
                    except Exception:
                        raw_books = None
                except Exception:
                    raw_books = None
                read_seconds["books"] = float(
                    (self._now() - read_started_at).total_seconds()
                )
                if isinstance(raw_books, Mapping) and raw_books:
                    books = raw_books
                else:
                    books_failed = True

            reservations = self._candidate_reservations()
            # One whole-round gate, per the issue-138 round-2 decision: an
            # account, metadata, reward, or entire-book failure keeps every
            # published value this cycle instead of half-refreshed tables.
            round_failed = (
                account_error is not None
                or metadata_error is not None
                or reward_error is not None
                or books_failed
            )

            def evaluate_row(
                row: Mapping[str, object],
                cached: Mapping[str, object],
                evaluation_now: datetime,
            ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
                """Re-qualify one published row with this round's sources.

                Returns ``(new_row, facts)`` when the row was refreshed, or
                ``(None, None)`` when the row must keep its published values
                (all of its books are missing this round).
                """

                condition_id = str(row.get("condition_id") or "").strip()
                directions = live_directions(cached)
                updated_directions: list[Mapping[str, object]] = []
                direction_results: dict[str, dict[str, object]] = {}
                eligible_directions: list[dict[str, object]] = []
                row_missing_books = 0
                row_directions = 0
                for direction in directions:
                    market_value = direction.get("market")
                    if not isinstance(market_value, Mapping):
                        continue
                    row_directions += 1
                    maintenance_direction = deepcopy(dict(direction))
                    market = dict(market_value)
                    outcome = str(market.get("outcome") or "").upper()
                    token_id = str(market.get("token_id") or "").strip()
                    refreshed_market = refreshed_metadata.get(condition_id)
                    if (
                        metadata_due
                        and isinstance(refreshed_market, Mapping)
                        and metadata_error is None
                    ):
                        market = {
                            **dict(refreshed_market),
                            "condition_id": condition_id,
                            "token_id": token_id,
                            "outcome": outcome,
                        }
                        if market.get("reward_min_size") is not None:
                            market["_metadata_reward_min_size"] = market.get(
                                "reward_min_size"
                            )
                        if market.get("reward_max_spread") is not None:
                            market["_metadata_reward_max_spread"] = market.get(
                                "reward_max_spread"
                            )
                    if (
                        (
                            metadata_due
                            and isinstance(refreshed_market, Mapping)
                            and metadata_error is None
                        )
                        or (
                            reward_due
                            and condition_id in refreshed_rewards
                            and reward_error is None
                        )
                    ):
                        reward_source: dict[str, object] = {}
                        # Reuse cached reward rules only while their reward
                        # read remains valid.  Once reward facts expire, a
                        # response that omits the rules cannot renew those
                        # old values.
                        if not reward_due:
                            cached_minimum = market_value.get(
                                "_reward_catalog_min_size"
                            )
                            cached_spread = _maybe_decimal(
                                market_value.get("_reward_catalog_max_spread")
                            )
                            if cached_minimum is not None:
                                reward_source["rewards_min_size"] = cached_minimum
                            if cached_spread is not None:
                                reward_source["rewards_max_spread"] = cached_spread
                        refreshed_reward_row = refreshed_rewards.get(condition_id)
                        if isinstance(refreshed_reward_row, Mapping):
                            reward_source.update(dict(refreshed_reward_row))
                        market_for_rules = dict(market)
                        if not metadata_due:
                            # Normalized fields can have come from the prior
                            # reward read.  Reuse only rules proven to
                            # originate in the still-fresh metadata source.
                            market_for_rules.pop("reward_min_size", None)
                            market_for_rules.pop("reward_max_spread", None)
                            metadata_minimum = market.get(
                                "_metadata_reward_min_size"
                            )
                            metadata_spread = market.get(
                                "_metadata_reward_max_spread"
                            )
                            if metadata_minimum is not None:
                                market_for_rules["reward_min_size"] = (
                                    metadata_minimum
                                )
                            if metadata_spread is not None:
                                market_for_rules["reward_max_spread"] = (
                                    metadata_spread
                                )
                        reward_minimum, reward_spread = _lp_reward_terms(
                            market_for_rules, reward_source
                        )
                        market["reward_min_size"] = reward_minimum
                        market["reward_max_spread"] = reward_spread
                    maintenance_direction["market"] = market
                    refreshed_reward_row = refreshed_rewards.get(condition_id)
                    if (
                        reward_due
                        and isinstance(refreshed_reward_row, Mapping)
                        and reward_error is None
                    ):
                        maintenance_direction["reward_active"] = (
                            refreshed_reward_row.get("reward_active")
                        )
                        maintenance_direction["daily_pool_usd"] = (
                            refreshed_reward_row.get("daily_pool_usd")
                        )
                        maintenance_direction["reward_checked_at"] = (
                            refreshed_reward_row.get("reward_checked_at")
                            or raw_reward.get("checked_at")
                            if isinstance(raw_reward, Mapping)
                            else refreshed_reward_row.get("checked_at")
                        )
                        maintenance_direction[
                            "reward_guidance_deadline"
                        ] = self._reward_guidance_deadline(refreshed_reward_row)
                    book = (
                        books.get(token_id)
                        if book_due
                        else maintenance_direction.get("book")
                    )
                    if book_due and isinstance(book, Mapping):
                        maintenance_direction["book"] = dict(book)
                    if not isinstance(book, Mapping):
                        row_missing_books += 1
                        missing_book_token_ids.append(token_id)
                        evaluated: Mapping[str, object] = {
                            "state": "unknown",
                            "reason_codes": ["book_unknown"],
                            "guidance": None,
                        }
                    else:
                        if _candidate_source_expired(
                            market.get("metadata_checked_at"), evaluation_now
                        ) or _candidate_source_expired(
                            market.get("fees_checked_at"), evaluation_now
                        ):
                            evaluated = {
                                "state": "unknown",
                                "reason_codes": ["market_metadata_stale"],
                                "guidance": None,
                            }
                        elif _candidate_source_expired(
                            maintenance_direction.get("reward_checked_at"),
                            evaluation_now,
                        ):
                            evaluated = {
                                "state": "unknown",
                                "reason_codes": ["reward_data_stale"],
                                "guidance": None,
                            }
                        else:
                            evaluated = evaluate_lp_entry(
                                maintenance_direction,
                                account=account,
                                now=evaluation_now,
                                reservations=reservations,
                                candidate=True,
                            )
                    updated_directions.append(maintenance_direction)
                    result: dict[str, object] = {
                        "token_id": token_id,
                        "outcome": outcome,
                        "state": evaluated.get("state", "unknown"),
                        "eligible": evaluated.get("state") == "eligible",
                        "reason_codes": list(evaluated.get("reason_codes", ())),
                        "guidance": evaluated.get("guidance"),
                    }
                    if isinstance(result["guidance"], Mapping):
                        for field in (
                            "price",
                            "quantity",
                            "required_capital",
                            "estimated_exit_loss",
                            "estimated_exit_loss_ratio",
                            "checked_at",
                        ):
                            if field in result["guidance"]:
                                result[field] = result["guidance"][field]
                    if result["eligible"] is True and isinstance(
                        result["guidance"], Mapping
                    ):
                        # Issue #138 round 2: every eligible direction
                        # carries its 5% target-share estimate for the
                        # yield comparison.
                        result["estimate"] = _lp_direction_estimate(
                            maintenance_direction,
                            result["guidance"],
                            now=evaluation_now,
                        )
                    direction_results[outcome] = result
                    if result["eligible"] is True:
                        eligible_directions.append(result)

                if row_directions == 0 or row_missing_books == row_directions:
                    # None of the row's directions could be re-read: the row
                    # keeps its published values this round.
                    return None, None

                def _row_yield_key(
                    result: Mapping[str, object],
                ) -> tuple[Decimal, str]:
                    estimate = result.get("estimate")
                    raw = (
                        estimate.get("yield_pct_per_hour")
                        if isinstance(estimate, Mapping)
                        and estimate.get("state") == "known"
                        else None
                    )
                    parsed = _maybe_decimal(raw)
                    return (
                        -(parsed if parsed is not None else Decimal("-Infinity")),
                        str(result.get("token_id") or ""),
                    )

                selected_direction = (
                    min(eligible_directions, key=_row_yield_key)
                    if eligible_directions
                    else None
                )
                new_row = dict(row)
                new_row["directions"] = direction_results
                new_row["selected_direction"] = selected_direction
                new_row["state"] = (
                    "eligible"
                    if selected_direction is not None
                    else "unknown"
                    if any(
                        result.get("state") == "unknown"
                        for result in direction_results.values()
                    )
                    else "rejected"
                )
                # Issue 158: keep the review deadline current on re-qualified
                # rows (same projection as the scan publication).
                from .polymarket_lp_views import _next_review_at

                new_row["review_at"] = _iso(_next_review_at(evaluation_now))
                new_row["verification"] = (
                    "verified"
                    if direction_results
                    and all(
                        result.get("state") in {"eligible", "rejected"}
                        for result in direction_results.values()
                    )
                    else "partial"
                )
                for key in (
                    "realtime_price",
                    "realtime_capital",
                    "realtime_checked_at",
                ):
                    new_row.pop(key, None)
                if isinstance(selected_direction, Mapping):
                    guidance = selected_direction.get("guidance")
                    if isinstance(guidance, Mapping):
                        new_row["realtime_price"] = guidance.get("price")
                        new_row["realtime_capital"] = guidance.get(
                            "required_capital"
                        )
                        new_row["realtime_checked_at"] = guidance.get("checked_at")
                selected_estimate = (
                    selected_direction.get("estimate")
                    if isinstance(selected_direction, Mapping)
                    else None
                )
                _apply_row_estimate_fields(new_row, selected_estimate)
                facts = {
                    "directions": deepcopy(updated_directions),
                    "account": deepcopy(account)
                    if isinstance(account, Mapping)
                    else None,
                    "reservations": deepcopy(reservations),
                    "checked_at": self._now(),
                }
                return new_row, facts

            evaluation_now = self._now()
            # Issue #157: publication rewrites pool rows one by one.  A
            # refreshed row re-enters with its new judgment time, a failed
            # row keeps its values marked refresh_failed until its original
            # expiry, and a deterministically rejected row leaves the pool
            # immediately.
            round_failure_reason = (
                account_error
                or metadata_error
                or reward_error
                or ("book_unknown" if books_failed else None)
            )
            refreshed_any = False
            for row, cached in row_facts:
                condition_id = str(row.get("condition_id") or "").strip()
                new_row: dict[str, object] | None = None
                facts: dict[str, object] | None = None
                if cached is not None and not round_failed:
                    new_row, facts = evaluate_row(row, cached, evaluation_now)
                if new_row is None or new_row.get("state") == "unknown":
                    # Whole-round failure, missing cached facts, or a row
                    # whose re-read could not produce a verdict: keep the
                    # stored values until their original expiry and mark
                    # the row refresh_failed.
                    self._candidate_pool_record_failure(
                        condition_id, attempted_at=evaluation_now
                    )
                    continue
                if new_row.get("state") == "rejected":
                    self._candidate_pool_record_rejection(
                        condition_id, judged_at=evaluation_now
                    )
                    continue
                # Issue #157 review R4: the write protection may reject this
                # write because a concurrent newer publication already holds
                # the line.  The row was refreshed either way, so a
                # guard-rejected success is not a round failure and must not
                # advance the 60/120/300s maintenance backoff.
                self._candidate_pool_record_success(
                    condition_id,
                    new_row,
                    judged_at=evaluation_now,
                    facts=facts,
                )
                refreshed_any = True
            # Issue #146: the maintenance attempt's backoff bookkeeping is
            # applied together with its publication.
            with self._candidate_state_lock:
                maintenance_finished_at = self._now()
                self._candidate_maintenance_last_finished_at = (
                    maintenance_finished_at
                )
                if refreshed_any:
                    self._candidate_maintenance_failures = 0
                else:
                    self._candidate_maintenance_failures += 1
                failures_now = self._candidate_maintenance_failures
            notes: dict[str, object] = {
                "maintenance_consecutive_failures": failures_now,
            }
            if failures_now > 0:
                next_backoff = _CANDIDATE_MAINTENANCE_BACKOFF_SECONDS[
                    min(failures_now - 1, 2)
                ]
                notes["maintenance_next_attempt_at"] = _iso(
                    maintenance_finished_at
                    + timedelta(seconds=float(next_backoff))
                )
            else:
                notes["maintenance_next_attempt_at"] = None
            notes["maintenance_diagnostics"] = {
                "started_at": _iso(now),
                "finished_at": _iso(evaluation_now),
                "read_seconds": read_seconds,
            }
            return self._finish_candidate_scan(
                attempted_at=now,
                checked_at=evaluation_now,
                success=refreshed_any,
                notes=notes,
                missing_book_token_ids=missing_book_token_ids,
            )
        finally:
            self._candidate_maintenance_lock.release()

    def refresh_competition_cache(
        self, stop_event: threading.Event | None = None
    ) -> dict[str, object]:
        """Refresh the in-memory competition cache (issue #157).

        Public entry point for the dedicated competition thread: candidate
        batch paths only read the cache and never block on this read.  The
        optional ``stop_event`` flows into the underlying read so a stopping
        runtime cancels an in-flight page pull cooperatively.
        """

        return self._refresh_competition(stop_event)

    def _refresh_competition(
        self, stop_event: threading.Event | None
    ) -> dict[str, object]:
        """Pull official competitiveness into the in-process cache.

        #181: every full round is also persisted to the store (explicit
        zeros included) so later rounds can fall back to the last known
        value for markets the current round did not pull.  A failed read
        keeps the previous values (the reader already merges them) and a
        failed persistence only logs — neither blocks the candidate funnel.
        """

        reader = getattr(self.exchange, "lp_market_competitiveness", None)
        if callable(reader):
            with self._competition_lock:
                previous = dict(self._competition_state)
            try:
                result = reader(
                    stop_event=stop_event,
                    previous=previous.get("competitiveness", {}),
                    # Issue #177: resume the walk where the last round
                    # stopped instead of restarting from page one.
                    start_cursor=previous.get("next_start_cursor"),
                )
            except Exception:
                result = None
            if isinstance(result, Mapping):
                stored = {
                    "state": result.get("state", "unknown"),
                    "complete": result.get("complete") is True,
                    "round_checked_at": result.get("round_checked_at"),
                    "competitiveness": dict(
                        result.get("competitiveness") or {}
                    ),
                    "not_updated": list(result.get("not_updated") or []),
                    "next_start_cursor": result.get("resume_cursor"),
                }
                with self._competition_lock:
                    self._competition_state = stored
                self._persist_competition(stored)
        with self._competition_lock:
            return deepcopy(self._competition_state)

    def _persist_competition(self, stored: Mapping[str, object]) -> None:
        """Write one full round's competitiveness to the store (#181)."""

        upsert = getattr(self.store, "lp_competitiveness_upsert", None)
        if not callable(upsert):
            return
        raw_map = stored.get("competitiveness")
        if not isinstance(raw_map, Mapping):
            return
        entries: list[tuple[str, Decimal, datetime]] = []
        for condition_id, value in raw_map.items():
            if not isinstance(condition_id, str) or not condition_id.strip():
                continue
            if isinstance(value, tuple) and len(value) == 2:
                entry_value, checked_at = value
            elif isinstance(value, Mapping):
                entry_value = value.get("value")
                checked_at = value.get("checked_at")
            else:
                continue
            if not isinstance(entry_value, Decimal):
                continue
            if not isinstance(checked_at, datetime) or checked_at.tzinfo is None:
                continue
            entries.append((condition_id, entry_value, checked_at))
        if not entries:
            return
        try:
            upsert(entries)
        except Exception:
            logger.warning(
                "lp competition persistence failed: rows=%d", len(entries)
            )

    def _competition_entries(self) -> dict[str, object]:
        """Project the competition map for the trial-candidate view (#181).

        Fresh cache entries (inside the one-hour competition freshness gate)
        win with ``source="fresh"``; everything else — a missing cache or an
        entry older than the gate — falls back to the last persisted round
        with ``source="store"`` and the store's own checked_at.  Conditions
        present in neither source are left out entirely: the view owns the
        missing-data exclusion and there is no cold-start exemption.
        """

        # Lazy import: polymarket_lp_views imports this module at load time.
        from .polymarket_lp_views import LP_COMPETITION_MAX_AGE

        state = self._competition_state
        round_checked_at = state.get("round_checked_at")
        now = self.clock()
        entries: dict[str, object] = {}
        fresh_conditions: set[str] = set()
        raw_map = state.get("competitiveness")
        if isinstance(raw_map, Mapping):
            for condition_id, value in raw_map.items():
                if not isinstance(condition_id, str):
                    continue
                if isinstance(value, tuple) and len(value) == 2:
                    entry_value, checked_at = value
                elif isinstance(value, Mapping):
                    entry_value = value.get("value")
                    checked_at = value.get("checked_at")
                else:
                    continue
                fresh = False
                if isinstance(checked_at, datetime) and checked_at.tzinfo is not None:
                    age = (now - checked_at).total_seconds()
                    fresh = 0 <= age < LP_COMPETITION_MAX_AGE.total_seconds()
                if not fresh:
                    continue
                fresh_conditions.add(condition_id)
                entries[condition_id] = {
                    "value": entry_value,
                    "checked_at": checked_at,
                    "updated": (
                        checked_at == round_checked_at
                        if isinstance(checked_at, datetime)
                        and isinstance(round_checked_at, datetime)
                        else None
                    ),
                    "source": "fresh",
                }
        store_map = self._competition_store_map()
        if store_map:
            for condition_id, (value, checked_at) in store_map.items():
                if condition_id in fresh_conditions:
                    continue
                entries[condition_id] = {
                    "value": value,
                    "checked_at": checked_at,
                    "updated": None,
                    "source": "store",
                }
        return entries

    def _competition_store_map(self) -> dict[str, tuple[Decimal, datetime]]:
        """Read the persisted competition map, tolerating store failures."""

        reader = getattr(self.store, "lp_competitiveness_map", None)
        if not callable(reader):
            return {}
        try:
            store_map = reader()
        except Exception:
            logger.warning("lp competition store read failed")
            return {}
        return store_map if isinstance(store_map, dict) else {}

    def _candidate_reservations(self) -> tuple[dict[str, object], ...]:
        reservations: list[dict[str, object]] = []
        for session in self.store.lp_active_sessions():
            order_id = str(session.get("entry_order_id") or "").strip()
            if not order_id:
                order_id = f"lp-session:{session.get('session_id', '')}"
            price = _maybe_decimal(session.get("price"))
            quantity = _maybe_decimal(session.get("quantity"))
            filled = _maybe_decimal(session.get("buy_filled_quantity")) or Decimal("0")
            amount = (
                price * max(Decimal("0"), quantity - filled)
                if price is not None and quantity is not None
                else None
            )
            reservations.append({"order_id": order_id, "amount": amount})

        control_reader = getattr(self.store, "n_leg_control", None)
        batch_reader = getattr(self.store, "n_leg_batch", None)
        unknown = {"order_id": "n-leg-reservation:unknown", "amount": None}
        if not callable(control_reader) or not callable(batch_reader):
            reservations.append(unknown)
            return tuple(reservations)
        try:
            control = control_reader()
        except Exception:
            reservations.append(unknown)
            return tuple(reservations)
        if not isinstance(control, Mapping):
            reservations.append(unknown)
            return tuple(reservations)
        batch_id = control.get("active_batch_id")
        if batch_id is None:
            return tuple(reservations)
        if not isinstance(batch_id, str) or not batch_id.strip():
            reservations.append(unknown)
            return tuple(reservations)
        try:
            batch = batch_reader(batch_id)
        except Exception:
            reservations.append(unknown)
            return tuple(reservations)
        if (
            not isinstance(batch, Mapping)
            or batch.get("execution_batch_id") != batch_id
            or batch.get("state") == "INCIDENT_ACKNOWLEDGED"
            or bool(batch.get("unresolved_conflicts"))
            or batch.get("capital_exposure_unknown") is True
        ):
            reservations.append(unknown)
            return tuple(reservations)

        legs = batch.get("legs")
        receipts = batch.get("receipts")
        rows = batch.get("reservations")
        if (
            not isinstance(legs, Sequence)
            or isinstance(legs, (str, bytes))
            or not legs
            or not isinstance(receipts, Mapping)
            or not isinstance(rows, Sequence)
            or isinstance(rows, (str, bytes))
            or not rows
        ):
            reservations.append(unknown)
            return tuple(reservations)

        terminal_states = {"FILLED", "REJECTED", "CANCELLED"}
        for leg in legs:
            if not isinstance(leg, Mapping):
                reservations.append(unknown)
                return tuple(reservations)
            if any(
                not isinstance(leg.get(field), str) or not str(leg[field]).strip()
                for field in ("client_order_id", "venue_id", "account_id")
            ):
                reservations.append(unknown)
                return tuple(reservations)
            receipt = leg.get("receipt")
            if not isinstance(receipt, Mapping) or receipt.get("state") not in terminal_states:
                reservations.append(unknown)
                return tuple(reservations)
            receipt_id = receipt.get("receipt_id")
            if (
                not isinstance(receipt_id, str)
                or receipts.get(receipt_id) != receipt
                or receipt.get("execution_batch_id") != batch_id
                or any(
                    receipt.get(field) != leg.get(field)
                    for field in ("client_order_id", "venue_id", "account_id")
                )
            ):
                reservations.append(unknown)
                return tuple(reservations)

        native_key = ("polymarket", "catalog-v2", "usd-micro")
        native_remaining: int | None = None
        native_found = False
        for row in rows:
            if not isinstance(row, Mapping):
                reservations.append(unknown)
                return tuple(reservations)
            key_values = tuple(
                row.get(field)
                for field in ("venue_id", "account_id", "settlement_asset_id")
            )
            if any(not isinstance(value, str) or not value for value in key_values):
                reservations.append(unknown)
                return tuple(reservations)
            key = cast(tuple[str, str, str], key_values)
            if key == native_key:
                remaining = row.get("remaining_units")
                if (
                    native_found
                    or type(remaining) is not int
                    or remaining < 0
                ):
                    reservations.append(unknown)
                    return tuple(reservations)
                native_found = True
                native_remaining = remaining
        if native_found:
            reservations.append(
                {
                    "order_id": f"n-leg-reservation:{batch_id}:polymarket:usd-micro",
                    "amount": Decimal(cast(int, native_remaining)) / Decimal("1000000"),
                }
            )
        return tuple(reservations)

    @staticmethod
    def _reward_guidance_deadline(
        reward_market: Mapping[str, object],
    ) -> datetime | None:
        pool = _maybe_decimal(reward_market.get("daily_pool_usd"))
        if pool is None or pool <= 0:
            return None
        from .polymarket_trading import (
            LP_REWARD_ASSET_USD_ADDRESSES,
            _reward_date,
        )

        end_dates: list[date] = []
        config_fields = (
            ("combined_reward_configs",)
            if "combined_reward_configs" in reward_market
            else ("native_reward_configs", "sponsored_reward_configs")
        )
        for field in config_fields:
            configs = reward_market.get(field)
            if not isinstance(configs, Sequence) or isinstance(configs, (str, bytes)):
                continue
            for config in configs:
                if not isinstance(config, Mapping):
                    continue
                asset = _text(config.get("asset_address"))
                rate = _maybe_decimal(config.get("rate_per_day"))
                if (
                    asset is None
                    or asset.casefold() not in LP_REWARD_ASSET_USD_ADDRESSES
                    or rate is None
                    or rate <= 0
                ):
                    continue
                end_date = _reward_date(config.get("end_date"))
                if end_date is None:
                    return None
                end_dates.append(end_date)
        if not end_dates:
            return None
        return datetime.combine(min(end_dates) + timedelta(days=1), time.min, UTC)

    @staticmethod
    def _observe_event_end(
        condition_id: str,
        market: Mapping[str, object],
        *,
        existing: object,
        observed_at: datetime,
    ) -> dict[str, object] | None:
        if market.get("event_ended") is not True or market.get("event_finished_at") is not None:
            return None
        game_id = str(market.get("game_id") or "").strip()
        event_id = str(market.get("event_id") or "").strip()
        has_timing = any(
            market.get(field) is not None
            for field in ("game_start_time", "event_start_time")
        )
        if not game_id and not has_timing:
            return None

        if isinstance(existing, Mapping):
            previous_game_id = str(existing.get("game_id") or "").strip()
            previous_event_id = str(existing.get("event_id") or "").strip()
            matches = bool(game_id or event_id)
            if game_id:
                matches = matches and previous_game_id == game_id
            if event_id:
                matches = matches and previous_event_id == event_id
            try:
                _timestamp(
                    existing.get("confirmed_end_at"),
                    name="confirmed_end_at",
                )
            except ValueError:
                matches = False
            if matches:
                return dict(existing)

        return {
            "condition_id": condition_id,
            "game_id": game_id or None,
            "event_id": event_id or None,
            "confirmed_end_at": _iso(observed_at),
        }

    def _read_candidate_snapshot(
        self, identity: Mapping[str, object], *, now: datetime
    ) -> dict[str, object]:
        """Read and qualify one candidate market through selected adapters only."""

        condition_id = str(identity.get("condition_id") or "").strip()
        token_id = str(identity.get("token_id") or "").strip()
        outcome = str(identity.get("outcome") or "").upper()
        if not condition_id or not token_id or outcome not in {"YES", "NO"}:
            raise ValueError("candidate_identity_unknown")
        account_reader = getattr(self.exchange, "lp_account_snapshot", None)
        metadata_reader = getattr(self.exchange, "lp_market_metadata_fresh", None)
        reward_reader = getattr(self.exchange, "lp_reward_catalog", None)
        books_reader = getattr(self.exchange, "lp_order_books", None)
        if not all(
            callable(reader)
            for reader in (account_reader, metadata_reader, reward_reader, books_reader)
        ):
            raise ValueError("candidate_readers_unavailable")
        try:
            account = account_reader()
        except Exception as exc:
            raise ValueError("account_unknown") from exc
        if not isinstance(account, Mapping):
            raise ValueError("account_unknown")
        try:
            try:
                raw_metadata = metadata_reader((condition_id,), stop_event=None)
            except TypeError:
                raw_metadata = metadata_reader((condition_id,))
        except Exception as exc:
            raise ValueError("candidate_market_unknown") from exc
        metadata = (
            raw_metadata.get("markets")
            if isinstance(raw_metadata, Mapping)
            and isinstance(raw_metadata.get("markets"), Mapping)
            else raw_metadata
        )
        market_metadata = (
            metadata.get(condition_id)
            if isinstance(metadata, Mapping)
            else None
        )
        if not isinstance(market_metadata, Mapping):
            raise ValueError("candidate_market_unknown")
        try:
            try:
                raw_catalog = reward_reader(
                    condition_ids=(condition_id,), stop_event=None
                )
            except TypeError:
                raw_catalog = reward_reader(condition_ids=(condition_id,))
        except Exception as exc:
            raise ValueError("candidate_reward_unknown") from exc
        if not isinstance(raw_catalog, Mapping):
            raise ValueError("candidate_reward_unknown")
        reward_market = next(
            (
                row
                for row in _items(raw_catalog.get("markets"))
                if isinstance(row, Mapping)
                and str(row.get("condition_id") or "") == condition_id
            ),
            None,
        )
        if not isinstance(reward_market, Mapping):
            raise ValueError("candidate_reward_unknown")
        if reward_market.get("state") == "unknown":
            reasons = reward_market.get("reason_codes")
            reason = (
                str(reasons[0])
                if isinstance(reasons, Sequence)
                and not isinstance(reasons, (str, bytes))
                and reasons
                else "candidate_reward_unknown"
            )
            raise ValueError(reason)
        raw_outcomes = market_metadata.get("outcomes")
        if not isinstance(raw_outcomes, Mapping):
            raise ValueError("candidate_market_rules_unknown")
        selected_outcome = next(
            (
                value
                for value in raw_outcomes.values()
                if isinstance(value, Mapping)
                and str(value.get("token_id") or "") == token_id
                and str(value.get("label") or "").upper() == outcome
            ),
            None,
        )
        if not isinstance(selected_outcome, Mapping):
            raise ValueError("candidate_identity_mismatch")
        reward_minimum, reward_spread = _lp_reward_terms(
            market_metadata, reward_market
        )
        if reward_minimum is None or reward_spread is None:
            raise ValueError("candidate_market_rules_unknown")
        market = {
            **dict(market_metadata),
            "condition_id": condition_id,
            "token_id": token_id,
            "outcome": outcome,
            "reward_min_size": reward_minimum,
            "reward_max_spread": reward_spread,
        }
        # Issue #143: the preview re-check reads both token books of the
        # selected market in one call, separate from the scan's round budget.
        outcome_token_ids = [
            str(value.get("token_id") or "").strip()
            for value in raw_outcomes.values()
            if isinstance(value, Mapping) and str(value.get("token_id") or "").strip()
        ]
        preview_token_ids = tuple(dict.fromkeys(outcome_token_ids)) or (token_id,)
        try:
            try:
                raw_books = books_reader(preview_token_ids, stop_event=None)
            except TypeError:
                raw_books = books_reader(preview_token_ids)
        except Exception as exc:
            raise ValueError("book_unknown") from exc
        book = raw_books.get(token_id) if isinstance(raw_books, Mapping) else None
        if not isinstance(book, Mapping):
            raise ValueError("book_unknown")
        direction = {
            "market": market,
            "book": book,
            "reward_active": reward_market.get("reward_active"),
            "daily_pool_usd": reward_market.get("daily_pool_usd"),
            "reward_checked_at": reward_market.get(
                "reward_checked_at", reward_market.get("checked_at", raw_catalog.get("checked_at"))
            ),
            "reward_guidance_deadline": self._reward_guidance_deadline(reward_market),
        }
        evaluation_now = self._now()
        evaluated = evaluate_lp_entry(
            direction,
            account=account,
            now=evaluation_now,
            reservations=self._candidate_reservations(),
            candidate=True,
        )
        if evaluated.get("state") != "eligible":
            reasons = evaluated.get("reason_codes")
            reason = (
                str(reasons[0])
                if isinstance(reasons, Sequence)
                and not isinstance(reasons, (str, bytes))
                and reasons
                else "candidate_not_eligible"
            )
            raise ValueError(reason)
        return {
            "account": dict(account),
            "market": market,
            "book": dict(book),
            "candidate_evaluation": evaluated,
        }


    def _fresh_candidate_row(
        self,
        identity: Mapping[str, object],
        snapshot: Mapping[str, object],
        *,
        now: datetime,
    ) -> Mapping[str, object]:
        """Return the same shared candidate qualification used by refresh."""

        evaluated = snapshot.get("candidate_evaluation")
        if not isinstance(evaluated, Mapping) or evaluated.get("state") != "eligible":
            snapshot = self._read_candidate_snapshot(identity, now=now)
            evaluated = snapshot.get("candidate_evaluation")
        guidance = evaluated.get("guidance") if isinstance(evaluated, Mapping) else None
        if not isinstance(guidance, Mapping) or not _lp_guidance_is_usable(guidance):
            raise ValueError("candidate_not_eligible")
        return {
            **dict(identity),
            **dict(guidance),
            "state": "eligible",
            "reason_codes": list(evaluated.get("reason_codes", ()))
            if isinstance(evaluated, Mapping)
            else [],
        }

    def _mutation_allowed(self, action: str = "submit") -> bool:
        owner = self.owner_lock
        if owner is not None:
            held = getattr(owner, "held", None)
            if held is False:
                return False
        guard = self._mutation_guard
        if guard is None:
            return True
        try:
            return guard(action) is True
        except TypeError:
            try:
                return guard() is True
            except Exception:
                return False
        except Exception:
            return False

    def _require_mutation(self) -> None:
        if not self._mutation_allowed("submit"):
            raise _MutationBlocked("mutation_blocked")

    @staticmethod
    def _action_key(session_id: str, role: str, order_id: str | None = None) -> str:
        suffix = f":{order_id}" if order_id else ""
        return f"{session_id}:{role}{suffix}"

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock_invalid")
        return value.astimezone(UTC)

    def preview(self, request: Mapping[str, object]) -> dict[str, object]:
        """Fetch independent exchange facts and return a short-lived preview."""

        try:
            normalized = self._normalize_request(request)
            snapshot = self._read_snapshot(normalized)
            facts = self._validate_snapshot(
                normalized, snapshot, now=self._now(),
                reservations=self._candidate_reservations(),
            )
        except ValueError as exc:
            return {"state": "rejected", "reason": str(exc)}
        expires_at = self._now() + timedelta(seconds=PREVIEW_TTL_SECONDS)
        payload = {**normalized, "preflight": facts}
        preview_id = self.store.create_preview(
            payload,
            expires_at=_iso(expires_at),
            created_at=_iso(self._now()),
        )
        return {
            "state": "previewed",
            "preview_id": preview_id,
            "expires_at": _iso(expires_at),
            "request": normalized,
            "preflight": facts,
            "queue_protection_estimate": self._queue_protection_preview_estimate(
                normalized, snapshot
            ),
        }

    def preview_candidate(self, candidate: Mapping[str, object]) -> dict[str, object]:
        """Build a fresh candidate preview from the server-observed best bid."""

        if not isinstance(candidate, Mapping):
            return {"state": "rejected", "reason": "candidate_invalid"}
        identity: dict[str, object] = {}
        for key in ("market_id", "condition_id", "token_id"):
            value = _text(candidate.get(key))
            if value is None:
                return {"state": "rejected", "reason": f"{key}_invalid"}
            identity[key] = value
        outcome = _text(candidate.get("outcome"))
        if outcome is None or outcome.upper() not in {"YES", "NO"}:
            return {"state": "rejected", "reason": "outcome_invalid"}
        identity["outcome"] = outcome.upper()

        try:
            now = self._now()
            snapshot = self._read_candidate_snapshot(identity, now=now)
            now = self._now()
            from .polymarket_lp_views import _next_review_at

            review_at = _next_review_at(now)
            expiration_for_review(review_at, now=now)
            eligible = self._fresh_candidate_row(identity, snapshot, now=now)
            price = _decimal(eligible.get("price"), "candidate_price")
            quantity = _decimal(eligible.get("quantity"), "candidate_quantity")
            request = self._normalize_request(
                {
                    **identity,
                    "price": price,
                    "quantity": quantity,
                    "review_at": review_at,
                    "candidate_policy": "best_bid_minimum",
                }
            )
            facts = self._validate_snapshot(
                request, snapshot, now=now,
                reservations=self._candidate_reservations(),
            )
        except ValueError as exc:
            return {"state": "rejected", "reason": str(exc)}

        expires_at = now + timedelta(seconds=PREVIEW_TTL_SECONDS)
        preview_id = self.store.create_preview(
            {**request, "preflight": facts},
            expires_at=_iso(expires_at),
            created_at=_iso(now),
        )
        return {
            "state": "previewed",
            "preview_id": preview_id,
            "expires_at": _iso(expires_at),
            "request": request,
            "preflight": facts,
            "queue_protection_estimate": self._queue_protection_preview_estimate(
                request, snapshot
            ),
        }

    @staticmethod
    def _session_augment_own_order_ids(session: Mapping[str, object]) -> list[str]:
        """Own resting order ids an augment re-check must tolerate."""

        ids: list[str] = []
        entry_id = str(session.get("entry_order_id") or "")
        if entry_id:
            ids.append(entry_id)
        for value in _items(session.get("augment_order_ids")):
            augment_id = str(value or "")
            if augment_id:
                ids.append(augment_id)
        return ids

    def _augment_merge_estimate(
        self,
        request: Mapping[str, object],
        snapshot: Mapping[str, object],
    ) -> dict[str, object]:
        """Issue 158 merged share estimate for one augment.

        ``front`` is the same-price level minus the session's own remaining
        BUY size; the projected ratio is front/(front+own+quantity) so the
        new order's share is judged on the merged protection umbrella.
        """

        price = cast(Decimal, request["price"])
        quantity = cast(Decimal, request["quantity"])
        token_id = str(request["token_id"])
        level_total = self._queue_baseline_front(snapshot, price)
        own_remaining = self._own_queue_remaining(
            snapshot, token_id=token_id, price=price
        )
        if own_remaining is None:
            return {
                "baseline_front": None,
                "own_remaining": None,
                "projected_ratio": None,
            }
        front = level_total - own_remaining
        if front < 0:
            front = Decimal("0")
        denominator = front + own_remaining + quantity
        projected = (
            front / denominator if denominator > 0 else Decimal("0")
        )
        return {
            "baseline_front": front,
            "own_remaining": own_remaining,
            "projected_ratio": projected,
        }

    def _augment_session(
        self, session_id: str
    ) -> tuple[dict[str, object] | None, str | None]:
        """Load the augment target session or the rejection reason.

        Issue 166: an augment only lands on ``entry_open``.  Terminal states
        keep the ``session_not_active`` reason; every other non-terminal
        state maps through the shared table to its real reason, so the
        single-shot and two-phase augment paths reject with one voice.
        """

        session = self.store.lp_session(str(session_id))
        if session is None:
            return None, "session_not_found"
        state = str(session.get("state"))
        if state in {"complete", "entry_rejected"}:
            return None, "session_not_active"
        block = _LP_AUGMENT_STATE_BLOCKS.get(state)
        if block is not None:
            return None, block
        return session, None

    def augment_preview(self, request: Mapping[str, object]) -> dict[str, object]:
        """Build a short-lived augment preview for one price level.

        Issue 167: the optional ``price`` unlocks the level (default = the
        group price); the shared gate rejects a level that still carries an
        own resting BUY and a price above the snapshot's best bid.  The
        estimate projects the chosen level's fresh depth (the issue 158
        merged same-price basis is retired).
        """

        if not isinstance(request, Mapping):
            return {"state": "rejected", "reason": "request_invalid"}
        session_id = _text(request.get("session_id"))
        if session_id is None:
            return {"state": "rejected", "reason": "session_id_invalid"}
        quantity = _maybe_decimal(request.get("quantity"))
        if quantity is None or quantity <= 0:
            return {"state": "rejected", "reason": "quantity_invalid"}
        session, rejection = self._augment_session(session_id)
        if session is None:
            return {"state": "rejected", "reason": rejection or "session_not_active"}
        price = _maybe_decimal(session.get("price"))
        if request.get("price") is not None:
            requested_price = _maybe_decimal(request.get("price"))
            if requested_price is None or requested_price <= 0:
                return {"state": "rejected", "reason": "price_invalid"}
            price = requested_price
        if price is None or price <= 0:
            return {"state": "rejected", "reason": "entry_price_unknown"}
        identity: dict[str, object] = {}
        for key in ("market_id", "condition_id", "token_id"):
            value = _text(session.get(key))
            if value is None:
                return {"state": "rejected", "reason": f"{key}_invalid"}
            identity[key] = value
        outcome = _text(session.get("outcome"))
        if outcome is None or outcome.upper() not in {"YES", "NO"}:
            return {"state": "rejected", "reason": "outcome_invalid"}
        identity["outcome"] = outcome.upper()
        if session.get("review_at") is None:
            return {"state": "rejected", "reason": "review_at_unknown"}
        try:
            augment_request = self._normalize_request(
                {**identity, "price": price, "quantity": quantity,
                 "review_at": session.get("review_at")}
            )
            snapshot = self._read_snapshot(augment_request)
            # Issue 176: the validation clock is taken after the snapshot read
            # completes; received_at is stamped at read time, so a pre-read
            # clock yields a negative age and a deterministic stale rejection.
            now = self._now()
            price_rejection = self._augment_price_rejection(session, snapshot, price)
            if price_rejection is not None:
                return {"state": "rejected", "reason": price_rejection}
            facts = self._validate_snapshot(
                augment_request,
                snapshot,
                now=now,
                allowed_open_order_ids=self._session_augment_own_order_ids(session),
                reservations=self._candidate_reservations(),
            )
        except ValueError as exc:
            return {"state": "rejected", "reason": str(exc)}
        expires_at = now + timedelta(seconds=PREVIEW_TTL_SECONDS)
        payload = {**augment_request, "session_id": session_id, "preflight": facts}
        preview_id = self.store.create_preview(
            payload,
            expires_at=_iso(expires_at),
            created_at=_iso(now),
        )
        return {
            "state": "previewed",
            "preview_id": preview_id,
            "expires_at": _iso(expires_at),
            "request": {"session_id": session_id, "quantity": quantity},
            "price": price,
            "preflight": facts,
            "queue_protection_estimate": self._queue_protection_preview_estimate(
                augment_request, snapshot
            ),
        }

    def _augment_recorded_result(
        self, session_id: str, key: str
    ) -> dict[str, object] | None:
        """Return the durable result of a prior augment with the same key.

        Issue 158: idempotent replays resolve here before any preview read,
        fresh-facts re-check, or best-bid comparison can run again.
        """

        intent_key = self._action_key(session_id, f"augment-submit:{key}")
        for action in self.store.lp_actions(session_id):
            if str(action.get("action_key") or "") != intent_key:
                continue
            state = str(action.get("state") or "")
            order_id = str(action.get("order_id") or "")
            if state == "accepted":
                session = self.store.lp_session(session_id)
                result = (
                    self._status_payload(session)
                    if session is not None
                    else {"state": "none", "session_id": session_id}
                )
                result["augment_order_id"] = order_id
                return result
            if state == "rejected":
                return {
                    "state": "rejected",
                    "reason": "augment_submit_rejected",
                    "session_id": session_id,
                    "order_id": order_id,
                }
            return {
                "state": "needs_attention",
                "session_id": session_id,
                "reason": "augment_submit_unknown",
            }
        return None

    def augment(
        self,
        session_id: str,
        preview_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """Revalidate an augment preview, then submit exactly one more BUY."""

        key = (idempotency_key or "").strip()
        if not key:
            return {"state": "rejected", "reason": "idempotency_key_required"}
        with self._mutex:
            session, rejection = self._augment_session(str(session_id))
            if session is None:
                return {"state": "rejected", "reason": rejection or "session_not_active"}
            session_id_str = str(session["session_id"])
            recorded = self._augment_recorded_result(session_id_str, key)
            if recorded is not None:
                return recorded
            if not self._mutation_allowed("submit"):
                return {"state": "locked", "reason": "mutation_blocked"}
            preview = self.store.lp_preview(preview_id)
            if preview is None:
                return {"state": "rejected", "reason": "preview_not_found"}
            try:
                expires_at = _timestamp(preview["expires_at"], name="preview_expiry")
                if self._now() >= expires_at:
                    raise ValueError("preview_expired")
                if str(preview.get("session_id") or "") != session_id_str:
                    raise ValueError("preview_session_mismatch")
                request = self._normalize_request(preview)
                snapshot = self._read_snapshot(request)
                # Issue 176: validation clock taken after the read completes
                # (received_at stamps at read time — a pre-read clock goes
                # deterministically stale).
                now = self._now()
                # Issue 167: shared augment price gate on the fresh snapshot
                # (level-active first, then the best-bid ceiling).
                price_rejection = self._augment_price_rejection(
                    session, snapshot, cast(Decimal, request["price"])
                )
                if price_rejection is not None:
                    return {"state": "rejected", "reason": price_rejection}
                facts = self._validate_snapshot(
                    request,
                    snapshot,
                    now=now,
                    allowed_open_order_ids=self._session_augment_own_order_ids(session),
                    reservations=self._candidate_reservations(),
                )
                # Issue 158: same submit-time best-bid re-check as start.
                credential_preflight = preview.get("preflight")
                credential_best_bid = (
                    _maybe_decimal(credential_preflight.get("best_bid"))
                    if isinstance(credential_preflight, Mapping)
                    else None
                )
                if (
                    credential_best_bid is not None
                    and credential_best_bid != facts["best_bid"]
                ):
                    raise ValueError("best_bid_changed")
                expiration = expiration_for_review(
                    _timestamp(request["review_at"], name="review_at"), now=now
                )
            except ValueError as exc:
                return {"state": "rejected", "reason": str(exc)}
            try:
                self.store.consume_lp_preview(preview_id)
            except ValueError as exc:
                return {"state": "rejected", "reason": str(exc)}
            return self._augment_execute(
                session=session,
                session_id=session_id_str,
                request=request,
                snapshot=snapshot,
                now=now,
                key=key,
                expiration=expiration,
            )

    def _augment_execute(
        self,
        *,
        session: Mapping[str, object],
        session_id: str,
        request: Mapping[str, object],
        snapshot: Mapping[str, object],
        now: datetime,
        key: str,
        expiration: int,
    ) -> dict[str, object]:
        """Record the augment intent and submit exactly one more BUY.

        Shared tail of the two-phase ``augment`` (issue 158) and the
        single-shot ``submit_augment`` (issue 163): the caller has already
        validated fresh facts against the session's own resting orders;
        this helper owns the action rows, the exchange post, the appended
        order history, and the #152 baseline re-anchor.
        """

        price = cast(Decimal, request["price"])
        quantity = cast(Decimal, request["quantity"])
        action_base: dict[str, object] = {
            "role": "augment",
            "side": "BUY",
            "token_id": str(request["token_id"]),
            "idempotency_key": key,
            "price": price,
            "quantity": quantity,
            "expiration": expiration,
            "submit_requested_at": _iso(now),
        }
        intent_key = self._action_key(session_id, f"augment-submit:{key}")
        self.store.lp_upsert_action(
            session_id,
            intent_key,
            state="pending",
            payload={**action_base},
        )
        try:
            signed = self._create_limit(
                token_id=str(request["token_id"]),
                price=price,
                quantity=quantity,
                side="BUY",
                post_only=True,
                expiration=expiration,
            )
            response = self._post_limit(signed)
        except Exception as exc:
            self.store.lp_upsert_action(
                session_id,
                intent_key,
                state="unknown",
                payload={**action_base, "error": type(exc).__name__},
            )
            return {
                "state": "needs_attention",
                "session_id": session_id,
                "reason": "augment_submit_unknown",
                "error": type(exc).__name__,
            }
        accepted, order_id = self._order_response(response)
        if not accepted:
            self.store.lp_upsert_action(
                session_id,
                intent_key,
                state="rejected",
                payload={
                    **action_base,
                    "order_id": order_id or "",
                    "error": "augment_submit_rejected",
                },
            )
            return {
                "state": "rejected",
                "reason": "augment_submit_rejected",
                "session_id": session_id,
                "order_id": order_id or "",
            }
        if not order_id:
            self.store.lp_upsert_action(
                session_id,
                intent_key,
                state="unknown",
                payload={**action_base, "error": "accepted_without_order_id"},
            )
            return {
                "state": "needs_attention",
                "session_id": session_id,
                "reason": "augment_submit_unknown",
            }
        self.store.lp_upsert_action(
            session_id,
            self._action_key(session_id, f"augment-submit:{order_id}"),
            state="accepted",
            payload={
                **action_base,
                "order_id": order_id,
                "submit_receipt_at": _iso(self._now()),
            },
        )
        self.store.lp_upsert_action(
            session_id,
            intent_key,
            state="accepted",
            payload={**action_base, "order_id": order_id},
        )
        # The venue post can overlap an off-lock queue-protection cancel.
        # Re-read after the post so the session merge starts from the latest
        # durable A/B state instead of the pre-submit snapshot.
        session = self.store.lp_session(session_id) or session
        augment_ids = [
            str(value) for value in _items(session.get("augment_order_ids"))
        ]
        if order_id not in augment_ids:
            augment_ids.append(order_id)
        order_history = self._order_history(session)
        order_history[order_id] = {
            "order_id": order_id,
            "token_id": str(request["token_id"]),
            "side": "BUY",
            "status": str(_field(response, "status", "LIVE")).upper() or "LIVE",
            "price": price,
            "quantity": quantity,
            "expiration": expiration,
            "role": "augment",
        }
        # Issue 167: the augment registers its own price-level bucket — one
        # resting order per level, so the issue 158 same-price merged
        # re-anchor is retired for new orders (_augment_merge_estimate stays
        # only as a legacy-payload read helper).  A legacy scalar payload
        # wraps into its single entry bucket here, so the stored shape lands
        # in the v2 {version, data_failures, levels} form naturally.
        raw_protection = session.get("queue_protection")
        levels = _queue_protection_level_buckets(
            raw_protection,
            default_order_id=str(session.get("entry_order_id") or ""),
        )
        bucket: dict[str, object] = {
            "order_id": order_id,
            **self._queue_protection_baseline(request, snapshot),
            "baseline_version": 1,
            "threshold": LP_QUEUE_PROTECTION_THRESHOLD,
            "state": "registered",
            "notification_sent": False,
            "blocked_notified": False,
            "cancel_scope": "own_buys_at_level",
            "cancel_targets": [],
            "canceled_order_ids": [],
            "cancel_target_remaining": {},
            "canceled_remaining": None,
            "partially_filled_quantity": None,
            "reason_codes": [],
        }
        levels[format(price, "f")] = bucket
        protection_payload: dict[str, object] = {
            "version": 2,
            "data_failures": _queue_group_failures_int(
                raw_protection.get("data_failures")
                if isinstance(raw_protection, Mapping)
                else None
            ),
            "levels": levels,
        }
        # Issue 167: the group's ordered BUY ceiling grows by this augment so
        # the fill accounting never trips opening_quantity_exceeded.
        prior_group_quantity = _maybe_decimal(session.get("group_buy_quantity")) or (
            _maybe_decimal(session.get("quantity")) or Decimal("0")
        )
        session_patch = {
            "augment_order_ids": augment_ids,
            "augment_order_id": order_id,
            "augment_quantity": quantity,
            "group_buy_quantity": prior_group_quantity + quantity,
            "order_history": order_history,
        }
        merger = getattr(self.store, "lp_merge_queue_protection", None)
        if callable(merger):
            updated = merger(
                session_id,
                queue_protection=protection_payload,
                patch=session_patch,
            )
        else:
            updated = self.store.lp_update_session(
                session_id,
                patch={**session_patch, "queue_protection": protection_payload},
            )
        return self._status_payload(updated)

    def _session_price_level_active(
        self,
        session: Mapping[str, object],
        snapshot: Mapping[str, object],
        price: Decimal,
    ) -> bool:
        """Issue 167 D1-c: an own non-terminal BUY still rests at this price.

        Evidence sources mirror ``_session_augment_own_order_ids``: the
        durable order history first, then the snapshot receipts/account rows.
        An order whose status is unreadable counts as alive (never proven
        terminal); an order whose price is unreadable cannot be attributed
        to any level and does not block.
        """

        history = self._order_history(session)
        rows_by_id = self._queue_level_rows(snapshot)
        for order_id in self._session_augment_own_order_ids(session):
            record = history.get(order_id)
            row = rows_by_id.get(order_id)
            source = (
                record
                if isinstance(record, Mapping) and record.get("status")
                else row
            )
            if source is None:
                continue
            status = str(_field(source, "status", "") or "").upper()
            if status and status in TERMINAL_ORDER_STATES:
                continue
            order_price = _maybe_decimal(_field(source, "price"))
            if order_price is not None and order_price == price:
                return True
        return False

    def _augment_price_rejection(
        self,
        session: Mapping[str, object],
        snapshot: Mapping[str, object],
        price: Decimal,
    ) -> str | None:
        """Issue 167 D1-c/D2: shared augment price gate for both paths.

        Fixed order: a level with a resting own BUY rejects first (one
        resting order per price level), then the best-bid ceiling (a price
        above the same snapshot's top bid can never rest).  An empty bid
        side has no provable ceiling and rejects too.
        """

        if self._session_price_level_active(session, snapshot, price):
            return "price_level_active"
        book = snapshot.get("book")
        bids: list[tuple[Decimal, Decimal]] = []
        if isinstance(book, Mapping):
            try:
                bids = self._levels(book.get("bids"), "bids")
            except ValueError:
                bids = []
        best_bid = max((row_price for row_price, _ in bids), default=None)
        if best_bid is None or price > best_bid:
            return "price_above_best_bid"
        return None

    def submit_augment(
        self,
        session_id: str,
        quantity: str,
        idempotency_key: str | None = None,
        price: str | Decimal | None = None,
    ) -> dict[str, object]:
        """Issue 163: single-shot augment bound to one named session.

        The target session is explicit — the request can never land on
        "whatever session is currently active".  Replays resolve from the
        recorded action row before any fresh-facts read; the one snapshot
        read tolerates only this session's own resting orders, and the
        review deadline stays the session's original one (no re-timer).
        Issue 167: the optional ``price`` unlocks the augment price level
        (default = the group price); the shared gate rejects a level that
        still carries an own resting BUY and a price above the best bid.
        """

        key = (idempotency_key or "").strip()
        if not key:
            return {"state": "rejected", "reason": "idempotency_key_required"}
        with self._mutex:
            session, rejection = self._augment_session(str(session_id))
            if session is None:
                return {"state": "rejected", "reason": rejection or "session_not_active"}
            session_id_str = str(session["session_id"])
            recorded = self._augment_recorded_result(session_id_str, key)
            if recorded is not None:
                return recorded
            if not self._mutation_allowed("submit"):
                return {"state": "locked", "reason": "mutation_blocked"}
            quantity_d = _maybe_decimal(quantity)
            if quantity_d is None or quantity_d <= 0:
                return {"state": "rejected", "reason": "quantity_invalid"}
            price_d = _maybe_decimal(session.get("price"))
            if price is not None:
                requested_price = _maybe_decimal(price)
                if requested_price is None or requested_price <= 0:
                    return {"state": "rejected", "reason": "price_invalid"}
                price_d = requested_price
            if price_d is None or price_d <= 0:
                return {"state": "rejected", "reason": "entry_price_unknown"}
            identity: dict[str, object] = {}
            for identity_key in ("market_id", "condition_id", "token_id"):
                value = _text(session.get(identity_key))
                if value is None:
                    return {"state": "rejected", "reason": f"{identity_key}_invalid"}
                identity[identity_key] = value
            outcome = _text(session.get("outcome"))
            if outcome is None or outcome.upper() not in {"YES", "NO"}:
                return {"state": "rejected", "reason": "outcome_invalid"}
            identity["outcome"] = outcome.upper()
            if session.get("review_at") is None:
                return {"state": "rejected", "reason": "review_at_unknown"}
            try:
                request = self._normalize_request(
                    {**identity, "price": price_d, "quantity": quantity_d,
                     "review_at": session.get("review_at")}
                )
                snapshot = self._read_snapshot(request)
                # Issue 176: validation clock taken after the read completes
                # (received_at stamps at read time — a pre-read clock goes
                # deterministically stale).
                now = self._now()
                price_rejection = self._augment_price_rejection(
                    session, snapshot, price_d
                )
                if price_rejection is not None:
                    return {"state": "rejected", "reason": price_rejection}
                facts = self._validate_snapshot(
                    request,
                    snapshot,
                    now=now,
                    allowed_open_order_ids=self._session_augment_own_order_ids(session),
                    reservations=self._candidate_reservations(),
                )
                expiration = expiration_for_review(
                    _timestamp(request["review_at"], name="review_at"), now=now
                )
            except ValueError as exc:
                return {"state": "rejected", "reason": str(exc)}
            return self._augment_execute(
                session=session,
                session_id=session_id_str,
                request=request,
                snapshot=snapshot,
                now=now,
                key=key,
                expiration=expiration,
            )

    def _queue_baseline_front(
        self, snapshot: Mapping[str, object], price: Decimal
    ) -> Decimal:
        """Sum the price level's size in a validated book; a missing level is 0.

        Registration never rejects on the level: validation already proved the
        session owns no order on the token, so any level size present here is
        queue ahead.  An empty level registers a zero baseline by explicit
        decision (first tick with any depth then estimates A = 0% and cancels).
        """

        book = snapshot.get("book")
        if not isinstance(book, Mapping):
            return Decimal("0")
        try:
            bids = self._levels(book.get("bids"), "bids")
        except ValueError:
            return Decimal("0")
        return sum(
            (size for row_price, size in bids if row_price == price),
            Decimal("0"),
        )

    def _queue_protection_baseline(
        self, request: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> dict[str, object]:
        """Build the durable baseline facts recorded at the submit boundary."""

        book = snapshot.get("book")
        book_mapping = book if isinstance(book, Mapping) else None
        return {
            "baseline_front": self._queue_baseline_front(
                snapshot, cast(Decimal, request["price"])
            ),
            "baseline_price": request["price"],
            "baseline_source": "submit",
            "baseline_book_received_at": (
                book_mapping.get("received_at") if book_mapping else None
            ),
            "baseline_source_timestamp": (
                book_mapping.get("source_timestamp") if book_mapping else None
            ),
            "baseline_book_hash": book_mapping.get("hash") if book_mapping else None,
        }

    def _queue_protection_preview_estimate(
        self, request: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> dict[str, object]:
        """Project the post-submit share of the same-price level (A estimate)."""

        baseline_front = self._queue_baseline_front(
            snapshot, cast(Decimal, request["price"])
        )
        quantity = cast(Decimal, request["quantity"])
        projected = (
            baseline_front / (baseline_front + quantity)
            if baseline_front + quantity > 0
            else Decimal("0")
        )
        return {"baseline_front": baseline_front, "projected_ratio": projected}

    def _queue_baseline_book_sample(
        self, request: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> dict[str, object] | None:
        """Build one BBO receipt row mirroring sample_candidate_books."""

        book = snapshot.get("book")
        if not isinstance(book, Mapping) or book.get("received_at") is None:
            return None
        try:
            received_at = _timestamp(book.get("received_at"), name="book_received_at")
            bids = self._levels(book.get("bids"), "bids")
            asks = self._levels(book.get("asks"), "asks")
        except ValueError:
            return None
        if not bids or not asks:
            return None
        bid_price, bid_size = max(bids, key=lambda level: level[0])
        ask_price, ask_size = min(asks, key=lambda level: level[0])
        return {
            "condition_id": str(request["condition_id"]),
            "token_id": str(request["token_id"]),
            "received_at": received_at,
            "source_timestamp": book.get("source_timestamp"),
            "best_bid_price": bid_price,
            "best_bid_size": bid_size,
            "best_ask_price": ask_price,
            "best_ask_size": ask_size,
        }

    def _first_seen_book_sample(
        self,
        *,
        condition_id: str,
        token_id: str,
        book: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Build one BBO receipt row for a first-seen registration."""

        if book.get("received_at") is None:
            return None
        try:
            received_at = _timestamp(book.get("received_at"), name="book_received_at")
            bids = self._levels(book.get("bids"), "bids")
            asks = self._levels(book.get("asks"), "asks")
        except ValueError:
            return None
        if not bids or not asks:
            return None
        bid_price, bid_size = max(bids, key=lambda level: level[0])
        ask_price, ask_size = min(asks, key=lambda level: level[0])
        return {
            "condition_id": condition_id,
            "token_id": token_id,
            "received_at": received_at,
            "source_timestamp": book.get("source_timestamp"),
            "best_bid_price": bid_price,
            "best_bid_size": bid_size,
            "best_ask_price": ask_price,
            "best_ask_size": ask_size,
        }

    def register_first_seen_candidates(
        self, rows: object, *, now: datetime
    ) -> dict[str, object]:
        """Register first-seen baseline episodes for web-manual BUYs (issue 159).

        ``rows`` are one token's newly diff-detected own BUY rows (order id,
        price, remaining, venue created_at, first-seen stamp).  The anchor is
        the earliest first-seen BUY (ties by venue created_at, then order
        id); the anchor price's book level minus the own remaining at that
        price becomes the fallback baseline.  A book that cannot be read or
        a baseline that cannot be resolved fails the registration so the
        caller keeps the candidates pending — there is deliberately no time
        limit on that retry.
        """

        candidates = [row for row in _items(rows) if isinstance(row, Mapping)]
        if not candidates:
            return {"state": "skipped", "reason": "no_candidates"}
        token_id = str(candidates[0].get("token_id") or "").strip()
        condition_id = str(candidates[0].get("condition_id") or "").strip()
        if not token_id or not condition_id:
            return {"state": "skipped", "reason": "identity_unknown"}
        if any(
            str(row.get("token_id") or "").strip() != token_id for row in candidates
        ):
            return {"state": "skipped", "reason": "mixed_tokens"}
        with self._mutex:
            for episode in self.store.lp_active_first_seen_episodes():
                if str(episode.get("token_id") or "") == token_id:
                    return {"state": "skipped", "reason": "episode_exists"}
            ordered = sorted(
                candidates,
                key=lambda row: (
                    _first_seen_stamp_key(row.get("first_seen_at")),
                    _first_seen_stamp_key(row.get("created_at")),
                    str(row.get("order_id") or ""),
                ),
            )
            anchor = ordered[0]
            anchor_price = _maybe_decimal(anchor.get("price"))
            if anchor_price is None:
                return {
                    "state": "registration_failed",
                    "reason_codes": ["price_unknown"],
                    "token_id": token_id,
                }
            level_rows = [
                row
                for row in ordered
                if _maybe_decimal(row.get("price")) == anchor_price
            ]
            own_remaining = Decimal("0")
            for row in level_rows:
                remaining = _maybe_decimal(row.get("remaining"))
                if remaining is None:
                    own_remaining = None
                    break
                own_remaining += remaining
            book = self._read_first_seen_book(token_id)
            baseline = first_observation_baseline(
                book,
                price=anchor_price,
                own_remaining=own_remaining,
            )
            if baseline["state"] != "known":
                return {
                    "state": "registration_failed",
                    "reason_codes": list(baseline["reason_codes"]),
                    "token_id": token_id,
                }
            anchor_order_ids = [
                str(row.get("order_id") or "") for row in level_rows
            ]
            anchor_order_ids = [order_id for order_id in anchor_order_ids if order_id]
            first_seen_at = _first_seen_stamp_value(
                anchor.get("first_seen_at"), default=now
            )
            order_placement_times: dict[str, object] = {}
            placement_datetimes: list[datetime] = []
            for row in level_rows:
                order_id = str(row.get("order_id") or "")
                if not order_id:
                    continue
                try:
                    placement = _timestamp(
                        row.get("created_at"), name="venue_created_at"
                    )
                except ValueError:
                    order_placement_times[order_id] = None
                else:
                    order_placement_times[order_id] = _iso(placement)
                    placement_datetimes.append(placement)

            def first_text(key: str) -> str | None:
                for row in ordered:
                    value = _text(row.get(key))
                    if value is not None:
                        return value
                return None

            market_id = first_text("market_id")
            market_title = first_text("market_title")
            market_url = first_text("market_url")
            outcome = first_text("outcome")
            received_at = _timestamp(
                baseline["baseline_book_received_at"], name="book_received_at"
            )
            registration_delay = (received_at - first_seen_at).total_seconds()
            episode_id = uuid.uuid4().hex
            payload: dict[str, object] = {
                "token_id": token_id,
                "condition_id": condition_id,
                "anchor_order_ids": anchor_order_ids,
                "anchor_price": str(anchor_price),
                "baseline_front": baseline["baseline_front"],
                "baseline_price": baseline["baseline_price"],
                "baseline_book_received_at": baseline["baseline_book_received_at"],
                "baseline_book_hash": baseline["baseline_book_hash"],
                "baseline_source": "first_observation",
                "baseline_version": 1,
                "threshold": LP_QUEUE_PROTECTION_THRESHOLD,
                "first_seen_at": _iso(first_seen_at),
                "venue_created_at": (
                    _iso(min(placement_datetimes)) if placement_datetimes else None
                ),
                "order_placement_times": order_placement_times,
                "market_id": market_id,
                "market_title": market_title,
                "market_url": market_url,
                "outcome": outcome,
                "registration_delay": registration_delay,
                "data_failures": 0,
                "state": "monitoring",
                "notification_sent": False,
                "blocked_notified": False,
                "cancel_scope": "own_buys_at_level",
                "cancel_targets": [],
                "canceled_order_ids": [],
                "cancel_target_remaining": {},
                "canceled_remaining": None,
                "partially_filled_quantity": None,
                "cancel_requested_at": None,
                "cancel_reason": None,
                "reason_codes": [],
            }
            episode = self.store.lp_create_first_seen_episode(
                episode_id,
                token_id=token_id,
                condition_id=condition_id,
                state="monitoring",
                payload=payload,
            )
            recorder = getattr(self.store, "lp_record_book_samples", None)
            if callable(recorder):
                sample = self._first_seen_book_sample(
                    condition_id=condition_id, token_id=token_id, book=book
                )
                if sample is not None:
                    try:
                        recorder([sample], now=now)
                    except Exception:
                        pass
            return {
                "state": "registered",
                "episode_id": episode_id,
                "episode": episode,
                "token_id": token_id,
                "anchor_price": str(anchor_price),
                "baseline_front": baseline["baseline_front"],
            }

    def _read_first_seen_book(self, token_id: str) -> Mapping[str, object] | None:
        """Read one token's book through the bounded SDK batch adapter."""

        books_reader = getattr(self.exchange, "lp_order_books", None)
        if not callable(books_reader):
            return None
        try:
            try:
                raw_books = books_reader((token_id,), stop_event=None)
            except TypeError:
                raw_books = books_reader((token_id,))
        except Exception:
            return None
        if not isinstance(raw_books, Mapping):
            return None
        book = raw_books.get(token_id)
        return book if isinstance(book, Mapping) else None

    def start(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        *,
        idempotency_identity: str | None = None,
    ) -> dict[str, object]:
        """Revalidate a preview, persist intent, then submit exactly one BUY."""

        key = (idempotency_key or idempotency_identity or "").strip()
        if not key:
            return {"state": "rejected", "reason": "idempotency_key_required"}
        with self._mutex:
            existing = self.store.lp_session_by_idempotency(key)
            if existing is not None:
                return self._status_payload(existing)
            # The preview is read-only, but consuming it and creating the
            # durable submit intent must still happen under the same guard as
            # the eventual exchange write.  Otherwise a breaker opened after
            # preview could leave a phantom session or race another writer.
            if not self._mutation_allowed("submit"):
                return {"state": "locked", "reason": "mutation_blocked"}
            preview = self.store.lp_preview(preview_id)
            if preview is None:
                return {"state": "rejected", "reason": "preview_not_found"}
            try:
                expires_at = _timestamp(preview["expires_at"], name="preview_expiry")
                if self._now() >= expires_at:
                    raise ValueError("preview_expired")
                request = self._normalize_request(preview)
                read_started = self._now()
                snapshot = (
                    self._read_candidate_snapshot(request, now=read_started)
                    if request.get("candidate_policy") == "best_bid_minimum"
                    else self._read_snapshot(request)
                )
                # Issue 176: the validation clock is taken unconditionally
                # after the snapshot read completes (received_at stamps at
                # read time — a pre-read clock goes deterministically stale).
                now = self._now()
                if request.get("candidate_policy") == "best_bid_minimum":
                    eligible = self._fresh_candidate_row(request, snapshot, now=now)
                    if (
                        _decimal(eligible.get("price"), "candidate_price")
                        != request["price"]
                        or _decimal(eligible.get("quantity"), "candidate_quantity")
                        != request["quantity"]
                    ):
                        raise ValueError("candidate_changed")
                facts = self._validate_snapshot(
                    request, snapshot, now=now,
                    reservations=self._candidate_reservations(),
                )
                # Issue 166/R1: the submit path must honor the same
                # participation gate as the preview path — any account order
                # or position on this condition (either direction) rejects
                # here, so a same-condition opposite-direction group can only
                # ever be opened by #168 lifting this gate.  Idempotent
                # replays returned above and never reach this check, and the
                # augment paths tolerate their own group's orders because the
                # check is deliberately NOT inside _validate_snapshot.
                if _has_market_order(
                    cast(Mapping[str, object], snapshot.get("account")),
                    cast(Mapping[str, object], snapshot.get("market")),
                ):
                    raise ValueError("market_already_participating")
                # Issue 158: submit-time re-check — the fresh best bid must
                # still equal the one recorded in the preview credential.
                # Idempotent replays returned above before this point, so a
                # retry can never fail here after its session already exists.
                credential_preflight = preview.get("preflight")
                credential_best_bid = (
                    _maybe_decimal(credential_preflight.get("best_bid"))
                    if isinstance(credential_preflight, Mapping)
                    else None
                )
                if (
                    credential_best_bid is not None
                    and credential_best_bid != facts["best_bid"]
                ):
                    raise ValueError("best_bid_changed")
                expiration = expiration_for_review(
                    _timestamp(request["review_at"], name="review_at"), now=now
                )
            except ValueError as exc:
                return {"state": "rejected", "reason": str(exc)}
            try:
                self.store.consume_lp_preview(preview_id)
            except ValueError as exc:
                return {"state": "rejected", "reason": str(exc)}
            return self._entry_execute(
                request=request,
                snapshot=snapshot,
                facts=facts,
                now=now,
                key=key,
                expiration=expiration,
            )

    def _entry_execute(
        self,
        *,
        request: dict[str, object],
        snapshot: Mapping[str, object],
        facts: Mapping[str, object],
        now: datetime,
        key: str,
        expiration: int,
    ) -> dict[str, object]:
        """Register the session and submit exactly one post-only BUY.

        Shared tail of the two-phase ``start`` (issue 158) and the
        single-shot ``submit_entry`` (issue 163): the caller has already
        validated fresh facts and bound the GTD expiration; this helper
        owns the #152 baseline registration, the durable session row, the
        exchange post, and every terminal state write.
        """

        # Issue 152: registration boundary.  Validation just proved the
        # session owns no order on the token, so the whole price level in
        # this validated snapshot is queue ahead of the entry order.
        queue_baseline = self._queue_protection_baseline(request, snapshot)
        recorder = getattr(self.store, "lp_record_book_samples", None)
        if callable(recorder):
            baseline_sample = self._queue_baseline_book_sample(request, snapshot)
            if baseline_sample is not None:
                try:
                    recorder([baseline_sample], now=now)
                except Exception:
                    pass
        queue_baseline_summary = {
            "baseline_front": queue_baseline["baseline_front"],
            "baseline_price": queue_baseline["baseline_price"],
        }
        entry_action_base = {
            "submit_requested_at": _iso(now),
            "queue_protection_baseline": queue_baseline_summary,
        }
        reward_date = self._now().date().isoformat()
        session_id = uuid.uuid4().hex
        intent: dict[str, object] = {
            **request,
            "preflight": facts,
            "entry_order_id": None,
            "entry_expiration": expiration,
            "entry_cancel_requested": False,
            "buy_filled_quantity": Decimal("0"),
            "buy_cost": Decimal("0"),
            "sold_quantity": Decimal("0"),
            "sold_revenue": Decimal("0"),
            "residual_quantity": Decimal("0"),
            "residual_exit_value": Decimal("0"),
            "fees": Decimal("0"),
            "fee_status": "unknown",
            "opening_loss": None,
            "position_reconciled": False,
            "account_checked_at": None,
            "book_checked_at": None,
            "stop_loss_latched": False,
            "stop_loss_triggered_at": None,
            "stop_loss_triggered_loss": None,
            "scoring_status": "unknown",
            "scoring_checked_at": None,
            "scoring_order_id": None,
            "scoring_order_role": None,
            "scoring_lost_at": None,
            "passive_exit_order_id": None,
            "passive_exit_price": None,
            "passive_cancel_requested": False,
            "passive_exit_attempt_key": None,
            "passive_exit_attempt_state": None,
            "passive_exit_retryable": False,
            "protected_exit_order_id": None,
            "protected_exit_attempt_key": None,
            "protected_exit_attempt_state": None,
            "protected_exit_retryable": False,
            "protected_exit_submit_quantity": None,
            "protected_exit_submit_sold_quantity": None,
            "protected_exit_submit_residual_quantity": None,
            "owned_order_ids": [],
            "order_history": {},
            "orders_terminal": False,
            "reward_date": reward_date,
            "reward_status": "unknown",
            "trade_pnl": None,
            "total_pnl": None,
            "queue_protection": {
                **queue_baseline,
                "baseline_version": 1,
                "threshold": LP_QUEUE_PROTECTION_THRESHOLD,
                "data_failures": 0,
                "state": "registered",
                "notification_sent": False,
                "cancel_scope": "own_buys_at_level",
            },
        }
        try:
            session = self.store.lp_create_session(
                session_id,
                key,
                state="entry_submit_pending",
                payload=intent,
            )
        except ValueError as exc:
            return {"state": "rejected", "reason": str(exc)}
        entry_action_key = self._action_key(session_id, "entry-submit")
        self.store.lp_upsert_action(
            session_id,
            entry_action_key,
            state="pending",
            payload={
                "role": "entry",
                "side": "BUY",
                "token_id": request["token_id"],
                "expiration": expiration,
                **entry_action_base,
            },
        )
        try:
            signed = self._create_limit(
                token_id=str(request["token_id"]),
                price=cast(Decimal, request["price"]),
                quantity=cast(Decimal, request["quantity"]),
                side="BUY",
                post_only=True,
                expiration=expiration,
            )
            response = self._post_limit(signed)
        except Exception as exc:
            self.store.lp_upsert_action(
                session_id,
                entry_action_key,
                state="unknown",
                payload={
                    "role": "entry",
                    "side": "BUY",
                    "token_id": request["token_id"],
                    "expiration": expiration,
                    "error": type(exc).__name__,
                    **entry_action_base,
                },
            )
            session = self.store.lp_update_session(
                session_id,
                state="needs_attention",
                patch={"submit_status": "unknown", "resume_state": "entry_submit_pending"},
            )
            return self._status_payload(session)
        accepted, order_id = self._order_response(response)
        if not accepted:
            self.store.lp_upsert_action(
                session_id,
                entry_action_key,
                state="rejected",
                payload={
                    "role": "entry",
                    "side": "BUY",
                    "token_id": request["token_id"],
                    "expiration": expiration,
                    "order_id": order_id or "",
                    **entry_action_base,
                },
            )
            session = self.store.lp_update_session(
                session_id,
                state="entry_rejected",
                patch={"entry_order_id": order_id, "submit_status": "rejected"},
            )
            return self._status_payload(session)
        self.store.lp_upsert_action(
            session_id,
            entry_action_key,
            state="accepted",
            payload={
                "role": "entry",
                "side": "BUY",
                "token_id": request["token_id"],
                "expiration": expiration,
                "order_id": order_id,
                **entry_action_base,
                "submit_receipt_at": _iso(self._now()),
            },
        )
        if not order_id:
            session = self.store.lp_update_session(
                session_id,
                state="needs_attention",
                patch={
                    "submit_status": "accepted_without_order_id",
                    "resume_state": "entry_submit_pending",
                },
            )
            return self._status_payload(session)
        order_history = self._order_history(session)
        order_history[order_id] = {
            "order_id": order_id,
            "token_id": request["token_id"],
            "side": "BUY",
            "status": str(_field(response, "status", "LIVE")).upper() or "LIVE",
            "price": request["price"],
            "quantity": request["quantity"],
            "expiration": expiration,
        }
        session = self.store.lp_update_session(
            session_id,
            state="entry_open",
            patch={
                "entry_order_id": order_id,
                "submit_status": "accepted",
                "owned_order_ids": [order_id],
                "order_history": order_history,
            },
        )
        return self._status_payload(session)

    def _lp_market_conflict(
        self, condition_id: object, outcome: object
    ) -> dict[str, object] | None:
        """Issue 166: the active group occupying the same (condition_id, outcome).

        ``None`` means the market is free and admission may proceed; the
        returned session is the conflict group named by the busy payload.
        """

        wanted_condition = str(condition_id or "").strip()
        wanted_outcome = str(outcome or "").strip().upper()
        if not wanted_condition or not wanted_outcome:
            return None
        for session in self.store.lp_active_sessions():
            if (
                str(session.get("condition_id") or "").strip() == wanted_condition
                and str(session.get("outcome") or "").strip().upper()
                == wanted_outcome
            ):
                return session
        return None

    def submit_entry(
        self,
        request: Mapping[str, object],
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        """Issue 163: single-shot entry — validate fresh facts, post one BUY.

        No preview session exists: the one fresh snapshot read and the one
        full validation happen here, under the same guard order as
        ``start`` (key replay first, then mutation, then facts).  A
        rejection never creates a session row and never posts.
        """

        key = (idempotency_key or "").strip()
        if not key:
            return {"state": "rejected", "reason": "idempotency_key_required"}
        with self._mutex:
            existing = self.store.lp_session_by_idempotency(key)
            if existing is not None:
                return self._status_payload(existing)
            if not self._mutation_allowed("submit"):
                return {"state": "locked", "reason": "mutation_blocked"}
            # The request body is operator input: normalization failures are
            # caller errors and surface as exceptions (HTTP 400), while every
            # fact-vs-plan mismatch below is a semantic rejection payload.
            normalized = self._normalize_request(request)
            # Issue 166: the only admission conflict is another active group
            # on the same (condition_id, outcome); other markets proceed to
            # the original gates downstream.
            conflict = self._lp_market_conflict(
                normalized.get("condition_id"), normalized.get("outcome")
            )
            if conflict is not None:
                return {
                    "state": "busy",
                    "reason": "lp_session_market_active",
                    "session_id": conflict.get("session_id"),
                }
            try:
                snapshot = self._read_snapshot(normalized)
                # Issue 176: validation clock taken after the read completes
                # (received_at stamps at read time — a pre-read clock goes
                # deterministically stale).
                now = self._now()
                facts = self._validate_snapshot(
                    normalized, snapshot, now=now,
                    reservations=self._candidate_reservations(),
                )
                # Issue 166/R1: same participation gate as the preview and
                # two-phase start paths (see start()) — an account order or
                # position anywhere on this condition blocks the new group.
                if _has_market_order(
                    cast(Mapping[str, object], snapshot.get("account")),
                    cast(Mapping[str, object], snapshot.get("market")),
                ):
                    raise ValueError("market_already_participating")
            except ValueError as exc:
                # Issue 163: the single-shot trial anchor rejects with the
                # operator-facing reason instead of the internal candidate
                # mismatch name; freshness tolerance (60s) is preserved by
                # the candidate_policy passthrough above.
                if str(exc) == "candidate_best_bid_changed":
                    return {"state": "rejected", "reason": "best_bid_changed"}
                return {"state": "rejected", "reason": str(exc)}
            # Issue 163 定案 1（评审修复 P1）：默认试挂单锚定本次唯一快照的
            # 盘口顶档买一 max(bids)——与 _validate_snapshot 的 candidate 检查
            # 同源同口径（候选行 guidance 价与 UI 预填价即顶档）。奖励资格买一
            # （facts["best_bid"]，自顶档向下累计到 reward_min_size 才落定）在
            # 薄顶档市场低于顶档，不得用作锚，否则确认价=顶档时两个检查互斥、
            # 提交被永久锁死。5%/custom plans do not anchor.
            if normalized.get("candidate_policy") == "best_bid_minimum":
                book = snapshot.get("book")
                bids = self._levels(book.get("bids"), "bids")
                if (
                    cast(Decimal, normalized["price"])
                    != max(level_price for level_price, _ in bids)
                ):
                    return {"state": "rejected", "reason": "best_bid_changed"}
            try:
                expiration = expiration_for_review(
                    _timestamp(normalized["review_at"], name="review_at"),
                    now=now,
                )
            except ValueError as exc:
                return {"state": "rejected", "reason": str(exc)}
            return self._entry_execute(
                request=normalized,
                snapshot=snapshot,
                facts=facts,
                now=now,
                key=key,
                expiration=expiration,
            )

    def refresh_rewards(
        self,
        session_id: str | None = None,
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Refresh the persisted platform-earnings observation for one session.

        Issue 165: with ``session_id=None`` the unique active session is
        refreshed (falling back to the newest historical session as a
        read-only projection); an explicit id that matches no row returns
        the none payload and never falls back to the active group.
        """

        with self._reward_refresh_lock:
            session = self._reward_session(session_id)
            if session is None:
                return {"state": "none", "session_id": None}
            now = self._now()
            if self._reward_after_review(session, now):
                return self._status_payload(session)
            reward_date = self._session_reward_date(session)
            condition_id = _text(session.get("condition_id"))
            previous = session.get("reward_observation")
            previous_observation = (
                dict(previous) if isinstance(previous, Mapping) else {}
            )
            try:
                if reward_date is None or condition_id is None:
                    raise ValueError("reward_identity_unknown")
                reader = getattr(self.exchange, "lp_reward_snapshot", None)
                if not callable(reader):
                    raise ValueError("reward_reader_unavailable")
                if stop_event is None:
                    snapshot = reader(reward_date, condition_id)
                else:
                    snapshot = reader(
                        reward_date,
                        condition_id,
                        stop_event=stop_event,
                    )
                observation = self._known_reward_observation(
                    snapshot,
                    reward_date=reward_date,
                    condition_id=condition_id,
                    checked_at=now,
                )
            except Exception as exc:
                observation = self._unknown_reward_observation(
                    previous_observation,
                    reward_date=reward_date,
                    condition_id=condition_id,
                    attempted_at=now,
                    reason=type(exc).__name__,
                )
            with self._mutex:
                current = self.store.lp_session(str(session["session_id"]))
                if current is None:
                    return {"state": "none", "session_id": session.get("session_id")}
                updated = self.store.lp_update_session(
                    str(session["session_id"]),
                    patch={"reward_observation": observation},
                )
            return self._status_payload(updated)

    @staticmethod
    def _reward_after_review(session: Mapping[str, object], now: datetime) -> bool:
        review_at = session.get("review_at")
        if review_at is None:
            return False
        try:
            review_boundary = _timestamp(review_at, name="review_at")
        except ValueError:
            return False
        if now < review_boundary:
            return False
        previous = session.get("reward_observation")
        if not isinstance(previous, Mapping):
            return False
        last_attempt_at = previous.get("last_attempt_at")
        if last_attempt_at is None:
            return False
        try:
            return _timestamp(last_attempt_at, name="last_attempt_at") >= review_boundary
        except ValueError:
            return False

    def _reward_session(self, session_id: str | None) -> dict[str, object] | None:
        if session_id:
            return self.store.lp_session(session_id)
        session = self.store.lp_active_session()
        if session is not None:
            return session
        latest = getattr(self.store, "lp_latest_session", None)
        return latest() if callable(latest) else None

    @staticmethod
    def _session_reward_date(session: Mapping[str, object]) -> str | None:
        value = _text(session.get("reward_date"))
        if value is not None:
            try:
                return datetime.fromisoformat(value).date().isoformat()
            except ValueError:
                pass
        created_at = session.get("created_at")
        try:
            return _timestamp(created_at, name="created_at").date().isoformat()
        except ValueError:
            return None

    @staticmethod
    def _known_reward_observation(
        snapshot: object,
        *,
        reward_date: str,
        condition_id: str,
        checked_at: datetime,
    ) -> dict[str, object]:
        if not isinstance(snapshot, Mapping):
            raise ValueError("reward_snapshot_unknown")
        partial_raw_read = (
            snapshot.get("state") == "unknown"
            and snapshot.get("reason") == "usd_value_unknown"
            and (
                _maybe_decimal(snapshot.get("market_amount_raw")) is not None
                or _maybe_decimal(snapshot.get("account_amount_raw")) is not None
                or bool(snapshot.get("market_accruals_raw"))
                or bool(snapshot.get("account_accruals_raw"))
            )
        )
        if snapshot.get("state") != "known" and not partial_raw_read:
            raise ValueError("reward_snapshot_unknown")
        if str(snapshot.get("reward_date") or "") != reward_date:
            raise ValueError("reward_date_mismatch")
        if str(snapshot.get("condition_id") or "") != condition_id:
            raise ValueError("reward_condition_mismatch")
        account_amount = _maybe_decimal(snapshot.get("account_amount"))
        market_amount = _maybe_decimal(snapshot.get("market_amount"))
        if snapshot.get("state") == "known" and (
            account_amount is None or market_amount is None
        ):
            raise ValueError("reward_amount_unknown")
        if account_amount is not None and account_amount < 0:
            raise ValueError("reward_amount_invalid")
        if market_amount is not None and market_amount < 0:
            raise ValueError("reward_amount_invalid")
        usd_known = account_amount is not None and market_amount is not None
        if usd_known:
            assert account_amount is not None and market_amount is not None
            gap = max(Decimal("0"), REWARD_THRESHOLD - account_amount)
            status = "met" if account_amount >= REWARD_THRESHOLD else "below"
        else:
            gap = None
            status = "unknown"
        account_amount_raw = _maybe_decimal(snapshot.get("account_amount_raw"))
        market_amount_raw = _maybe_decimal(snapshot.get("market_amount_raw"))
        if account_amount_raw is not None and account_amount_raw < 0:
            account_amount_raw = None
        if market_amount_raw is not None and market_amount_raw < 0:
            market_amount_raw = None
        checked = _iso(checked_at)
        return {
            "status": status,
            "threshold_status": status,
            "reward_date": reward_date,
            "condition_id": condition_id,
            "market_amount": market_amount,
            "account_amount": account_amount,
            "gap": gap,
            "market_amount_raw": market_amount_raw,
            "market_asset": _text(snapshot.get("market_asset")),
            "market_accruals_raw": _reward_accrual_rows(
                snapshot.get("market_accruals_raw")
            ),
            "account_amount_raw": account_amount_raw,
            "account_asset": _text(snapshot.get("account_asset")),
            "account_accruals_raw": _reward_accrual_rows(
                snapshot.get("account_accruals_raw")
            ),
            "usd_state": snapshot.get("usd_state", "known" if usd_known else "unknown"),
            "valuation_reason": snapshot.get("reason") if not usd_known else None,
            "checked_at": checked,
            "last_success_at": checked,
            "last_attempt_at": checked,
            "stale": False,
            "source": "platform_earnings",
            "currency": "USD",
            "paid": False,
        }

    @staticmethod
    def _unknown_reward_observation(
        previous: Mapping[str, object],
        *,
        reward_date: str | None,
        condition_id: str | None,
        attempted_at: datetime,
        reason: str,
    ) -> dict[str, object]:
        same_session = (
            previous.get("reward_date") == reward_date
            and previous.get("condition_id") == condition_id
        )
        retained = previous if same_session else {}
        return {
            "status": "unknown",
            "threshold_status": "unknown",
            "reward_date": reward_date,
            "condition_id": condition_id,
            "market_amount": retained.get("market_amount"),
            "account_amount": retained.get("account_amount"),
            "gap": retained.get("gap"),
            "market_amount_raw": retained.get("market_amount_raw"),
            "market_asset": retained.get("market_asset"),
            "market_accruals_raw": retained.get("market_accruals_raw"),
            "account_amount_raw": retained.get("account_amount_raw"),
            "account_asset": retained.get("account_asset"),
            "account_accruals_raw": retained.get("account_accruals_raw"),
            "usd_state": retained.get("usd_state", "unknown"),
            "checked_at": retained.get("checked_at"),
            "last_success_at": retained.get("last_success_at"),
            "last_attempt_at": _iso(attempted_at),
            "stale": True,
            "source": "platform_earnings",
            "currency": "USD",
            "paid": False,
            "error": reason,
        }

    def generate_due_report(self) -> dict[str, object]:
        """Persist the next completed Beijing 08:00 report from saved facts."""

        now = self._now()
        local_now = now.astimezone(_BEIJING)
        read_report = getattr(self.store, "lp_daily_report", None)
        save_report = getattr(self.store, "lp_save_daily_report", None)
        list_sessions = getattr(self.store, "lp_sessions", None)
        latest_report = getattr(self.store, "lp_latest_daily_report", None)
        if not all(
            callable(method)
            for method in (read_report, save_report, list_sessions, latest_report)
        ):
            return {"state": "unavailable"}
        with self._report_lock:
            sessions = list_sessions()
            if not isinstance(sessions, (list, tuple)):
                sessions = []
            today_boundary = datetime.combine(local_now.date(), time(8), tzinfo=_BEIJING)
            completed_date = (
                local_now.date()
                if local_now >= today_boundary
                else local_now.date() - timedelta(days=1)
            )
            latest = latest_report()
            if isinstance(latest, Mapping) and latest.get("report_date"):
                target_date = date.fromisoformat(str(latest["report_date"])) + timedelta(days=1)
            else:
                due_review_dates: list[date] = []
                for session in sessions:
                    if not isinstance(session, Mapping):
                        continue
                    try:
                        review_at = _timestamp(session.get("review_at"), name="review_at")
                    except ValueError:
                        continue
                    review_date = review_at.astimezone(_BEIJING).date()
                    if review_date <= completed_date and review_at <= now:
                        due_review_dates.append(review_date)
                target_date = min(due_review_dates) if due_review_dates else completed_date
            if target_date > completed_date:
                existing = read_report(completed_date.isoformat())
                return existing if isinstance(existing, Mapping) else {"state": "not_due"}

            report_date = target_date.isoformat()
            existing = read_report(report_date)
            if existing is not None:
                return existing
            period_end = datetime.combine(target_date, time(8), tzinfo=_BEIJING).astimezone(UTC)
            period_start = period_end - timedelta(days=1)
            session_reports: list[dict[str, object]] = []
            for session in sessions:
                if not isinstance(session, Mapping):
                    continue
                row, relevant = self._daily_report_session(
                    session,
                    period_start=period_start,
                    period_end=period_end,
                    generated_at=now,
                )
                if not relevant:
                    continue
                session_reports.append(row)
            realized_total = self._known_report_sum(
                session_reports, "realized_trade_pnl", empty=Decimal("0")
            )
            paid_total = self._known_report_sum(session_reports, "paid_rewards")
            net_total = (
                realized_total + paid_total
                if realized_total is not None and paid_total is not None
                else None
            )
            payload: dict[str, object] = {
                "state": "ready",
                "report_date": report_date,
                "period_start": _report_boundary_iso(period_start),
                "period_end": _report_boundary_iso(period_end),
                "generated_at": _report_boundary_iso(now),
                "observation_status": "late" if now > period_end else "on_time",
                "cutoff_market_data_status": "unknown" if now > period_end else "observed_at_generation",
                "sessions": session_reports,
                "totals": {
                    "realized_trade_pnl": realized_total,
                    "paid_rewards": paid_total,
                    "realized_net_pnl": net_total,
                },
            }
            saved = save_report(report_date, payload)
            return saved if isinstance(saved, Mapping) else payload

    @staticmethod
    def _known_report_sum(
        rows: Sequence[Mapping[str, object]],
        key: str,
        *,
        empty: Decimal | None = None,
    ) -> Decimal | None:
        if not rows:
            return empty
        values = [_maybe_decimal(row.get(key)) for row in rows]
        if any(value is None for value in values):
            return None
        return sum((value for value in values if value is not None), Decimal("0"))

    @staticmethod
    def _daily_report_session(
        session: Mapping[str, object],
        *,
        period_start: datetime,
        period_end: datetime,
        generated_at: datetime,
    ) -> tuple[dict[str, object], bool]:
        from .polymarket_lp_views import lp_report_totals

        # The reserved manual-cancel anchor session is audit bookkeeping,
        # never reportable LP activity; report it as irrelevant so the
        # caller's `if not relevant: continue` skips it.
        if session.get("session_id") == LP_RESERVED_MANUAL_SESSION_ID:
            return {"session_id": str(session.get("session_id") or "")}, False

        events_value = session.get("trade_events")
        events = _items(events_value)
        ledger_complete = isinstance(events_value, (list, tuple))
        normalized: list[dict[str, object]] = []
        seen: dict[tuple[str, str], dict[str, object]] = {}
        event_totals = {
            "buy_filled_quantity": Decimal("0"),
            "buy_cost": Decimal("0"),
            "sold_quantity": Decimal("0"),
            "sold_revenue": Decimal("0"),
        }
        for raw_event in events:
            if not isinstance(raw_event, Mapping):
                ledger_complete = False
                continue
            status = str(raw_event.get("status") or "").upper()
            if status == "FAILED":
                continue
            if status != "CONFIRMED":
                ledger_complete = False
                continue
            trade_id = str(raw_event.get("trade_id") or "")
            order_id = str(raw_event.get("order_id") or "")
            if not trade_id:
                ledger_complete = False
                continue
            identity = (trade_id, order_id)
            if identity in seen:
                if seen[identity] != dict(raw_event):
                    ledger_complete = False
                continue
            try:
                matched_at = _timestamp(raw_event.get("matched_at"))
            except ValueError:
                ledger_complete = False
                continue
            side = str(raw_event.get("side") or "").upper()
            quantity = _maybe_decimal(raw_event.get("quantity"))
            price = _maybe_decimal(raw_event.get("price"))
            fee = _maybe_decimal(raw_event.get("fee"))
            if (
                side not in {"BUY", "SELL"}
                or quantity is None
                or quantity <= 0
                or price is None
                or price <= 0
            ):
                ledger_complete = False
                continue
            event = {
                "trade_id": trade_id,
                "order_id": order_id,
                "matched_at": matched_at,
                "side": side,
                "quantity": quantity,
                "price": price,
                "fee": fee,
            }
            seen[identity] = dict(raw_event)
            normalized.append(event)
            quantity_key, value_key = (
                ("buy_filled_quantity", "buy_cost")
                if side == "BUY"
                else ("sold_quantity", "sold_revenue")
            )
            event_totals[quantity_key] += quantity
            event_totals[value_key] += quantity * price

        for key, event_total in event_totals.items():
            recorded = _maybe_decimal(session.get(key))
            if recorded is None or recorded != event_total:
                ledger_complete = False

        for side, field_name in (("BUY", "buy_fees"), ("SELL", "sell_fees")):
            side_events = [event for event in normalized if event["side"] == side]
            fees = [_maybe_decimal(event.get("fee")) for event in side_events]
            fee_total = (
                None
                if any(fee is None for fee in fees)
                else sum((fee for fee in fees if fee is not None), Decimal("0"))
            )
            recorded_fee = _maybe_decimal(session.get(field_name))
            if side_events and (fee_total is None or recorded_fee != fee_total):
                ledger_complete = False
            elif not side_events and recorded_fee not in (None, Decimal("0")):
                ledger_complete = False

        normalized.sort(key=lambda event: cast(datetime, event["matched_at"]))

        payment_events_value = session.get("verified_paid_reward_events")
        payment_events = _items(payment_events_value)
        payments_complete = isinstance(payment_events_value, (list, tuple)) and bool(
            payment_events
        )
        normalized_payments: list[tuple[datetime, Decimal]] = []
        for raw_payment in payment_events:
            if not isinstance(raw_payment, Mapping) or raw_payment.get("verified") is not True:
                payments_complete = False
                continue
            try:
                paid_at = _timestamp(raw_payment.get("paid_at"))
            except ValueError:
                payments_complete = False
                continue
            amount = _maybe_decimal(raw_payment.get("usd_amount"))
            if not raw_payment.get("payment_id") or amount is None or amount < 0:
                payments_complete = False
                continue
            normalized_payments.append((paid_at, amount))

        def cutoff_values(cutoff: datetime) -> tuple[dict[str, object], dict[str, object]]:
            selected = [
                event
                for event in normalized
                if cast(datetime, event["matched_at"]) < cutoff
            ]
            sums = {
                "buy_filled_quantity": Decimal("0"),
                "buy_cost": Decimal("0"),
                "sold_quantity": Decimal("0"),
                "sold_revenue": Decimal("0"),
            }
            cutoff_fees: dict[str, Decimal | None] = {}
            for event in selected:
                side = str(event["side"])
                quantity = cast(Decimal, event["quantity"])
                price = cast(Decimal, event["price"])
                quantity_key, value_key = (
                    ("buy_filled_quantity", "buy_cost")
                    if side == "BUY"
                    else ("sold_quantity", "sold_revenue")
                )
                sums[quantity_key] += quantity
                sums[value_key] += quantity * price
            for side, field_name in (("BUY", "buy_fees"), ("SELL", "sell_fees")):
                side_events = [event for event in selected if event["side"] == side]
                fees = [_maybe_decimal(event.get("fee")) for event in side_events]
                cutoff_fees[field_name] = (
                    None
                    if any(fee is None for fee in fees)
                    else sum((fee for fee in fees if fee is not None), Decimal("0"))
                )
            buy_quantity = sums["buy_filled_quantity"]
            sold_quantity = sums["sold_quantity"]
            residual_quantity = buy_quantity - sold_quantity
            if residual_quantity < 0:
                residual_quantity = None
            if not ledger_complete:
                cutoff_sums: dict[str, object] = {key: None for key in sums}
                cutoff_fees = {key: None for key in cutoff_fees}
                residual_quantity = None
            else:
                cutoff_sums = sums
            paid_total = (
                sum(
                    (amount for paid_at, amount in normalized_payments if paid_at < cutoff),
                    Decimal("0"),
                )
                if payments_complete
                else None
            )
            opening: dict[str, object] = {
                **cutoff_sums,
                **cutoff_fees,
                "residual_quantity": residual_quantity,
                "residual_exit_value": Decimal("0")
                if residual_quantity == 0
                else None,
                "projected_exit_fee": Decimal("0")
                if residual_quantity == 0
                else None,
            }
            return opening, lp_report_totals(opening, paid_rewards=paid_total)

        _, opening_projection = cutoff_values(period_start)
        closing_totals, closing_projection = cutoff_values(period_end)

        def difference(key: str) -> Decimal | None:
            opening_value = _maybe_decimal(opening_projection.get(key))
            closing_value = _maybe_decimal(closing_projection.get(key))
            if opening_value is None or closing_value is None:
                return None
            return closing_value - opening_value

        realized_value = difference("realized_trade_pnl")
        paid_rewards = difference("paid_rewards")
        realized_net = (
            realized_value + paid_rewards
            if realized_value is not None and paid_rewards is not None
            else None
        )
        inventory_at_end = closing_totals.get("residual_quantity")
        inventory_at_end = (
            cast(Decimal, inventory_at_end)
            if isinstance(inventory_at_end, Decimal)
            else None
        )

        residual_exit_at_end: Decimal | None = None
        quote_status = "unknown"
        if inventory_at_end == 0:
            residual_exit_at_end = Decimal("0")
            quote_status = "not_required"
        elif inventory_at_end is not None and generated_at <= period_end:
            try:
                book_checked_at = _timestamp(session.get("book_checked_at"))
            except ValueError:
                book_checked_at = None
            current_exit_value = _maybe_decimal(session.get("residual_exit_value"))
            if (
                book_checked_at is not None
                and abs((period_end - book_checked_at).total_seconds())
                <= float(BOOK_FRESHNESS_SECONDS)
                and current_exit_value is not None
            ):
                residual_exit_at_end = current_exit_value
                quote_status = "known"

        order_history = session.get("order_history")
        unresolved_orders: list[dict[str, object]] = []
        if isinstance(order_history, Mapping):
            for order_id, raw_order in order_history.items():
                if not isinstance(raw_order, Mapping):
                    continue
                status = str(raw_order.get("status") or "UNKNOWN").upper()
                if status not in TERMINAL_ORDER_STATES:
                    unresolved_orders.append({"order_id": str(order_id), "status": status})
        reward_observation = session.get("reward_observation")
        pending_rewards = (
            dict(reward_observation)
            if isinstance(reward_observation, Mapping)
            else None
        )
        current_inventory = _maybe_decimal(session.get("residual_quantity"))
        current_exit_value = _maybe_decimal(session.get("residual_exit_value"))
        stop_loss_at = session.get("stop_loss_triggered_at")
        stop_loss_value = _maybe_decimal(session.get("stop_loss_triggered_loss"))
        session_report = {
            "session_id": str(session.get("session_id") or ""),
            "market_id": session.get("market_id"),
            "condition_id": session.get("condition_id"),
            "market_title": session.get("market_title", session.get("question")),
            "market_url": session.get("market_url"),
            "outcome": session.get("outcome"),
            "session_state": session.get("state"),
            "period_start": _report_boundary_iso(period_start),
            "period_end": _report_boundary_iso(period_end),
            "realized_trade_pnl": realized_value,
            "realized_trade_pnl_status": "known" if realized_value is not None else "unknown",
            "paid_rewards": paid_rewards,
            "paid_rewards_status": "known" if paid_rewards is not None else "needs_verification",
            "realized_net_pnl": realized_net,
            "residual_quantity_at_period_end": inventory_at_end,
            "residual_exit_value_at_period_end": residual_exit_at_end,
            "residual_exit_estimate_status": quote_status,
            "current_residual_quantity": current_inventory,
            "current_residual_exit_value": current_exit_value,
            "current_inventory_checked_at": session.get("account_checked_at"),
            "current_exit_checked_at": session.get("book_checked_at"),
            "management_observed_at": _report_boundary_iso(generated_at),
            "stop_loss_triggered": stop_loss_at is not None or session.get("stop_loss_latched") is True,
            "stop_loss_triggered_at": stop_loss_at,
            "stop_loss_triggered_loss": stop_loss_value,
            "review_status": session.get("review_status"),
            "orders_terminal": session.get("orders_terminal"),
            "unresolved_orders": unresolved_orders,
            "reconciliation": session.get("reconciliation"),
            "pending_rewards": pending_rewards,
        }

        event_in_period = any(
            period_start <= cast(datetime, event["matched_at"]) < period_end
            for event in normalized
        )
        payment_in_period = any(
            period_start <= paid_at < period_end
            for paid_at, _amount in normalized_payments
        )
        try:
            review_at = _timestamp(session.get("review_at"), name="review_at")
        except ValueError:
            review_at = None
        review_in_period = review_at is not None and period_start < review_at <= period_end
        try:
            created_at = _timestamp(session.get("created_at"), name="created_at")
        except ValueError:
            created_at = None
        created_in_period = created_at is not None and period_start <= created_at < period_end
        active_at_observation = str(session.get("state") or "") not in {
            "complete",
            "entry_rejected",
        }
        existed_at_cutoff = created_at is not None and created_at <= period_end
        relevant = (
            event_in_period
            or payment_in_period
            or review_in_period
            or created_in_period
            or (inventory_at_end is not None and inventory_at_end > 0)
            or (active_at_observation and existed_at_cutoff)
        )
        return session_report, relevant

    def status(self, session_id: str | None = None) -> dict[str, object]:
        """Report one session; ``None`` targets the single active session.

        Issue 165: with ``session_id=None`` the unique active session is
        reported (falling back to the newest historical session as a
        read-only projection); an explicit id that matches no row returns
        the none payload and never falls back to the active group.
        """

        with self._mutex:
            session = (
                self.store.lp_session(session_id)
                if session_id
                else self.store.lp_active_session()
            )
            if session is None and not session_id:
                latest = getattr(self.store, "lp_latest_session", None)
                session = latest() if callable(latest) else None
            if session is None:
                return {"state": "none", "session_id": None}
            result = self._status_payload(session)
            checked_at = session.get("scoring_checked_at")
            if result.get("scoring_status") in {"true", "false"} and checked_at is not None:
                try:
                    age = (self._now() - _timestamp(checked_at, name="scoring_checked_at")).total_seconds()
                except ValueError:
                    age = float("inf")
                if age < 0 or age > float(SCORING_STALE_SECONDS):
                    result["scoring_status"] = "unknown"
            return result

    def stop(self, session_id: str | None = None) -> dict[str, object]:
        """Stop one session; ``None`` targets the single active session.

        Issue 165: with ``session_id=None`` the unique active session is
        stopped; an explicit id that matches no row returns the none payload
        and never falls back to the active group.  Issue 166: with two or
        more active groups the unnamed stop is rejected as ambiguous — the
        operator must name the group.
        """

        with self._mutex:
            if session_id:
                session = self.store.lp_session(session_id)
            else:
                active = self.store.lp_active_sessions()
                if len(active) >= 2:
                    return {
                        "state": "rejected",
                        "reason": "session_ambiguous",
                        "session_ids": [
                            row.get("session_id") for row in active
                        ],
                    }
                session = active[0] if active else None
            if session is None:
                return {"state": "none", "session_id": None}
            state = str(session.get("state"))
            if state in {"complete", "entry_rejected", "review"}:
                return self._status_payload(session)
            try:
                self._cancel_owned_orders(session)
            except Exception as exc:
                updated = self.store.lp_update_session(
                    str(session["session_id"]),
                    state="needs_attention",
                    patch={
                        "stop_requested": True,
                        "reconciliation": f"stop_cancel_{type(exc).__name__}",
                        "resume_state": "review",
                    },
                )
                return self._status_payload(updated)
            updated = self.store.lp_update_session(
                str(session["session_id"]),
                state="review",
                patch={
                    "stop_requested": True,
                    "review_status": "awaiting_reconciliation",
                    # 停止即结束当前「需要核对」episode：不清键会让残留
                    # since/notified 把下一轮 episode 当同一轮（不推或早推）。
                    "needs_attention_since": None,
                    "needs_attention_notified": False,
                },
            )
            return self._status_payload(updated)

    def _queue_protection_levels(
        self, session: Mapping[str, object]
    ) -> dict[str, dict[str, object]]:
        """All per-price protection buckets of one session (D6 read helper)."""

        return _queue_protection_level_buckets(
            session.get("queue_protection"),
            default_order_id=str(session.get("entry_order_id") or ""),
        )

    def _order_cancel_requested(
        self, session: Mapping[str, object], order_id: str
    ) -> bool:
        """True when a cancel was already requested for this own order."""

        if not order_id:
            return False
        if str(session.get("entry_order_id") or "") == order_id:
            return bool(session.get("entry_cancel_requested"))
        requested = {str(value) for value in _items(session.get("augment_cancel_requested"))}
        return order_id in requested

    def _bucket_protection_gate_open(
        self,
        session: Mapping[str, object],
        bucket: Mapping[str, object],
        rows_by_id: Mapping[str, object],
    ) -> bool:
        """Issue 167: a bucket is protected only while its own order is
        alive, not yet cancel-requested, and completely unfilled.

        Evidence order mirrors the runtime reads: the durable order history
        first, then the snapshot receipts.  An order with no readable
        status anywhere counts as alive (the pre-sync state).
        """

        order_id = str(bucket.get("order_id") or "")
        if not order_id:
            return False
        if self._order_cancel_requested(session, order_id):
            return False
        history = self._order_history(session)
        record = history.get(order_id)
        row = rows_by_id.get(order_id) if rows_by_id is not None else None
        source = (
            record
            if isinstance(record, Mapping) and record.get("status")
            else row
        )
        if source is None:
            return True
        status = str(_field(source, "status", "") or "").upper()
        if status and status in TERMINAL_ORDER_STATES:
            return False
        matched = _maybe_decimal(_field(source, "size_matched"))
        if matched is not None and matched > 0:
            return False
        return True

    def _queue_protection_gate_open(self, session: Mapping[str, object]) -> bool:
        """Issue 152/167: protection still applies to at least one bucket.

        Shared by the runtime evaluation, the data-outage counter, and the
        conservative cancel so the three can never drift apart.
        """

        buckets = self._queue_protection_levels(session)
        if not buckets:
            return False
        history = self._order_history(session)
        for bucket in buckets.values():
            if str(bucket.get("state")) in {"canceling", "canceled", "partially_filled"}:
                continue
            order_id = str(bucket.get("order_id") or "")
            if not order_id or self._order_cancel_requested(session, order_id):
                continue
            record = history.get(order_id, {})
            status = str(record.get("status") or "").upper()
            if status and status in TERMINAL_ORDER_STATES:
                continue
            matched = _maybe_decimal(record.get("size_matched"))
            if matched is not None and matched > 0:
                continue
            return True
        return False

    def _queue_protection_data_failure(
        self, session: Mapping[str, object], reason: str
    ) -> dict[str, object] | None:
        """Increment the durable group outage counter on data-outage exits."""

        protection = session.get("queue_protection")
        buckets = self._queue_protection_levels(session)
        if not isinstance(protection, Mapping) or not buckets:
            return None
        if reason not in _QUEUE_DATA_FAILURE_REASONS:
            return None
        if all(
            str(bucket.get("state")) in {"canceling", "canceled", "partially_filled"}
            for bucket in buckets.values()
        ):
            return None
        if not self._queue_protection_gate_open(session):
            # No bucket protects a live order anymore (filled, cancel
            # requested, terminal, or never submitted): an outage streak is
            # irrelevant, so the counter resets instead of accumulating
            # toward a stale conservative cancel.
            failures = 0
        else:
            failures = _queue_group_failures_int(protection.get("data_failures")) + 1
        return {
            "version": 2,
            "data_failures": failures,
            "levels": buckets,
        }

    def _conservative_protection_cancel(
        self,
        session: Mapping[str, object],
        failures: dict[str, object] | None,
    ) -> dict[str, object] | None:
        """Issue 152 D5 / 167 D4: cancel the whole group's own BUYs after
        ten data outages — one protection cancel per price bucket."""

        if failures is None:
            return None
        count = _maybe_decimal(failures.get("data_failures")) or Decimal("0")
        if count < LP_PROTECTION_DATA_FAILURE_LIMIT:
            return None
        if not self._queue_protection_gate_open(session):
            return None
        buckets = failures.get("levels")
        if not isinstance(buckets, Mapping) or not buckets:
            return None
        new_levels: dict[str, dict[str, object]] = {}
        session_patch: dict[str, object] = {}
        changed = False
        for key, bucket in buckets.items():
            if not isinstance(bucket, Mapping):
                new_levels[str(key)] = dict(bucket) if isinstance(bucket, Mapping) else {}
                continue
            bucket_dict = dict(bucket)
            if str(bucket_dict.get("state")) in {"canceling", "canceled", "partially_filled"}:
                new_levels[str(key)] = bucket_dict
                continue
            if not self._bucket_protection_gate_open(session, bucket_dict, {}):
                new_levels[str(key)] = bucket_dict
                continue
            new_bucket, patch = self._request_bucket_protection_cancel(
                session, None, bucket_dict, reason="book_unreliable"
            )
            new_levels[str(key)] = new_bucket
            for patch_key, patch_value in patch.items():
                if patch_key == "augment_cancel_requested":
                    merged = {
                        str(value)
                        for value in _items(session_patch.get(patch_key))
                    } | {str(value) for value in _items(patch_value)}
                    session_patch[patch_key] = sorted(merged)
                else:
                    session_patch[patch_key] = patch_value
            changed = True
        if not changed:
            return None
        session_patch["queue_protection"] = {
            "version": 2,
            "data_failures": _queue_group_failures_int(failures.get("data_failures")),
            "levels": new_levels,
        }
        return self.store.lp_update_session(
            str(session["session_id"]), patch=session_patch
        )

    def tick(self) -> dict[str, object]:
        """Run one deterministic monitoring/reconciliation iteration."""

        return self._tick()

    def tick_with_apply_lock(
        self,
        acquire_lock: Callable[[], object | None],
        release_lock: Callable[[object], None],
    ) -> dict[str, object]:
        """Run a tick with the execution lock reserved for short apply work."""

        return self._tick(apply_lock=(acquire_lock, release_lock))

    def _tick(
        self,
        *,
        apply_lock: tuple[Callable[[], object | None], Callable[[object], None]] | None = None,
    ) -> dict[str, object]:
        """Run one deterministic monitoring/reconciliation iteration.

        Issue 165: group-level reconciliation is delegated to
        ``_reconcile_session`` for every active session row.  Issue 166:
        with zero groups the none payload is returned unchanged; with one
        group the top level keeps today's single-group payload shape plus a
        ``sessions`` key; with several groups the top level is an aggregate
        (worst state, oldest check stamps, no session_id) carrying every
        group's full payload in ``sessions``.
        """

        # Issue 159: first-seen fallback protections remain first and
        # serialized.  If another execution owns the global lock, skip this
        # retry and continue with the active-session lane.
        if apply_lock is None:
            with self._mutex:
                self._apply_first_seen_protections()
        else:
            first_seen_lock = apply_lock[0]()
            if first_seen_lock is not None:
                try:
                    with self._mutex:
                        self._apply_first_seen_protections()
                finally:
                    apply_lock[1](first_seen_lock)

        active_reader = getattr(
            self.store, "lp_active_sessions_with_revisions", None
        )
        if callable(active_reader):
            session_pairs = active_reader()
        else:
            # Compatibility for lightweight stores used by older callers.
            session_pairs = []
            for session in self.store.lp_active_sessions():
                session_id = str(session.get("session_id") or "")
                try:
                    revision = self.store.lp_session_revision(session_id)
                except Exception:
                    revision = 0
                session_pairs.append((session, revision))
        if not session_pairs:
            return {"state": "none", "session_id": None}

        payloads: list[dict[str, object]] = []
        for session, initial_revision in session_pairs:
            session_id = str(session.get("session_id") or "")
            snapshot: Mapping[str, object] | None = None
            snapshot_error: ValueError | None = None
            snapshot_exception: Exception | None = None
            try:
                request = self._normalize_request(session)
                # The main network read deliberately runs outside both the
                # service mutex and the execution service's global lock.
                snapshot = self._read_snapshot(request)
            except ValueError as exc:
                snapshot_error = exc
            except Exception as exc:
                snapshot_exception = exc

            protected_levels: set[str] = set()
            protection_writes = 0
            if snapshot is not None:
                try:
                    protected_levels, protection_writes = (
                        self._apply_triggered_protection_cancel_off_lock(
                            session, snapshot
                        )
                    )
                except Exception:
                    # A partial protection write is fenced by the durable
                    # revision below.  If no write happened, the ordinary
                    # serialized apply can retry the protection operation.
                    protected_levels = set()
                    protection_writes = 0

            apply_handle: object | None = None
            try:
                if apply_lock is not None:
                    apply_handle = apply_lock[0]()
                    if apply_handle is None:
                        current = self.store.lp_session(session_id) or session
                        payloads.append(self._status_payload(current))
                        continue
                with self._mutex:
                    if snapshot_exception is not None:
                        raise snapshot_exception
                    current_with_revision = getattr(
                        self.store, "lp_session_with_revision", None
                    )
                    if callable(current_with_revision):
                        current_row = current_with_revision(session_id)
                    else:
                        current = self.store.lp_session(session_id)
                        current_row = (
                            (current, self.store.lp_session_revision(session_id))
                            if current is not None
                            else None
                        )
                    if current_row is None:
                        payloads.append(self._status_payload(session))
                        continue
                    current, current_revision = current_row
                    expected_revision = int(initial_revision) + protection_writes
                    if current_revision != expected_revision:
                        # A submit/augment changed this group while its
                        # snapshot was in flight.  The durable bucket cancel
                        # remains valid; discard the stale group-wide apply.
                        payloads.append(self._status_payload(current))
                        continue
                    if snapshot_error is not None:
                        payloads.append(self._handle_snapshot_failure(current, snapshot_error))
                        continue
                    if snapshot is None:
                        raise RuntimeError("lp_tick_snapshot_missing")
                    reconcile = self._reconcile_session
                    # Keep lightweight one-argument overrides compatible with
                    # the long-standing tick seam.  The production method
                    # receives the prefetched snapshot and protection keys;
                    # an override owns its own snapshot lifecycle.
                    if (
                        getattr(reconcile, "__func__", None)
                        is PolymarketLPService._reconcile_session
                    ):
                        payloads.append(
                            reconcile(
                                current,
                                prefetched_snapshot=snapshot,
                                protection_skip_keys=protected_levels,
                            )
                        )
                    else:
                        payloads.append(reconcile(current))
            except Exception as exc:
                # One group's reconciliation failure must not swallow the
                # others: record the failure into that group's payload and
                # continue with the remaining groups.
                payloads.append(
                    {
                        "state": "error",
                        "session_id": session_id,
                        "error": type(exc).__name__,
                    }
                )
            finally:
                if apply_handle is not None and apply_lock is not None:
                    apply_lock[1](apply_handle)
        return self._lp_tick_aggregate(payloads)

    def _apply_triggered_protection_cancel_off_lock(
        self,
        session: Mapping[str, object],
        snapshot: Mapping[str, object],
    ) -> tuple[set[str], int]:
        """Issue only triggered protection cancel actions outside the locks.

        Monitoring fields, convergence, outage counters, and every other
        session update stay in the serialized apply section.  This lane only
        sends a durable cancel intent for an already-triggered bucket (or a
        failed cancel retry) and merges that one bucket transactionally.
        """

        protection = session.get("queue_protection")
        buckets = self._queue_protection_levels(session)
        if not isinstance(protection, Mapping) or not buckets:
            return set(), 0
        # A confirmed BUY fill is collected by the serialized reconciliation
        # lane before queue protection is evaluated.  Let that lane mark the
        # buckets as group_fill_collect instead of racing a queue-ahead cancel
        # when the session image is still current.  A concurrent augment makes
        # the snapshot stale; its protection cancel remains independently
        # actionable while the revision fence discards stale fill facts.
        current_session = self.store.lp_session(str(session["session_id"]))
        concurrent_group_change = (
            isinstance(current_session, Mapping)
            and set(self._session_order_ids(current_session))
            != set(self._session_order_ids(session))
        )
        if (
            self._snapshot_has_group_buy_fill(session, snapshot)
            and not concurrent_group_change
        ):
            return set(), 0
        rows_by_id = self._queue_level_rows(snapshot)
        session_id = str(session["session_id"])
        merger = getattr(self.store, "lp_merge_queue_protection_bucket", None)
        if not callable(merger):
            merger = getattr(self.store, "lp_merge_queue_protection", None)
        if not callable(merger):
            return set(), 0

        protected_levels: set[str] = set()
        writes = 0
        for key, bucket in buckets.items():
            state = str(bucket.get("state") or "")
            only_order_ids: list[str] | None = None
            if state == "canceling":
                only_order_ids = [
                    str(value)
                    for value in _items(bucket.get("cancel_failed"))
                    if str(value or "")
                ]
                retryable: list[str] = []
                for order_id in only_order_ids:
                    row = rows_by_id.get(order_id)
                    if row is not None:
                        status = str(_field(row, "status", "") or "").upper()
                        matched = _maybe_decimal(_field(row, "size_matched"))
                        if status in TERMINAL_ORDER_STATES or (
                            matched is not None and matched > 0
                        ):
                            continue
                    retryable.append(order_id)
                only_order_ids = retryable
                if not only_order_ids:
                    continue
                next_bucket, patch = self._request_bucket_protection_cancel(
                    session,
                    snapshot,
                    bucket,
                    reason=str(bucket.get("cancel_reason") or "queue_ahead_ratio"),
                    only_order_ids=only_order_ids,
                    notify_blocked=False,
                )
            elif state == "triggered":
                next_bucket, patch = self._request_bucket_protection_cancel(
                    session,
                    snapshot,
                    bucket,
                    reason="queue_ahead_ratio",
                    notify_blocked=False,
                )
            else:
                evaluation = self._bucket_queue_protection_evaluation(
                    session, bucket, snapshot, rows_by_id
                )
                if evaluation is None or str(evaluation.get("state")) != "triggered":
                    continue
                next_bucket, patch = self._request_bucket_protection_cancel(
                    session,
                    snapshot,
                    evaluation,
                    reason="queue_ahead_ratio",
                    notify_blocked=False,
                )

            if str(next_bucket.get("state") or "") not in {
                "canceling",
                "canceled",
                "partially_filled",
            }:
                # Blocked/unknown protection state is ordinary monitoring
                # state and must be persisted by the serialized apply.
                continue
            if hasattr(self.store, "lp_merge_queue_protection_bucket"):
                merger(
                    session_id,
                    level_key=key,
                    bucket=next_bucket,
                    patch=patch,
                )
            else:
                merger(
                    session_id,
                    queue_protection={
                        "version": 2,
                        "levels": {key: next_bucket},
                    },
                    patch=patch,
                )
            protected_levels.add(key)
            writes += 1
        return protected_levels, writes

    def _snapshot_has_group_buy_fill(
        self,
        session: Mapping[str, object],
        snapshot: Mapping[str, object],
    ) -> bool:
        """Return whether the snapshot proves a fill for this group's BUY."""

        token_id = str(session.get("token_id") or "")
        for order_id in self._session_order_ids(session):
            if not order_id:
                continue
            try:
                quantity, _ = self._trade_totals(
                    snapshot,
                    order_id,
                    "BUY",
                    token_id=token_id or None,
                )
            except ValueError:
                # An uncertain trade read belongs to the serialized
                # reconciliation path; do not issue a speculative cancel.
                return True
            if quantity > 0:
                return True
        return False

    @staticmethod
    def _lp_tick_aggregate(
        payloads: list[dict[str, object]],
    ) -> dict[str, object]:
        """Build the tick report per the issue 166 aggregation contract."""

        if len(payloads) == 1:
            return {**payloads[0], "sessions": list(payloads)}
        oldest: dict[str, object] = {}
        for key in ("account_checked_at", "book_checked_at"):
            oldest[key] = PolymarketLPService._oldest_stamp(
                [payload.get(key) for payload in payloads]
            )
        states = {
            str(payload.get("state") or "") for payload in payloads
        }
        aggregate_state = "ok"
        for worst in ("needs_attention", "busy", "error", "failed"):
            if worst in states:
                aggregate_state = worst
                break
        return {
            "state": aggregate_state,
            "session_id": None,
            "sessions": list(payloads),
            "account_checked_at": oldest["account_checked_at"],
            "book_checked_at": oldest["book_checked_at"],
        }

    @staticmethod
    def _oldest_stamp(values: list[object]) -> object:
        """Return the oldest parseable stamp; None when none is parseable."""

        best: tuple[datetime, object] | None = None
        for value in values:
            if value is None:
                continue
            try:
                moment = _timestamp(value, name="stamp")
            except ValueError:
                continue
            if best is None or moment < best[0]:
                best = (moment, value)
        return best[1] if best is not None else None

    def _reconcile_session(
        self,
        session: Mapping[str, object],
        *,
        prefetched_snapshot: Mapping[str, object] | None = None,
        protection_skip_keys: Collection[str] = (),
    ) -> dict[str, object]:
        """Run one group-level monitoring/reconciliation iteration.

        A tick may supply the main snapshot fetched outside ``_mutex``.  The
        remaining account/fill/scoring work stays in the serialized apply
        section; already-triggered bucket cancels are skipped there so they
        cannot be sent twice by the same tick.
        """

        state = str(session.get("state"))
        if state in {"entry_rejected", "complete"}:
            return self._status_payload(session)
        try:
            request = self._normalize_request(session)
            snapshot = (
                prefetched_snapshot
                if prefetched_snapshot is not None
                else self._read_snapshot(request)
            )
        except ValueError as exc:
            return self._handle_snapshot_failure(session, exc)
        try:
            patch = self._fill_patch(session, snapshot)
        except ValueError as exc:
            outage_patch: dict[str, object] = {
                "reconciliation": str(exc),
                "resume_state": state
                if state != "needs_attention"
                else session.get("resume_state"),
            }
            failures = self._queue_protection_data_failure(session, str(exc))
            if failures is not None:
                outage_patch["queue_protection"] = failures
            outage_patch.update(
                self._needs_attention_notify_patch(
                    session, self._now(), protection=failures
                )
            )
            updated = self.store.lp_update_session(
                str(session["session_id"]),
                state="needs_attention",
                patch=outage_patch,
            )
            conservative = self._conservative_protection_cancel(updated, failures)
            if conservative is not None:
                return self._status_payload(conservative)
            return self._status_payload(updated)
        ownership_reason = self._unowned_target_order_reason(session, snapshot)
        if ownership_reason is not None:
            resume_state = (
                str(session.get("resume_state") or "")
                if state == "needs_attention"
                else state
            ) or "entry_open"
            patch.update(
                {
                    "reconciliation": ownership_reason,
                    "resume_state": resume_state,
                }
            )
            patch.update(
                self._needs_attention_notify_patch(
                    session,
                    self._now(),
                    protection=(
                        patch["queue_protection"]
                        if isinstance(patch.get("queue_protection"), Mapping)
                        else None
                    ),
                )
            )
            updated = self.store.lp_update_session(
                str(session["session_id"]),
                state="needs_attention",
                patch=patch,
            )
            return self._status_payload(updated)
        updated = self.store.lp_update_session(str(session["session_id"]), patch=patch)
        session = updated
        try:
            session = self._sync_order_history(session, snapshot)
        except ValueError as exc:
            updated = self.store.lp_update_session(
                str(session["session_id"]),
                state="needs_attention",
                patch={
                    "reconciliation": str(exc),
                    "resume_state": state
                    if state != "needs_attention"
                    else session.get("resume_state"),
                    **self._needs_attention_notify_patch(session, self._now()),
                },
            )
            return self._status_payload(updated)
        if state == "needs_attention":
            resume_state = str(session.get("resume_state") or "")
            if not resume_state:
                resume_state = "review" if session.get("stop_requested") else "entry_open"
            if resume_state not in {"entry_open", "passive_exit", "stop_loss_exit", "review"}:
                resume_state = "review"
            session = self.store.lp_update_session(
                str(session["session_id"]),
                state=resume_state,
                patch={
                    "reconciliation": None,
                    "resume_state": None,
                    "needs_attention_since": None,
                    "needs_attention_notified": False,
                },
            )
            state = resume_state
        # Issue 152: queue protection runs inside the existing one-second
        # monitor tick, before any exit reconciliation can mutate orders.
        session = self._apply_queue_protection(
            session,
            snapshot,
            skip_cancel_keys=protection_skip_keys,
        )
        session = self._reconcile_protected_exit(session, snapshot)
        if _decimal(session.get("buy_filled_quantity", 0), "buy_filled_quantity") > 0:
            # Issue 167 D3: a fill at any level collects the whole group's
            # own BUYs; economics stay group-merged.
            session = self._collect_group_buys(session, snapshot)
            if not self._group_buys_terminal(session, snapshot):
                return self._status_payload(session)
        if state == "review":
            self._update_scoring(session, snapshot)
            session = self.store.lp_session(str(session["session_id"])) or session
            return self._review_iteration(session, snapshot)
        self._update_scoring(session, snapshot)
        session = self.store.lp_session(str(session["session_id"])) or session
        if str(session.get("state")) == "review":
            return self._review_iteration(session, snapshot)
        now = self._now()
        review_at = _timestamp(session["review_at"], name="review_at")
        residual = _decimal(session.get("residual_quantity", 0), "residual_quantity")
        loss = self._opening_loss_from_session(session)
        session = self.store.lp_update_session(
            str(session["session_id"]),
            patch={"opening_loss": loss},
        )
        # A missing bid, fee, or position reconciliation must not prevent
        # the absolute review deadline from cancelling known quotes.
        if now >= review_at:
            try:
                self._cancel_owned_orders(session)
            except Exception as exc:
                updated = self.store.lp_update_session(
                    str(session["session_id"]),
                    state="needs_attention",
                    patch={
                        "reconciliation": f"deadline_cancel_{type(exc).__name__}",
                        "resume_state": "review",
                        "review_status": "awaiting_reconciliation",
                    },
                )
                return self._status_payload(updated)
            # Issue 167: the deadline sweep cancels every own BUY; the price
            # buckets follow into canceling so receipts settle them.
            session = self.store.lp_session(str(session["session_id"])) or session
            session = self._mark_group_buckets_canceling(session, "review_deadline")
            updated = self.store.lp_update_session(
                str(session["session_id"]),
                state="review",
                patch={"review_status": "awaiting_reconciliation"},
            )
            return self._status_payload(updated)
        if loss is not None and loss >= STOP_LOSS:
            session = self.store.lp_update_session(
                str(session["session_id"]),
                state="stop_loss_exit",
                patch=self._stop_loss_latch_patch(session, loss),
            )
        session = self.store.lp_session(str(session["session_id"])) or session
        if bool(session.get("stop_loss_latched")) or str(session.get("state")) == "stop_loss_exit":
            if str(session.get("state")) != "stop_loss_exit":
                session = self.store.lp_update_session(
                    str(session["session_id"]),
                    state="stop_loss_exit",
                    patch={"stop_loss_latched": True},
                )
            session = self._request_passive_cancel(session)
            session = self.store.lp_session(str(session["session_id"])) or session
            passive_id = str(session.get("passive_exit_order_id") or "")
            if passive_id and not self._order_terminal(snapshot, passive_id, session):
                return self._status_payload(session)
            residual = _decimal(session.get("residual_quantity", 0), "residual_quantity")
            if residual > 0 and not session.get("protected_exit_order_id"):
                self._submit_protected_exit(session, residual, snapshot)
                session = self.store.lp_session(str(session["session_id"])) or session
            return self._complete_if_flat(session, snapshot)
        if loss is None:
            return self._complete_if_flat(session, snapshot)
        if residual > 0:
            self._ensure_passive_exit(session, snapshot, residual)
            session = self.store.lp_session(str(session["session_id"])) or session
        return self._complete_if_flat(session, snapshot)

    def _handle_snapshot_failure(
        self, session: Mapping[str, object], exc: ValueError
    ) -> dict[str, object]:
        """Persist one failed main snapshot read in serialized apply."""

        state = str(session.get("state"))
        patch: dict[str, object] = {
            "reconciliation": str(exc),
            "resume_state": state
            if state != "needs_attention"
            else session.get("resume_state"),
        }
        failures = self._queue_protection_data_failure(session, str(exc))
        if failures is not None:
            patch["queue_protection"] = failures
        patch.update(
            self._needs_attention_notify_patch(
                session, self._now(), protection=failures
            )
        )
        updated = self.store.lp_update_session(
            str(session["session_id"]),
            state="needs_attention",
            patch=patch,
        )
        conservative = self._conservative_protection_cancel(updated, failures)
        if conservative is not None:
            return self._status_payload(conservative)
        return self._status_payload(updated)

    def _collect_group_buys(
        self, session: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> dict[str, object]:
        """Issue 167 D3: after any fill, cancel the group's every own BUY.

        Reuses ``_cancel_owned_orders`` (entry + augments).  Buckets whose
        order the sweep cancel-requests are marked ``canceling`` with reason
        ``group_fill_collect`` so the receipt convergence settles them; the
        D5 protection notifications deliberately stay silent for a group
        collection (no protection trigger fired).
        """

        history = self._order_history(session)
        actionable = False
        for order_id in self._session_augment_own_order_ids(session):
            if self._order_terminal(snapshot, order_id, session):
                continue
            if self._order_cancel_requested(session, order_id):
                continue
            actionable = True
            break
        if not actionable:
            return session
        try:
            self._cancel_owned_orders(session)
        except Exception as exc:
            return self.store.lp_update_session(
                str(session["session_id"]),
                state="needs_attention",
                patch={
                    "reconciliation": f"group_collect_{type(exc).__name__}",
                    "resume_state": str(session.get("state") or "entry_open"),
                },
            )
        session = self.store.lp_session(str(session["session_id"])) or session
        return self._mark_group_buckets_canceling(session, "group_fill_collect")

    def _mark_group_buckets_canceling(
        self,
        session: Mapping[str, object],
        reason: str,
    ) -> dict[str, object]:
        """Mark every cancel-requested bucket ``canceling`` with this reason.

        The D5 protection notifications deliberately stay silent for these
        sweeps (a group collection or the review deadline is not a
        protection trigger), so ``notification_sent`` is preset and the
        receipt convergence settles the buckets without announcing.
        """

        protection = session.get("queue_protection")
        buckets = self._queue_protection_levels(session)
        if not isinstance(protection, Mapping) or not buckets:
            return session
        changed = False
        new_levels: dict[str, dict[str, object]] = {}
        for key, bucket in buckets.items():
            if str(bucket.get("state")) in {"canceling", "canceled", "partially_filled"}:
                new_levels[key] = bucket
                continue
            order_id = str(bucket.get("order_id") or "")
            if order_id and self._order_cancel_requested(session, order_id):
                bucket = {
                    **bucket,
                    "state": "canceling",
                    "cancel_reason": reason,
                    "cancel_targets": [order_id],
                    "cancel_requested_at": _iso(self._now()),
                    "notification_sent": True,
                }
                changed = True
            new_levels[key] = bucket
        if not changed:
            return session
        return self.store.lp_update_session(
            str(session["session_id"]),
            patch={
                "queue_protection": {
                    "version": 2,
                    "data_failures": _queue_group_failures_int(
                        protection.get("data_failures")
                    ),
                    "levels": new_levels,
                }
            },
        )

    def _group_buys_terminal(
        self, session: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> bool:
        """True when every own BUY of the group shows a terminal receipt."""

        for order_id in self._session_augment_own_order_ids(session):
            if not self._order_terminal(snapshot, order_id, session):
                return False
        return True

    # Public math is intentionally not used by the approved behavior cases;
    # the lifecycle calls this private helper after reading external facts.
    def _opening_loss_from_session(self, session: Mapping[str, object]) -> Decimal | None:
        if session.get("position_reconciled") is not True:
            return None
        cost = _maybe_decimal(session.get("buy_cost"))
        proceeds = _maybe_decimal(session.get("sold_revenue"))
        residual = _maybe_decimal(session.get("residual_exit_value"))
        fees = _maybe_decimal(session.get("fees"))
        if cost is None or proceeds is None or residual is None or fees is None:
            return None
        return cost - proceeds - residual + fees

    def _stop_loss_latch_patch(
        self, session: Mapping[str, object], loss: Decimal | None
    ) -> dict[str, object]:
        patch: dict[str, object] = {"stop_loss_latched": True}
        if (
            loss is not None
            and loss >= STOP_LOSS
            and session.get("stop_loss_triggered_at") is None
            and session.get("stop_loss_triggered_loss") is None
        ):
            patch["stop_loss_triggered_at"] = _iso(self._now())
            patch["stop_loss_triggered_loss"] = loss
        return patch

    def _normalize_request(self, request: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(request, Mapping):
            raise ValueError("request_invalid")
        result = dict(request)
        for key in ("market_id", "condition_id", "token_id", "outcome"):
            value = _text(result.get(key))
            if value is None:
                raise ValueError(f"{key}_invalid")
            result[key] = value
        if result["outcome"] not in {"YES", "NO"}:
            raise ValueError("outcome_invalid")
        for key in ("price", "quantity"):
            result[key] = _decimal(result.get(key), key)
        if cast(Decimal, result["price"]) <= 0 or cast(Decimal, result["quantity"]) <= 0:
            raise ValueError("quantity_or_price_invalid")
        result["review_at"] = _timestamp(result.get("review_at"), name="review_at")
        return result

    def _read_snapshot(self, request: Mapping[str, object]) -> Mapping[str, object]:
        for name in ("lp_snapshot", "snapshot"):
            method = getattr(self.exchange, name, None)
            if not callable(method):
                continue
            try:
                value = method(dict(request))
            except TypeError:
                try:
                    value = method()
                except Exception as exc:
                    raise ValueError("external_snapshot_unknown") from exc
            except Exception as exc:
                raise ValueError("external_snapshot_unknown") from exc
            if isinstance(value, Mapping):
                return value
        raise ValueError("external_snapshot_unknown")

    @classmethod
    def _validate_snapshot(
        cls,
        request: Mapping[str, object],
        snapshot: Mapping[str, object],
        *,
        now: datetime,
        allowed_open_order_ids: Collection[str] | None = None,
        reservations: object = None,
    ) -> dict[str, object]:
        account = snapshot.get("account")
        if not isinstance(account, Mapping):
            raise ValueError("account_unknown")
        authenticated = account.get("authenticated", account.get("auth"))
        if authenticated is None:
            raise ValueError("account_unknown")
        if authenticated is not True:
            raise ValueError("account_invalid")
        if snapshot.get("external_inventory") is True:
            raise ValueError("target_inventory")
        if snapshot.get("external_orders") is True:
            raise ValueError("target_orders")
        positions = _items(account.get("positions"))
        for position in positions:
            token = _field(position, "token_id", _field(position, "asset_id"))
            size = _maybe_decimal(_field(position, "size", _field(position, "quantity", 0)))
            if token == request["token_id"] and size is not None and size > 0:
                raise ValueError("target_inventory")
        # Issue 158: an augment re-check must tolerate the session's own
        # resting orders (entry + earlier augments); every other order on the
        # token still rejects exactly as before.
        allowed_ids = {
            str(value or "") for value in (allowed_open_order_ids or ())
        }
        for order in _items(account.get("open_orders")):
            if cls._order_id(order) and cls._order_id(order) in allowed_ids:
                continue
            token = _field(order, "token_id", _field(order, "asset_id"))
            market = _field(order, "market_id", _field(order, "market"))
            if token == request["token_id"] or market == request["market_id"]:
                raise ValueError("target_orders")
        balance = account.get("balance")
        allowance = account.get("allowance")
        if balance is None:
            raise ValueError("balance_unknown")
        if allowance is None:
            raise ValueError("allowance_unknown")
        balance_d = _decimal(balance, "balance")
        allowance_d = _decimal(allowance, "allowance")
        # Issue 166: submission competes against the balance/allowance left
        # after every active group's resting BUY reservation (dedup by order
        # id inside _account_after_reservations), not the raw wallet fact.
        # When the occupancy itself cannot be computed the honest answer is
        # "facts unknown", never an unadjusted pass.
        if reservations is not None:
            available_account = _account_after_reservations(account, reservations)
            if available_account is None:
                raise ValueError("account_facts_unknown")
            available_balance = _decimal(
                available_account.get("balance"), "balance"
            )
            available_allowance = _decimal(
                available_account.get("allowance"), "allowance"
            )
        else:
            available_balance = balance_d
            available_allowance = allowance_d
        market = snapshot.get("market")
        if not isinstance(market, Mapping):
            raise ValueError("market_unknown")
        for key in ("market_id", "condition_id", "token_id", "outcome"):
            if market.get(key) is None:
                raise ValueError("market_identity_unknown")
            if str(market.get(key)) != str(request[key]):
                raise ValueError("market_identity_mismatch")
        accepting = market.get("accepting_orders")
        if accepting is None:
            raise ValueError("market_acceptance_unknown")
        if accepting is not True:
            raise ValueError("market_not_accepting")
        exchange_type = market.get("exchange_type")
        if exchange_type is None:
            raise ValueError("exchange_unknown")
        if str(exchange_type).upper() not in {"CLOB", "POLYMARKET"}:
            raise ValueError("exchange_unknown")
        tick = market.get("tick_size")
        minimum = market.get("minimum_order_size")
        reward_min = market.get("reward_min_size")
        reward_spread = market.get("reward_max_spread")
        fee = market.get("fee")
        taker_fee_rate = market.get("taker_fee_rate", market.get("fee_rate"))
        fees_enabled = market.get("fees_enabled")
        if tick is None:
            raise ValueError("tick_size_unknown")
        if minimum is None:
            raise ValueError("minimum_order_size_unknown")
        if reward_min is None:
            raise ValueError("reward_min_size_unknown")
        if reward_spread is None:
            raise ValueError("reward_distance_unknown")
        if fee is None and fees_enabled is not False:
            raise ValueError("fee_unknown")
        if taker_fee_rate is None and fees_enabled is not False:
            raise ValueError("fee_rate_unknown")
        tick_d = _decimal(tick, "tick_size")
        minimum_d = _decimal(minimum, "minimum_order_size")
        reward_min_d = _decimal(reward_min, "reward_min_size")
        reward_spread_d = _decimal(reward_spread, "reward_max_spread")
        fee_d = Decimal("0") if fees_enabled is False else _decimal(fee, "fee")
        taker_fee_rate_d = (
            Decimal("0")
            if fees_enabled is False
            else _decimal(taker_fee_rate, "taker_fee_rate")
        )
        if (
            tick_d <= 0
            or minimum_d <= 0
            or reward_min_d <= 0
            or reward_spread_d <= 0
            or fee_d < 0
            or taker_fee_rate_d < 0
        ):
            raise ValueError("market_rule_invalid")
        price = cast(Decimal, request["price"])
        quantity = cast(Decimal, request["quantity"])
        if price % tick_d != 0:
            raise ValueError("price_off_tick")
        if quantity < minimum_d or quantity < reward_min_d:
            raise ValueError("order_size_invalid")
        if (
            available_balance < 0
            or available_allowance < 0
            or available_balance < price * quantity
            or available_allowance < price * quantity
        ):
            raise ValueError("balance_insufficient")
        book = snapshot.get("book")
        if not isinstance(book, Mapping):
            raise ValueError("book_unknown")
        candidate_policy = request.get("candidate_policy")
        if candidate_policy not in (None, "best_bid_minimum"):
            raise ValueError("candidate_policy_invalid")
        stamp = book.get("received_at")
        if stamp is None:
            raise ValueError("book_freshness_unknown")
        _freshness(
            stamp,
            now,
            "book_freshness",
            max_age=60 if candidate_policy == "best_bid_minimum" else BOOK_FRESHNESS_SECONDS,
        )
        asks = cls._levels(book.get("asks"), "asks")
        bids = cls._levels(book.get("bids"), "bids")
        if not asks or not bids:
            raise ValueError("book_invalid")
        if candidate_policy == "best_bid_minimum":
            external_best_bid = max(level_price for level_price, _ in bids)
            if price != external_best_bid:
                raise ValueError("candidate_best_bid_changed")
        bid, ask, midpoint = _qualify_reward_quote(
            bids,
            asks,
            price=price,
            reward_min_size=reward_min_d,
            reward_max_spread=reward_spread_d,
            require_positive_score=candidate_policy == "best_bid_minimum",
            cumulative_depth=candidate_policy == "best_bid_minimum",
        )
        if sum(size for _, size in bids) < quantity:
            raise ValueError("exit_liquidity_insufficient")
        review_at = _timestamp(request["review_at"], name="review_at")
        expiration = expiration_for_review(review_at, now=now)
        return {
            "tick_size": tick_d,
            "minimum_order_size": minimum_d,
            "reward_min_size": reward_min_d,
            "reward_max_spread": reward_spread_d,
            "fee": fee_d,
            "taker_fee_rate": taker_fee_rate_d,
            "balance": balance_d,
            "allowance": allowance_d,
            "midpoint": midpoint,
            "best_bid": bid[0],
            "best_ask": ask[0],
            "midpoint_source": "local_size_filtered_estimate",
            "checked_at": now,
            "entry_expiration": expiration,
        }

    @staticmethod
    def _levels(value: object, name: str) -> list[tuple[Decimal, Decimal]]:
        return _levels(value, name)

    @staticmethod
    def _order_id(value: object) -> str:
        return str(_field(value, "order_id", _field(value, "id", "")) or "")

    @staticmethod
    def _queue_level_rows(
        snapshot: Mapping[str, object] | None,
    ) -> dict[str, object]:
        """Merge order receipts over the account open-order projection."""

        rows_by_id: dict[str, object] = {}
        if not isinstance(snapshot, Mapping):
            return rows_by_id
        for order in _items(snapshot.get("orders")):
            order_id = PolymarketLPService._order_id(order)
            if order_id:
                rows_by_id.setdefault(order_id, order)
        account = snapshot.get("account")
        open_orders = (
            account.get("open_orders") if isinstance(account, Mapping) else ()
        )
        for order in _items(open_orders):
            order_id = PolymarketLPService._order_id(order)
            if order_id:
                rows_by_id.setdefault(order_id, order)
        return rows_by_id

    @staticmethod
    def _queue_row_remaining(row: object) -> Decimal | None:
        for name in ("remaining_size", "remaining_quantity", "size"):
            value = _maybe_decimal(_field(row, name))
            if value is not None:
                return value
        original = _maybe_decimal(
            _field(row, "original_size", _field(row, "quantity"))
        )
        matched = _maybe_decimal(_field(row, "size_matched"))
        if original is not None and matched is not None:
            return original - matched
        return None

    def _queue_account_open_orders(self) -> list[object] | None:
        """Read open orders through one fresh account read (issue 152 D5)."""

        for name in ("lp_account_snapshot", "account_snapshot"):
            reader = getattr(self.exchange, name, None)
            if not callable(reader):
                continue
            try:
                value = reader()
            except Exception:
                return None
            rows: object = ()
            if isinstance(value, Mapping):
                rows = value.get("open_orders")
            else:
                rows = getattr(value, "open_orders", None)
            if rows is None:
                continue
            return list(_items(rows))
        return None

    def _own_queue_remaining(
        self,
        snapshot: Mapping[str, object],
        *,
        token_id: str,
        price: Decimal,
    ) -> Decimal | None:
        """Sum remaining size of own open BUY orders at one token and price.

        Receipt rows (``snapshot["orders"]``) take priority over the account
        ``open_orders`` projection for the same order id.  Any participating
        row without a parseable remaining size poisons the total: the caller
        receives None and the estimate stays UNKNOWN instead of guessing.
        """

        total = Decimal("0")
        for row in self._queue_level_rows(snapshot).values():
            row_token = _field(row, "token_id", _field(row, "asset_id"))
            if row_token not in (None, "", token_id):
                continue
            if str(_field(row, "side", "")).upper() != "BUY":
                continue
            if _maybe_decimal(_field(row, "price")) != price:
                continue
            if str(_field(row, "status", "")).upper() in TERMINAL_ORDER_STATES:
                continue
            remaining = self._queue_row_remaining(row)
            if remaining is None:
                return None
            total += remaining
        return total

    def _bucket_queue_protection_evaluation(
        self,
        session: Mapping[str, object],
        bucket: Mapping[str, object],
        snapshot: Mapping[str, object],
        rows_by_id: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Return the per-bucket queue-protection patch for this tick, or None
        when the bucket stays unchanged."""

        if str(bucket.get("state")) in {"canceling", "canceled", "partially_filled"}:
            return None
        baseline_price = _maybe_decimal(bucket.get("baseline_price"))
        if baseline_price is None:
            return None
        if not self._bucket_protection_gate_open(session, bucket, rows_by_id):
            return None
        token_id = str(session.get("token_id") or "")
        own_remaining = self._own_queue_remaining_rows(
            rows_by_id, token_id=token_id, price=baseline_price
        )
        estimate = estimate_lp_queue_position(
            snapshot.get("book"),
            price=baseline_price,
            own_remaining=own_remaining,
            baseline_front=_maybe_decimal(bucket.get("baseline_front"))
            or Decimal("0"),
            threshold=_maybe_decimal(bucket.get("threshold"))
            or LP_QUEUE_PROTECTION_THRESHOLD,
            condition_id=str(session.get("condition_id") or "") or None,
            token_id=token_id or None,
        )
        updated = dict(bucket)
        updated.update(
            {
                "state": estimate["state"],
                "front_estimate": estimate["front_estimate"],
                "level_total": estimate["level_total"],
                "ratio": estimate["ratio"],
                "reason_codes": estimate["reason_codes"],
                "data_time": estimate["data_time"],
            }
        )
        return updated

    def _notify_protection(
        self, title: str, message: str, xiaoai_text: str
    ) -> None:
        """Deliver one protection notification through both channels."""

        callback = self._protection_notifier
        if callback is None:
            return
        try:
            callback(title, message, xiaoai_text)
        except Exception:
            pass

    @staticmethod
    def _needs_attention_reason_copy(
        reconciliation: object,
    ) -> tuple[str, str]:
        """「需要核对」文案表（与前端 lpNeedsAttentionCopy 同源同句）。

        返回 (整句 main, 小爱原因短句)。
        """

        code = str(reconciliation or "")
        if code == "unowned_target_order":
            return (
                "账户里有一张挂在本市场、但不归本组管理的单（常见：手工挂的单）。"
                "系统已暂停本组自动管理，追加暂不可用；那张单撤掉或成交后自动恢复，无需操作。",
                "本市场有不归系统管理的挂单",
            )
        if code in {"external_snapshot_unknown", "book_unknown", "book_freshness_unknown"}:
            return ("市场/账户数据连续读取失败，系统自动重试中。", "数据读取连续失败")
        if "submit_unknown" in code:
            return (
                "一笔提交结果未知，需要到 Polymarket 订单页核对该单状态。",
                "有提交结果未知",
            )
        if code.startswith(("stop_cancel_", "deadline_cancel_", "group_collect_")):
            return ("一次撤单操作失败，系统自动重试中，长时间未恢复需人工核对。", "撤单操作失败")
        fallback = code or "unknown"
        return (f"系统暂停了本组的自动管理（原因：{fallback}）。", f"原因 {fallback}")

    def _needs_attention_notify_patch(
        self,
        session: Mapping[str, object],
        now: datetime,
        *,
        protection: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """克制的「需要核对」通知记账 patch（复用于四个巡检写分支）。

        进入（会话此前不在该状态或缺 since 键）→ 锚定 since、notified=False；
        已在状态 → 不动 since（reason 变化也算同一 episode，不重置不重推）；
        仍未自愈且距进入满 5 分钟且未推过 → 经 _notify_protection 推一条并把
        needs_attention_notified=True 随本次 patch 落库；发送异常由
        _notify_protection 吞掉（现有行为）。恢复路径负责清键重置。
        """

        state = str(session.get("state"))
        since_raw = session.get("needs_attention_since")
        if state != "needs_attention" or since_raw is None:
            return {
                "needs_attention_since": now.isoformat(),
                "needs_attention_notified": False,
            }
        if bool(session.get("needs_attention_notified")):
            return {}
        try:
            since = _timestamp(since_raw, name="needs_attention_since")
        except ValueError:
            return {
                "needs_attention_since": now.isoformat(),
                "needs_attention_notified": False,
            }
        if (now - since).total_seconds() < LP_NEEDS_ATTENTION_NOTIFY_SECONDS:
            return {}
        view = protection if isinstance(protection, Mapping) else None
        if view is None:
            raw = session.get("queue_protection")
            view = raw if isinstance(raw, Mapping) else {}
        failures = _queue_group_failures_int(view.get("data_failures"))
        main, xiaoai_reason = self._needs_attention_reason_copy(
            session.get("reconciliation")
        )
        identity = _text(session.get("market_title")) or str(
            session.get("condition_id") or ""
        )[:12]
        message = main + (
            f" 数据读取失败 {failures}/10，满 10 次将保护性撤单。"
            if failures > 0
            else ""
        )
        self._notify_protection(
            f"LP 需要核对 · {identity}",
            message,
            f"LP 需要核对，{xiaoai_reason}",
        )
        return {"needs_attention_notified": True}

    def _queue_protection_identity(
        self, session: Mapping[str, object]
    ) -> dict[str, str]:
        """Resolve notification identity from the durable payload or local cache."""

        condition_id = str(session.get("condition_id") or "").strip()
        token_id = str(session.get("token_id") or "").strip()
        title = _text(session.get("market_title")) or _text(session.get("question"))
        market_url = _text(session.get("market_url"))
        outcome = _text(session.get("outcome"))
        if not title or not market_url or not outcome:
            try:
                entries_reader = getattr(self.store, "lp_metadata_cache_entries", None)
                entries = (
                    entries_reader(now=self._now())
                    if callable(entries_reader)
                    else {}
                )
                entry = entries.get(condition_id) if isinstance(entries, Mapping) else None
                cached = (
                    entry[1]
                    if isinstance(entry, tuple) and len(entry) == 2
                    else None
                )
                if isinstance(cached, Mapping):
                    title = title or _text(cached.get("market_title")) or _text(
                        cached.get("question")
                    ) or _text(cached.get("title"))
                    market_url = market_url or _text(cached.get("market_url"))
                    outcome = outcome or _text(cached.get("outcome"))
                    outcomes = cached.get("outcomes")
                    if not outcome and isinstance(outcomes, Mapping):
                        for raw_outcome in outcomes.values():
                            if not isinstance(raw_outcome, Mapping):
                                continue
                            raw_token = str(raw_outcome.get("token_id") or "").strip()
                            if raw_token and raw_token == token_id:
                                outcome = _text(raw_outcome.get("label")) or _text(
                                    raw_outcome.get("outcome")
                                )
                                break
            except Exception:
                pass
        if not title:
            title = (
                f"未知市场（condition_id={condition_id or '未知'}; "
                f"token_id={token_id or '未知'}）"
            )
        if not outcome:
            outcome = f"选项未知（token_id={token_id or '未知'}）"
        return {
            "title": title,
            "url": market_url or "",
            "outcome": outcome,
            "condition_id": condition_id or "未知",
            "token_id": token_id or "未知",
        }

    def _queue_protection_market_title(
        self, session: Mapping[str, object]
    ) -> str:
        return self._queue_protection_identity(session)["title"]

    @staticmethod
    def _record_order_placement_times(
        protection: Mapping[str, object],
        rows_by_id: Mapping[str, object] | None,
        order_ids: Collection[str],
    ) -> dict[str, object]:
        """Carry valid venue placement stamps into the existing payload."""

        result: dict[str, object] = {}
        raw = protection.get("order_placement_times")
        if isinstance(raw, Mapping):
            result.update({str(key): value for key, value in raw.items() if str(key)})
        for order_id in order_ids:
            order_id = str(order_id or "")
            if not order_id:
                continue
            row = rows_by_id.get(order_id) if rows_by_id is not None else None
            raw_created_at = _field(row, "created_at") if row is not None else None
            if raw_created_at is None:
                result.setdefault(order_id, None)
                continue
            try:
                created_at = _timestamp(raw_created_at, name="venue_created_at")
            except ValueError:
                result.setdefault(order_id, None)
            else:
                result[order_id] = _iso(created_at)
        return result

    @staticmethod
    def _beijing_datetime_text(value: object) -> str | None:
        try:
            return _timestamp(value).astimezone(_BEIJING).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            return None

    @staticmethod
    def _lifetime_text(start: datetime, end: datetime) -> str | None:
        seconds = int((end - start).total_seconds())
        if seconds < 0:
            return None
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts: list[str] = []
        if hours:
            parts.append(f"{hours}小时")
        if minutes:
            parts.append(f"{minutes}分")
        if seconds or not parts:
            parts.append(f"{seconds}秒")
        return "".join(parts)

    def _queue_protection_lifecycle_lines(
        self,
        protection: Mapping[str, object],
        order_ids: Sequence[str],
        *,
        observed_at: object = None,
    ) -> list[str]:
        """Describe actual venue lifetime, or clearly label observation fallback."""

        placement_values = protection.get("order_placement_times")
        confirmation_values = protection.get("order_cancel_confirmed_at")
        placements: list[datetime] = []
        confirmations: list[datetime] = []
        if isinstance(placement_values, Mapping) and isinstance(
            confirmation_values, Mapping
        ):
            for order_id in order_ids:
                try:
                    placement = _timestamp(
                        placement_values.get(order_id), name="venue_created_at"
                    )
                    confirmation = _timestamp(
                        confirmation_values.get(order_id), name="cancel_confirmed_at"
                    )
                except (AttributeError, ValueError):
                    continue
                if placement > confirmation:
                    continue
                placements.append(placement)
                confirmations.append(confirmation)
        actual_known = bool(order_ids) and len(placements) == len(order_ids)
        lines: list[str] = []
        if actual_known and placements and confirmations:
            started = min(placements)
            completed = max(confirmations)
            lifetime = self._lifetime_text(started, completed)
            start_text = self._beijing_datetime_text(started)
            completed_text = self._beijing_datetime_text(completed)
            if lifetime is not None and start_text and completed_text:
                lines.extend(
                    [
                        f"首次挂单：北京时间 {start_text}",
                        f"撤单完成：北京时间 {completed_text}",
                        f"挂单存续：{lifetime}",
                    ]
                )
                return lines

        lines.append("首次挂单：未知（未取得有效的实际交易所下单时间）")
        completed: datetime | None = None
        if isinstance(confirmation_values, Mapping):
            valid_confirmations: list[datetime] = []
            for value in confirmation_values.values():
                try:
                    valid_confirmations.append(
                        _timestamp(value, name="cancel_confirmed_at")
                    )
                except (AttributeError, ValueError):
                    continue
            if valid_confirmations:
                completed = max(valid_confirmations)
                completed_text = self._beijing_datetime_text(completed)
                if completed_text:
                    lines.append(f"撤单完成：北京时间 {completed_text}")
        try:
            first_seen = _timestamp(protection.get("first_seen_at"), name="first_seen_at")
            observed_text = self._beijing_datetime_text(first_seen)
            observed_lifetime = (
                self._lifetime_text(first_seen, completed)
                if completed is not None
                else None
            )
            if observed_text:
                lines.append(f"首次观察：北京时间 {observed_text}")
            if observed_lifetime is not None:
                lines.append(f"观察到的存续时长：{observed_lifetime}")
        except (TypeError, ValueError):
            pass
        return lines

    @staticmethod
    def _queue_protection_order_ids(
        protection: Mapping[str, object],
    ) -> list[str]:
        values = protection.get("canceled_order_ids")
        return [str(value) for value in _items(values) if str(value or "")]

    def _queue_protection_success_notification(
        self,
        protection: Mapping[str, object],
        session: Mapping[str, object],
        *,
        canceled_count: int,
        manual_count: int,
        canceled_remaining: Decimal | None,
        title: str = "LP 位置保护撤单",
        trigger_prefix: str = "",
    ) -> tuple[str, str, str]:
        ratio = _maybe_decimal(protection.get("ratio"))
        cancel_reason = str(protection.get("cancel_reason") or "")
        price = _maybe_decimal(protection.get("baseline_price"))
        if cancel_reason == "book_unreliable":
            trigger = "行情数据中断超过 10 秒，进入保守撤单保护，因此已撤单"
        else:
            threshold = (
                _maybe_decimal(protection.get("threshold"))
                or LP_QUEUE_PROTECTION_THRESHOLD
            )
            front = _maybe_decimal(protection.get("front_estimate"))
            total = _maybe_decimal(protection.get("level_total"))
            trigger = (
                f"BUY 买单估计按份额排到 "
                f"{_queue_decimal_text(price)} 价位买单队列前约"
                f"{_queue_ratio_percent_text(ratio)}%的位置，进入“前"
                f"{_queue_ratio_percent_text(threshold)}%自动撤单”的保护范围"
                "（按份额估计，不是订单数，也不是精确名次），因此已撤单。\n"
                f"判断证据：A 比例 {_queue_ratio_percent_text(ratio)}% ≤ "
                f"{_queue_ratio_percent_text(threshold)}%"
                f"（前方≈{_queue_decimal_text(front)} / "
                f"同价位 {_queue_decimal_text(total)} 份）"
            )
        identity = self._queue_protection_identity(session)
        outcome = identity["outcome"]
        if outcome.upper() in {"YES", "NO"}:
            outcome = outcome.upper()
        identity_line = f"市场：{identity['title']}。"
        if identity["url"]:
            identity_line += f"\n链接：{identity['url']}。"
        identity_line += f"\n方向：BUY {outcome}；价格：{_queue_decimal_text(price)}。"
        order_ids = self._queue_protection_order_ids(protection)
        remaining_values = protection.get("cancel_target_remaining")
        order_parts: list[str] = []
        for order_id in order_ids:
            remaining = (
                _maybe_decimal(remaining_values.get(order_id))
                if isinstance(remaining_values, Mapping)
                else None
            )
            order_parts.append(f"{order_id}={_queue_decimal_text(remaining)}份")
        order_line = (
            "撤单订单（撤单时余量）：" + "；".join(order_parts) + "。"
            if order_parts
            else "撤单订单（撤单时余量）：未知。"
        )
        baseline_source = str(protection.get("baseline_source") or "")
        if baseline_source == "first_observation":
            source_line = "盘口基线：首次观察订单时建立的盘口基线。"
        elif baseline_source == "submit":
            source_line = "盘口基线：提交时基线。"
        else:
            source_line = "盘口基线：未知。"
        data_time = self._beijing_datetime_text(protection.get("data_time"))
        if data_time is None:
            data_time = beijing_clock(protection.get("data_time"), seconds=True)
        lifecycle = self._queue_protection_lifecycle_lines(protection, order_ids)
        message = (
            f"{identity_line}\n"
            f"触发：{trigger_prefix}{trigger}。\n"
            f"结果：已撤 {canceled_count} 张买单"
            f"合计余量 {_queue_decimal_text(canceled_remaining)} 份"
            f" @ {_queue_decimal_text(price)}"
            f"（含 {manual_count} 张手动）。\n"
            f"{order_line}\n"
            f"{source_line}\n"
            + "\n".join(lifecycle)
            + "\n"
            f"数据时间：北京时间 {data_time or '未知'}。"
        )
        return (
            title,
            message,
            f"{title}，{canceled_count} 张买单已撤",
        )

    def _queue_protection_blocked_notification(
        self,
        protection: Mapping[str, object],
        session: Mapping[str, object],
        failure_reason: str,
        remaining: Decimal | None,
        *,
        title: str = "LP 位置保护撤单受阻",
        trigger_prefix: str = "",
    ) -> tuple[str, str, str]:
        ratio = _maybe_decimal(protection.get("ratio"))
        price = _maybe_decimal(protection.get("baseline_price"))
        identity = self._queue_protection_identity(session)
        outcome = identity["outcome"]
        if outcome.upper() in {"YES", "NO"}:
            outcome = outcome.upper()
        identity_line = f"市场：{identity['title']}。"
        if identity["url"]:
            identity_line += f"\n链接：{identity['url']}。"
        identity_line += f"\n方向：BUY {outcome}；价格：{_queue_decimal_text(price)}。"
        cancel_reason = str(protection.get("cancel_reason") or "")
        if cancel_reason == "book_unreliable" or ratio is None:
            trigger = (
                "行情数据中断，无法可靠估计排队位置，进入保守撤单尝试"
            )
        else:
            threshold = (
                _maybe_decimal(protection.get("threshold"))
                or LP_QUEUE_PROTECTION_THRESHOLD
            )
            trigger = (
                f"{trigger_prefix}BUY 买单估计按份额排到 "
                f"{_queue_decimal_text(price)} 价位买单队列前约"
                f"{_queue_ratio_percent_text(ratio)}%的位置，进入“前"
                f"{_queue_ratio_percent_text(threshold)}%自动撤单”的保护范围"
                "（按份额估计，不是订单数，也不是精确名次）"
            )
        baseline_source = str(protection.get("baseline_source") or "")
        if baseline_source == "first_observation":
            source_line = "盘口基线：首次观察订单时建立的盘口基线。"
        elif baseline_source == "submit":
            source_line = "盘口基线：提交时基线。"
        else:
            source_line = "盘口基线：未知。"
        data_time = self._beijing_datetime_text(protection.get("data_time"))
        if data_time is None:
            data_time = beijing_clock(protection.get("data_time"), seconds=True)
        message = (
            f"{identity_line}\n"
            f"尝试保护：{trigger}。\n"
            f"撤单未成功：{failure_reason}，"
            f"残余 {_queue_decimal_text(remaining)} 份待处理。\n"
            f"{source_line}\n"
            f"数据时间：北京时间 {data_time or '未知'}。"
        )
        return (title, message, title)

    @staticmethod
    def _merge_order_id_lists(*groups: object) -> list[str]:
        """Union order ids across a protection episode (ordered, deduplicated)."""

        result: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for value in _items(group):
                order_id = str(value or "")
                if order_id and order_id not in seen:
                    seen.add(order_id)
                    result.append(order_id)
        return result

    @staticmethod
    def _episode_canceled_remaining(
        target_remaining: object, canceled_order_ids: list[str]
    ) -> Decimal | None:
        """Sum persisted cancel-time remaining over the episode's canceled set.

        Any canceled order without a persisted numeric remaining (unknown at
        request time, or persisted by an older build) makes the whole total
        UNKNOWN instead of under-reporting a partial sum (issue 152 review
        fix).
        """

        if not isinstance(target_remaining, Mapping):
            return None if canceled_order_ids else Decimal("0")
        total = Decimal("0")
        for order_id in canceled_order_ids:
            remaining = _maybe_decimal(target_remaining.get(str(order_id)))
            if remaining is None:
                return None
            total += remaining
        return total

    def _queue_protection_price_title(
        self, session: Mapping[str, object], bucket: Mapping[str, object], base: str
    ) -> str:
        """Issue 167 D5: per-bucket notification title carrying the price."""

        outcome = str(self._queue_protection_identity(session)["outcome"] or "").upper()
        price = _maybe_decimal(bucket.get("baseline_price"))
        return f"{outcome} {_queue_decimal_text(price)} {base}"

    def _request_bucket_protection_cancel(
        self,
        session: Mapping[str, object],
        snapshot: Mapping[str, object] | None,
        bucket: Mapping[str, object],
        *,
        reason: str = "queue_ahead_ratio",
        only_order_ids: list[str] | None = None,
        notify_blocked: bool = True,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Cancel every own BUY resting at this bucket's price (issue 152 D4 /
        issue 167: one cancel episode per price bucket).

        Deliberately not routed through the manual cancel audit pipeline;
        the protection episode records its own durable action instead.
        Returns the updated bucket plus the session-level cancel flags.
        """

        session_id = str(session["session_id"])
        entry_order_id = str(session.get("entry_order_id") or "")
        token_id = str(session.get("token_id") or "")
        baseline_price = _maybe_decimal(bucket.get("baseline_price"))
        updated = dict(bucket)
        if baseline_price is None:
            return updated, {}
        own_order_ids = self._session_augment_own_order_ids(session)

        if snapshot is None:
            # Data-unreliable path: enumerate targets from one fresh
            # account read; a failed read blocks this tick and retries.
            rows = self._queue_account_open_orders()
            if rows is None:
                return (
                    self._blocked_bucket_protection_cancel(
                        session,
                        updated,
                        "account_read_failed",
                        "账户读取失败",
                        None,
                        notify=notify_blocked,
                    ),
                    {},
                )
            rows_by_id: dict[str, object] = {}
            for row in rows:
                order_id = self._order_id(row)
                if order_id:
                    rows_by_id.setdefault(order_id, row)
        else:
            rows_by_id = self._queue_level_rows(snapshot)

        # Identity re-check: every registered own receipt must still name a
        # BUY on the protected token before any cancel is sent.
        for order_id, row in rows_by_id.items():
            if order_id not in own_order_ids:
                continue
            row_token = str(_field(row, "token_id", _field(row, "asset_id", "")) or "")
            side = str(_field(row, "side", "")).upper()
            if (side and side != "BUY") or (row_token and row_token != token_id):
                return (
                    self._blocked_bucket_protection_cancel(
                        session,
                        updated,
                        "identity_conflict",
                        "回执身份不符",
                        None,
                        notify=notify_blocked,
                    ),
                    {},
                )

        if not self._mutation_allowed():
            remaining = self._own_queue_remaining_rows(
                rows_by_id, token_id=token_id, price=baseline_price
            )
            return (
                self._blocked_bucket_protection_cancel(
                    session,
                    updated,
                    "mutation_blocked",
                    "撤单被熔断阻止",
                    remaining,
                    notify=notify_blocked,
                ),
                {},
            )

        history = self._order_history(session)
        anchor = str(bucket.get("order_id") or "") or format(baseline_price, "f")
        targets: list[str] = []
        skipped: list[dict[str, object]] = []
        seen_targets: set[str] = set()

        def add_target(order_id: str) -> None:
            if order_id and order_id not in seen_targets:
                seen_targets.add(order_id)
                targets.append(order_id)

        # The bucket's own registered order first, then every other own
        # order resting at this price (durable history or receipt evidence).
        add_target(str(bucket.get("order_id") or ""))
        for order_id in own_order_ids:
            record = history.get(order_id)
            row = rows_by_id.get(order_id)
            source = (
                record
                if isinstance(record, Mapping) and record.get("status")
                else row
            )
            if source is None:
                continue
            status = str(_field(source, "status", "") or "").upper()
            if status and status in TERMINAL_ORDER_STATES:
                continue
            order_price = _maybe_decimal(_field(source, "price"))
            if order_price is not None and order_price != baseline_price:
                continue
            side = str(_field(source, "side", "BUY") or "BUY").upper()
            if side != "BUY":
                continue
            add_target(order_id)
        # Then every same-token same-price BUY row visible on the read
        # (manual same-price orders share the bucket's cancel scope).
        for order_id, row in rows_by_id.items():
            if order_id in seen_targets:
                continue
            row_token = str(_field(row, "token_id", _field(row, "asset_id", "")) or "")
            if row_token and row_token != token_id:
                continue
            row_price = _maybe_decimal(_field(row, "price"))
            if row_price is None:
                skipped.append({"order_id": order_id, "reason": "identity_unknown"})
                continue
            if row_price != baseline_price:
                continue
            side = str(_field(row, "side", "")).upper()
            if side != "BUY":
                skipped.append({"order_id": order_id, "reason": "identity_mismatch"})
                continue
            if str(_field(row, "status", "")).upper() in TERMINAL_ORDER_STATES:
                continue
            add_target(order_id)

        if only_order_ids is not None:
            retry = set(only_order_ids)
            targets = [order_id for order_id in targets if order_id in retry]
        # Issue 152 review: within one protection episode the target set and
        # the canceled set accumulate across retries (ordered union), so a
        # retried batch can never overwrite the earlier episode state.
        episode_targets = self._merge_order_id_lists(
            bucket.get("cancel_targets"), targets
        )

        # Issue 152 review fix: persist every target's cancel-time remaining
        # at request (intent) time, whether or not this batch's cancel is
        # later acknowledged, so an order the venue cancels without our
        # acknowledgment still reports its share in the episode total. A
        # target without a readable remaining persists as None (UNKNOWN),
        # never 0. First write wins: retries never overwrite an earlier
        # batch's persisted value.
        persisted_remaining: dict[str, object] = {}
        raw_remaining = bucket.get("cancel_target_remaining")
        if isinstance(raw_remaining, Mapping):
            for key, value in raw_remaining.items():
                order_id = str(key or "")
                if order_id:
                    persisted_remaining[order_id] = value
        for order_id in targets:
            if order_id in persisted_remaining:
                continue
            row = rows_by_id.get(order_id)
            remaining = self._queue_row_remaining(row) if row is not None else None
            persisted_remaining[order_id] = (
                None if remaining is None else str(remaining)
            )
        placement_times = self._record_order_placement_times(
            updated, rows_by_id, targets
        )
        confirmed_times: dict[str, object] = {}
        raw_confirmed = updated.get("order_cancel_confirmed_at")
        if isinstance(raw_confirmed, Mapping):
            confirmed_times.update(
                {str(key): value for key, value in raw_confirmed.items() if str(key)}
            )

        action_key = f"{session_id}:entry-protection-cancel:{anchor}"
        intent_payload: dict[str, object] = {
            "role": "entry-protection-cancel",
            "targets": targets,
            "skipped": skipped,
            "reason": reason,
            "ratio": updated.get("ratio"),
            "data_time": updated.get("data_time"),
            "cancel_target_remaining": persisted_remaining,
        }
        self.store.lp_upsert_action(
            session_id, action_key, state="pending", payload=intent_payload
        )

        canceled: list[str] = []
        failed: list[str] = []
        failure_error: str | None = None
        for order_id in targets:
            try:
                if self._cancel_order(order_id):
                    canceled.append(order_id)
                    confirmed_times.setdefault(order_id, _iso(self._now()))
                else:
                    failed.append(order_id)
            except Exception as exc:
                failed.append(order_id)
                failure_error = type(exc).__name__

        receipt_payload: dict[str, object] = {
            **intent_payload,
            "canceled": canceled,
            "failed": failed,
        }
        if failed:
            receipt_payload["error"] = failure_error or "cancel_not_acknowledged"
            self.store.lp_upsert_action(
                session_id, action_key, state="pending", payload=receipt_payload
            )
        else:
            self.store.lp_upsert_action(
                session_id, action_key, state="accepted", payload=receipt_payload
            )

        # Cancel-time remaining is summed from the per-target values
        # persisted at request time over every order in the episode's
        # canceled set (acknowledged or receipt-proved), never re-derived
        # from post-cancel reads.
        episode_canceled = self._merge_order_id_lists(
            bucket.get("canceled_order_ids"), canceled
        )
        episode_remaining = self._episode_canceled_remaining(
            persisted_remaining, episode_canceled
        )

        updated["state"] = "canceling"
        updated["cancel_reason"] = reason
        updated["cancel_targets"] = episode_targets
        updated["cancel_failed"] = failed
        updated["cancel_target_remaining"] = persisted_remaining
        updated["order_placement_times"] = placement_times
        updated["order_cancel_confirmed_at"] = confirmed_times
        updated["canceled_order_ids"] = episode_canceled
        updated["canceled_remaining"] = episode_remaining
        updated["cancel_requested_at"] = _iso(self._now())
        if failed:
            updated["cancel_failure"] = failure_error or "cancel_not_acknowledged"
        canceled_set = set(episode_canceled)
        episode_complete = bool(episode_targets) and all(
            order_id in canceled_set for order_id in episode_targets
        )
        if episode_complete:
            manual_count = sum(
                1 for order_id in episode_canceled if order_id != entry_order_id
            )
            title, message, xiaoai = self._queue_protection_success_notification(
                updated,
                session,
                canceled_count=len(episode_canceled),
                manual_count=manual_count,
                canceled_remaining=episode_remaining,
                title=self._queue_protection_price_title(
                    session, updated, "位置保护已触发撤单"
                ),
            )
            self._notify_protection(title, message, xiaoai)
            updated["notification_sent"] = True

        session_patch: dict[str, object] = {}
        if entry_order_id in targets and not bool(session.get("entry_cancel_requested")):
            session_patch["entry_cancel_requested"] = True
        session_augment_ids = {
            str(value)
            for value in _items(session.get("augment_order_ids"))
            if str(value or "")
        }
        augment_requested = {
            str(value) for value in _items(session.get("augment_cancel_requested"))
        }
        new_augment = sorted(
            augment_requested
            | {
                order_id
                for order_id in targets
                if order_id in session_augment_ids
            }
        )
        if new_augment != sorted(augment_requested):
            session_patch["augment_cancel_requested"] = new_augment
        return updated, session_patch

    def _blocked_bucket_protection_cancel(
        self,
        session: Mapping[str, object],
        bucket: dict[str, object],
        reason_code: str,
        failure_reason: str,
        remaining: Decimal | None,
        *,
        notify: bool = True,
    ) -> dict[str, object]:
        codes = list(bucket.get("reason_codes") or [])
        if reason_code not in codes:
            codes.append(reason_code)
        bucket["state"] = "blocked"
        bucket["reason_codes"] = codes
        if notify and bucket.get("blocked_notified") is not True:
            title, message, xiaoai = self._queue_protection_blocked_notification(
                bucket,
                session,
                failure_reason,
                remaining,
                title=self._queue_protection_price_title(
                    session, bucket, "位置保护撤单受阻"
                ),
            )
            self._notify_protection(title, message, xiaoai)
            bucket["blocked_notified"] = True
        return bucket

    def _converge_queue_protection(
        self,
        session: Mapping[str, object],
        bucket: Mapping[str, object],
        rows_by_id: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Settle one canceling bucket from order receipts (issue 152 D4.7)."""

        if str(bucket.get("state")) != "canceling":
            return None
        targets = [
            str(value)
            for value in _items(bucket.get("cancel_targets"))
            if str(value or "")
        ]
        if not targets:
            return None
        history = self._order_history(session)
        filled = Decimal("0")
        receipt_canceled: list[str] = []
        for order_id in targets:
            record = history.get(order_id)
            row = rows_by_id.get(order_id)
            source = (
                record
                if isinstance(record, Mapping) and record.get("status")
                else row
            )
            if source is None:
                # No receipt and no resting row anywhere: the order is no
                # longer open on any read path, i.e. canceled.
                receipt_canceled.append(order_id)
                continue
            status = str(_field(source, "status", "") or "").upper()
            if not status:
                return None
            if status not in TERMINAL_ORDER_STATES:
                return None
            if status in {"CANCELED", "CANCELLED"}:
                receipt_canceled.append(order_id)
            matched = _maybe_decimal(_field(source, "size_matched"))
            if matched is not None and matched > 0:
                filled += matched
        updated = dict(bucket)
        updated["order_placement_times"] = self._record_order_placement_times(
            updated, rows_by_id, targets
        )
        confirmed_times: dict[str, object] = {}
        raw_confirmed = updated.get("order_cancel_confirmed_at")
        if isinstance(raw_confirmed, Mapping):
            confirmed_times.update(
                {str(key): value for key, value in raw_confirmed.items() if str(key)}
            )
        if receipt_canceled:
            observed_at = _iso(self._now())
            for order_id in receipt_canceled:
                confirmed_times.setdefault(order_id, observed_at)
        updated["order_cancel_confirmed_at"] = confirmed_times
        if filled > 0:
            updated["state"] = "partially_filled"
            updated["partially_filled_quantity"] = filled
        else:
            updated["state"] = "canceled"
        # Receipt-proved cancellations join the durable episode set so the
        # one-shot success notification reports only actually canceled
        # orders. A fully filled/rejected target has no cancellation success
        # to announce.
        canceled = self._merge_order_id_lists(
            updated.get("canceled_order_ids"), receipt_canceled
        )
        updated["canceled_order_ids"] = canceled
        if updated.get("notification_sent") is not True and canceled:
            entry_order_id = str(session.get("entry_order_id") or "")
            # Issue 152 review fix: report the total from the per-target
            # cancel-time remaining persisted at request time, so orders the
            # venue canceled without our acknowledgment keep their share
            # instead of fabricating 0.
            updated["canceled_remaining"] = self._episode_canceled_remaining(
                updated.get("cancel_target_remaining"), canceled
            )
            manual_count = sum(
                1 for order_id in canceled if order_id != entry_order_id
            )
            title, message, xiaoai = self._queue_protection_success_notification(
                updated,
                session,
                canceled_count=len(canceled),
                manual_count=manual_count,
                canceled_remaining=_maybe_decimal(
                    updated.get("canceled_remaining")
                ),
                title=self._queue_protection_price_title(
                    session, updated, "位置保护已触发撤单"
                ),
            )
            self._notify_protection(title, message, xiaoai)
            updated["notification_sent"] = True
        return updated

    def _apply_queue_protection(
        self,
        session: Mapping[str, object],
        snapshot: Mapping[str, object],
        *,
        skip_cancel_keys: Collection[str] = (),
    ) -> dict[str, object]:
        """Evaluate every price bucket inside the monitor tick and persist
        the v2 {version, data_failures, levels} payload (issue 167)."""

        protection = session.get("queue_protection")
        buckets = self._queue_protection_levels(session)
        if not isinstance(protection, Mapping) or not buckets:
            return dict(session)
        session_id = str(session["session_id"])
        rows_by_id = self._queue_level_rows(snapshot)
        failures = _queue_group_failures_int(protection.get("data_failures"))
        changed = False
        session_patch: dict[str, object] = {}
        new_levels: dict[str, dict[str, object]] = {}
        skip_keys = {str(value) for value in skip_cancel_keys}
        for key, bucket in buckets.items():
            if str(bucket.get("state")) == "canceling":
                converged = self._converge_queue_protection(
                    session, bucket, rows_by_id
                )
                if converged is not None:
                    new_levels[key] = converged
                    changed = True
                    continue
                failed = [
                    str(value)
                    for value in _items(bucket.get("cancel_failed"))
                    if str(value or "")
                ]
                if failed and key not in skip_keys:
                    bucket, patch = self._request_bucket_protection_cancel(
                        session,
                        snapshot,
                        bucket,
                        reason=str(bucket.get("cancel_reason") or "queue_ahead_ratio"),
                        only_order_ids=failed,
                    )
                    # Issue 167 review fix: each bucket computes its session
                    # flags from the same pre-loop session, so a plain
                    # update() would let the second triggered bucket drop
                    # the first one's augment ids. Merge set-like keys by
                    # union (same pattern as _conservative_protection_cancel).
                    for patch_key, patch_value in patch.items():
                        if patch_key == "augment_cancel_requested":
                            merged = {
                                str(value)
                                for value in _items(session_patch.get(patch_key))
                            } | {str(value) for value in _items(patch_value)}
                            session_patch[patch_key] = sorted(merged)
                        else:
                            session_patch[patch_key] = patch_value
                    new_levels[key] = bucket
                    changed = True
                    continue
                new_levels[key] = bucket
                continue
            evaluation = self._bucket_queue_protection_evaluation(
                session, bucket, snapshot, rows_by_id
            )
            if evaluation is not None:
                bucket = evaluation
                changed = True
                if str(bucket.get("state")) not in {"unknown", "registered"}:
                    failures = 0
            if str(bucket.get("state")) == "triggered":
                if key in skip_keys:
                    new_levels[key] = bucket
                    continue
                bucket, patch = self._request_bucket_protection_cancel(
                    session, snapshot, bucket, reason="queue_ahead_ratio"
                )
                # Same union merge as above: two buckets triggering in one
                # tick must accumulate their session flags, not overwrite.
                for patch_key, patch_value in patch.items():
                    if patch_key == "augment_cancel_requested":
                        merged = {
                            str(value)
                            for value in _items(session_patch.get(patch_key))
                        } | {str(value) for value in _items(patch_value)}
                        session_patch[patch_key] = sorted(merged)
                    else:
                        session_patch[patch_key] = patch_value
                new_levels[key] = bucket
                changed = True
                continue
            new_levels[key] = bucket
        if not changed:
            return dict(session)
        session_patch["queue_protection"] = {
            "version": 2,
            "data_failures": failures,
            "levels": new_levels,
        }
        queue_payload = cast(Mapping[str, object], session_patch.pop("queue_protection"))
        merger = getattr(self.store, "lp_merge_queue_protection", None)
        if callable(merger):
            updated = merger(
                session_id,
                queue_protection=queue_payload,
                patch=session_patch,
            )
        else:
            updated = self.store.lp_update_session(
                session_id,
                patch={**session_patch, "queue_protection": queue_payload},
            )
        return updated

    # ---- Issue 159: first-seen baseline fallback protections ----

    def _apply_first_seen_protections(self) -> None:
        """Evaluate every active first-seen episode, serially per token.

        Runs inside the existing one-second monitor tick under the service
        mutex, even when no LP session is active: web-manual BUYs registered
        by the dashboard's first-seen diff get the same protection loop as
        the issue 152 submit-boundary episodes, with no added concurrency.
        """

        reader = getattr(self.store, "lp_active_first_seen_episodes", None)
        if not callable(reader):
            return
        try:
            episodes = reader()
        except Exception:
            return
        for episode in episodes:
            if not isinstance(episode, Mapping):
                continue
            if not str(episode.get("episode_id") or ""):
                continue
            self._apply_first_seen_protection(episode)

    def _first_seen_rows_by_id(self) -> dict[str, object] | None:
        """Open-order rows keyed by id from one fresh account read, or None."""

        rows = self._queue_account_open_orders()
        if rows is None:
            return None
        rows_by_id: dict[str, object] = {}
        for row in rows:
            order_id = self._order_id(row)
            if order_id and order_id not in rows_by_id:
                rows_by_id[order_id] = row
        return rows_by_id

    @staticmethod
    def _first_seen_anchor_ids(episode: Mapping[str, object]) -> list[str]:
        raw = episode.get("anchor_order_ids")
        if isinstance(raw, Mapping) or not isinstance(raw, (list, tuple)):
            return []
        return [str(value) for value in raw if str(value or "")]

    def _first_seen_anchors_all_terminal(
        self,
        rows_by_id: dict[str, object] | None,
        episode: Mapping[str, object],
    ) -> bool:
        """True when every registered anchor is gone from every read path.

        A missing row means the order no longer rests anywhere open; a row
        without a readable status cannot prove terminality and keeps the
        episode alive.
        """

        anchor_ids = self._first_seen_anchor_ids(episode)
        if not anchor_ids:
            return False
        for order_id in anchor_ids:
            row = rows_by_id.get(order_id) if rows_by_id is not None else None
            if row is None:
                continue
            status = str(_field(row, "status", "") or "").upper()
            if not status:
                return False
            if status not in TERMINAL_ORDER_STATES:
                return False
        return True

    def _first_seen_gate_open(
        self,
        episode: Mapping[str, object],
        rows_by_id: dict[str, object] | None,
    ) -> bool:
        """Issue 159 gate, mirroring ``_queue_protection_gate_open``: at
        least one registered anchor alive, cancel not yet requested, and no
        anchor has taken a fill."""

        if episode.get("cancel_requested_at"):
            return False
        anchor_ids = self._first_seen_anchor_ids(episode)
        if not anchor_ids:
            return False
        for order_id in anchor_ids:
            row = rows_by_id.get(order_id) if rows_by_id is not None else None
            if row is None:
                continue
            status = str(_field(row, "status", "") or "").upper()
            if status and status in TERMINAL_ORDER_STATES:
                continue
            matched = _maybe_decimal(_field(row, "size_matched"))
            if matched is not None and matched > 0:
                continue
            return True
        return False

    def _own_queue_remaining_rows(
        self,
        rows_by_id: Mapping[str, object],
        *,
        token_id: str,
        price: Decimal,
    ) -> Decimal | None:
        """Sum remaining size of own open BUYs at one token and price.

        Same poisoning rule as ``_own_queue_remaining``: any participating
        row without a parseable remaining poisons the total to None.
        """

        total = Decimal("0")
        for row in rows_by_id.values():
            row_token = _field(row, "token_id", _field(row, "asset_id"))
            if row_token not in (None, "", token_id):
                continue
            if str(_field(row, "side", "")).upper() != "BUY":
                continue
            if _maybe_decimal(_field(row, "price")) != price:
                continue
            if str(_field(row, "status", "")).upper() in TERMINAL_ORDER_STATES:
                continue
            remaining = self._queue_row_remaining(row)
            if remaining is None:
                return None
            total += remaining
        return total

    def _first_seen_book(
        self, token_id: str
    ) -> tuple[Mapping[str, object] | None, str | None]:
        """Fresh single-token book read with snapshot freshness semantics.

        Freshness is measured against a clock read taken after the book
        arrives, so a book we just received is never "negative age" stale.
        """

        book = self._read_first_seen_book(token_id)
        if book is None or book.get("received_at") is None:
            return None, "book_unknown"
        try:
            _freshness(
                book.get("received_at"),
                self._now(),
                "book_freshness",
                max_age=BOOK_FRESHNESS_SECONDS,
            )
        except ValueError:
            return None, "book_freshness_unknown"
        return book, None

    def _first_seen_data_failure(
        self,
        episode: Mapping[str, object],
        reason: str,
        *,
        gate_open: bool,
    ) -> dict[str, object] | None:
        """Increment the durable outage counter, mirroring issue 152."""

        if reason not in _QUEUE_DATA_FAILURE_REASONS:
            return None
        if str(episode.get("state")) in {"canceling", "canceled", "partially_filled"}:
            return None
        updated = dict(episode)
        if not gate_open:
            # The episode no longer protects a live anchor: an outage streak
            # is irrelevant and resets instead of accumulating.
            updated["data_failures"] = 0
            return updated
        failures = _maybe_decimal(updated.get("data_failures")) or Decimal("0")
        updated["data_failures"] = failures + 1
        return updated

    def _conservative_first_seen_cancel(
        self,
        episode: Mapping[str, object],
        failures: dict[str, object] | None,
        *,
        gate_open: bool,
    ) -> dict[str, object] | None:
        """Cancel the protected anchors after ten data outages (issue 159)."""

        if failures is None:
            return None
        count = _maybe_decimal(failures.get("data_failures")) or Decimal("0")
        if count < LP_PROTECTION_DATA_FAILURE_LIMIT:
            return None
        if not gate_open:
            return None
        return self._request_first_seen_protection_cancel(
            episode, None, reason="book_unreliable"
        )

    def _apply_first_seen_protection(self, episode: Mapping[str, object]) -> None:
        """Run one episode's evaluate/converge/cancel step and persist it."""

        episode_id = str(episode["episode_id"])
        state = str(episode.get("state") or "")
        rows_by_id = self._first_seen_rows_by_id()

        if state == "canceling":
            converged = self._converge_first_seen_protection(episode, rows_by_id)
            if converged is not None:
                self.store.lp_update_first_seen_episode(
                    episode_id,
                    state=str(converged.get("state")),
                    patch=converged,
                )
                return
            failed = [
                str(value)
                for value in _items(episode.get("cancel_failed"))
                if str(value or "")
            ]
            if failed and rows_by_id is not None:
                result = self._request_first_seen_protection_cancel(
                    episode,
                    rows_by_id,
                    reason=str(episode.get("cancel_reason") or "queue_ahead_ratio"),
                    only_order_ids=failed,
                )
                if result is not None:
                    self.store.lp_update_first_seen_episode(
                        episode_id,
                        state=str(result.get("state")),
                        patch=result,
                    )
            return

        if rows_by_id is None:
            # The account read failed: the observation is a data outage and
            # the anchors cannot be proven dead, so the gate stays open.
            failures = self._first_seen_data_failure(
                episode, "external_snapshot_unknown", gate_open=True
            )
            conservative = self._conservative_first_seen_cancel(
                episode, failures, gate_open=True
            )
            if conservative is not None:
                self.store.lp_update_first_seen_episode(
                    episode_id,
                    state=str(conservative.get("state")),
                    patch=conservative,
                )
            elif failures is not None:
                self.store.lp_update_first_seen_episode(
                    episode_id, patch=failures
                )
            return

        if self._first_seen_anchors_all_terminal(rows_by_id, episode):
            # Every anchor reached a terminal state on its own: the episode
            # ends and the token's remaining orders return to position
            # unknown (no chaining, no re-anchoring).
            self.store.lp_update_first_seen_episode(
                episode_id, state="terminal"
            )
            return

        gate_open = self._first_seen_gate_open(episode, rows_by_id)
        token_id = str(episode.get("token_id") or "")
        anchor_price = _maybe_decimal(episode.get("anchor_price"))
        if anchor_price is None:
            return
        book, reason = self._first_seen_book(token_id)
        if reason is not None:
            failures = self._first_seen_data_failure(
                episode, reason, gate_open=gate_open
            )
            conservative = self._conservative_first_seen_cancel(
                episode, failures, gate_open=gate_open
            )
            if conservative is not None:
                self.store.lp_update_first_seen_episode(
                    episode_id,
                    state=str(conservative.get("state")),
                    patch=conservative,
                )
            elif failures is not None:
                self.store.lp_update_first_seen_episode(
                    episode_id, patch=failures
                )
            return
        if not gate_open:
            # Nothing protectable and the data is healthy: no write, exactly
            # like the issue 152 evaluation returning None.
            return

        own_remaining = self._own_queue_remaining_rows(
            rows_by_id, token_id=token_id, price=anchor_price
        )
        estimate = estimate_lp_queue_position(
            book,
            price=anchor_price,
            own_remaining=own_remaining,
            baseline_front=_maybe_decimal(episode.get("baseline_front"))
            or Decimal("0"),
            threshold=_maybe_decimal(episode.get("threshold"))
            or LP_QUEUE_PROTECTION_THRESHOLD,
            condition_id=str(episode.get("condition_id") or "") or None,
            token_id=token_id or None,
        )
        updated = dict(episode)
        updated.update(
            {
                "state": estimate["state"],
                "front_estimate": estimate["front_estimate"],
                "level_total": estimate["level_total"],
                "ratio": estimate["ratio"],
                "reason_codes": estimate["reason_codes"],
                "data_time": estimate["data_time"],
            }
        )
        if estimate["state"] != "unknown":
            updated["data_failures"] = 0
            if (
                str(episode.get("state")) == "blocked"
                and updated.get("blocked_notified") is True
                and (
                    "mutation_blocked"
                    not in {
                        str(value)
                        for value in _items(episode.get("reason_codes"))
                    }
                    or self._mutation_allowed()
                )
            ):
                # blocked → recovered: re-arm the one-shot block notice so a
                # later block notifies again.
                updated["blocked_notified"] = False
        # The column state stays in the episode lifecycle domain; an
        # UNKNOWN estimate keeps the episode monitoring with reason codes.
        next_state = (
            "monitoring"
            if estimate["state"] in {"monitoring", "unknown", "triggered"}
            else str(estimate["state"])
        )
        episode = self.store.lp_update_first_seen_episode(
            episode_id, state=next_state, patch=updated
        )
        if estimate["state"] != "triggered":
            return
        result = self._request_first_seen_protection_cancel(
            episode, rows_by_id, reason="queue_ahead_ratio"
        )
        if result is not None:
            self.store.lp_update_first_seen_episode(
                episode_id, state=str(result.get("state")), patch=result
            )

    def _blocked_first_seen_cancel(
        self,
        episode: Mapping[str, object],
        protection: dict[str, object],
        reason_code: str,
        failure_reason: str,
        remaining: Decimal | None,
    ) -> dict[str, object]:
        codes = list(protection.get("reason_codes") or [])
        if reason_code not in codes:
            codes.append(reason_code)
        protection["state"] = "blocked"
        protection["reason_codes"] = codes
        if protection.get("blocked_notified") is not True:
            title, message, xiaoai = self._queue_protection_blocked_notification(
                protection,
                episode,
                failure_reason,
                remaining,
                title="LP 位置保护撤单受阻（首见基线）",
                trigger_prefix="首见基线 · ",
            )
            self._notify_protection(title, message, xiaoai)
            protection["blocked_notified"] = True
        return protection

    def _converge_first_seen_protection(
        self,
        episode: Mapping[str, object],
        rows_by_id: dict[str, object] | None,
    ) -> dict[str, object] | None:
        """Settle a canceling episode from fresh open-order reads (issue 159)."""

        if str(episode.get("state")) != "canceling":
            return None
        targets = [
            str(value)
            for value in _items(episode.get("cancel_targets"))
            if str(value or "")
        ]
        if not targets or rows_by_id is None:
            return None
        filled = Decimal("0")
        receipt_canceled: list[str] = []
        for order_id in targets:
            row = rows_by_id.get(order_id)
            if row is None:
                # No resting row anywhere: the order is no longer open on
                # any read path, i.e. canceled.
                receipt_canceled.append(order_id)
                continue
            status = str(_field(row, "status", "") or "").upper()
            if not status:
                return None
            if status not in TERMINAL_ORDER_STATES:
                return None
            if status in {"CANCELED", "CANCELLED"}:
                receipt_canceled.append(order_id)
            matched = _maybe_decimal(_field(row, "size_matched"))
            if matched is not None and matched > 0:
                filled += matched
        updated = dict(episode)
        updated["order_placement_times"] = self._record_order_placement_times(
            updated, rows_by_id, targets
        )
        confirmed_times: dict[str, object] = {}
        raw_confirmed = updated.get("order_cancel_confirmed_at")
        if isinstance(raw_confirmed, Mapping):
            confirmed_times.update(
                {str(key): value for key, value in raw_confirmed.items() if str(key)}
            )
        if receipt_canceled:
            observed_at = _iso(self._now())
            for order_id in receipt_canceled:
                confirmed_times.setdefault(order_id, observed_at)
        updated["order_cancel_confirmed_at"] = confirmed_times
        if filled > 0:
            updated["state"] = "partially_filled"
            updated["partially_filled_quantity"] = filled
        else:
            updated["state"] = "canceled"
        # Keep the success notice tied to the durable canceled union. A
        # target that only reached FILLED/another terminal state must not be
        # presented as a cancellation.
        canceled = self._merge_order_id_lists(
            updated.get("canceled_order_ids"), receipt_canceled
        )
        updated["canceled_order_ids"] = canceled
        if updated.get("notification_sent") is not True and canceled:
            updated["canceled_remaining"] = self._episode_canceled_remaining(
                updated.get("cancel_target_remaining"), canceled
            )
            # Every first-seen target is a manual web order.
            manual_count = len(canceled)
            title, message, xiaoai = self._queue_protection_success_notification(
                updated,
                episode,
                canceled_count=len(canceled),
                manual_count=manual_count,
                canceled_remaining=_maybe_decimal(
                    updated.get("canceled_remaining")
                ),
                title="LP 位置保护撤单（首见基线）",
                trigger_prefix="首见基线 · ",
            )
            self._notify_protection(title, message, xiaoai)
            updated["notification_sent"] = True
        return updated

    def _request_first_seen_protection_cancel(
        self,
        episode: Mapping[str, object],
        rows_by_id: dict[str, object] | None,
        *,
        reason: str = "queue_ahead_ratio",
        only_order_ids: list[str] | None = None,
    ) -> dict[str, object] | None:
        """Cancel every own BUY resting at the first-seen anchor price.

        Deliberately not routed through the manual cancel audit pipeline;
        the episode records its own durable per-target actions instead.
        """

        episode_id = str(episode.get("episode_id") or "")
        token_id = str(episode.get("token_id") or "")
        anchor_price = _maybe_decimal(episode.get("anchor_price"))
        if not episode_id or anchor_price is None:
            return None
        updated = dict(episode)

        if rows_by_id is None:
            # Data-unreliable path: enumerate targets from one fresh
            # account read; a failed read blocks this tick and retries.
            rows_by_id = self._first_seen_rows_by_id()
            if rows_by_id is None:
                return self._blocked_first_seen_cancel(
                    episode,
                    updated,
                    "account_read_failed",
                    "账户读取失败",
                    None,
                )

        # Identity re-check: every registered anchor receipt must still name
        # a BUY on the episode's token before any cancel is sent.
        anchor_set = set(self._first_seen_anchor_ids(episode))
        for order_id, row in rows_by_id.items():
            if order_id not in anchor_set:
                continue
            row_token = str(_field(row, "token_id", _field(row, "asset_id", "")) or "")
            side = str(_field(row, "side", "")).upper()
            if (side and side != "BUY") or (row_token and row_token != token_id):
                return self._blocked_first_seen_cancel(
                    episode, updated, "identity_conflict", "回执身份不符", None
                )

        if not self._mutation_allowed():
            remaining = None
            anchor_ids = self._first_seen_anchor_ids(episode)
            if anchor_ids and all(order_id in rows_by_id for order_id in anchor_ids):
                anchor_rows_valid = all(
                    str(_field(rows_by_id[order_id], "side", "") or "").upper()
                    == "BUY"
                    and str(
                        _field(
                            rows_by_id[order_id],
                            "token_id",
                            _field(rows_by_id[order_id], "asset_id", ""),
                        )
                        or ""
                    )
                    == token_id
                    and _maybe_decimal(_field(rows_by_id[order_id], "price"))
                    == anchor_price
                    for order_id in anchor_ids
                )
                if anchor_rows_valid:
                    remaining = self._own_queue_remaining_rows(
                        rows_by_id, token_id=token_id, price=anchor_price
                    )
            return self._blocked_first_seen_cancel(
                episode, updated, "mutation_blocked", "撤单被熔断阻止", remaining
            )

        targets: list[str] = []
        skipped: list[dict[str, object]] = []
        for order_id in self._first_seen_anchor_ids(episode):
            targets.append(order_id)
        for order_id, row in rows_by_id.items():
            if not order_id or order_id in anchor_set:
                continue
            row_token = str(_field(row, "token_id", _field(row, "asset_id", "")) or "")
            if row_token and row_token != token_id:
                continue
            row_price = _maybe_decimal(_field(row, "price"))
            if row_price is None:
                skipped.append({"order_id": order_id, "reason": "identity_unknown"})
                continue
            if row_price != anchor_price:
                continue
            side = str(_field(row, "side", "")).upper()
            if side != "BUY":
                skipped.append({"order_id": order_id, "reason": "identity_mismatch"})
                continue
            if str(_field(row, "status", "")).upper() in TERMINAL_ORDER_STATES:
                continue
            targets.append(order_id)

        if only_order_ids is not None:
            retry = set(only_order_ids)
            targets = [order_id for order_id in targets if order_id in retry]
        # Within one protection episode the target set and the canceled set
        # accumulate across retries (ordered union), so a retried batch can
        # never overwrite the earlier episode state.
        episode_targets = self._merge_order_id_lists(
            episode.get("cancel_targets"), targets
        )

        # Persist every target's cancel-time remaining at request (intent)
        # time; first write wins, unknown stays None, never 0.
        persisted_remaining: dict[str, object] = {}
        raw_remaining = episode.get("cancel_target_remaining")
        if isinstance(raw_remaining, Mapping):
            for key, value in raw_remaining.items():
                order_id = str(key or "")
                if order_id:
                    persisted_remaining[order_id] = value
        for order_id in targets:
            if order_id in persisted_remaining:
                continue
            row = rows_by_id.get(order_id)
            remaining = self._queue_row_remaining(row) if row is not None else None
            persisted_remaining[order_id] = (
                None if remaining is None else str(remaining)
            )

        # One durable action per target: intent first, then the cancel.
        placement_times = self._record_order_placement_times(
            updated, rows_by_id, targets
        )
        confirmed_times: dict[str, object] = {}
        raw_confirmed = updated.get("order_cancel_confirmed_at")
        if isinstance(raw_confirmed, Mapping):
            confirmed_times.update(
                {str(key): value for key, value in raw_confirmed.items() if str(key)}
            )

        def action_payload(order_id: str) -> dict[str, object]:
            return {
                "role": "first-seen-protection-cancel",
                "episode_id": episode_id,
                "targets": [order_id],
                "skipped": skipped,
                "reason": reason,
                "ratio": updated.get("ratio"),
                "data_time": updated.get("data_time"),
                "cancel_target_remaining": {
                    order_id: persisted_remaining.get(order_id)
                },
            }

        action_keys = {
            order_id: f"{episode_id}:first-seen-protection-cancel:{order_id}"
            for order_id in targets
        }
        # lp_actions rows reference lp_sessions; the reserved manual anchor
        # session (state complete, never active) is created once so the FK
        # holds for first-seen episode actions, mirroring the dashboard
        # manual-cancel audit anchor.  Creation is best-effort: while an
        # n-leg batch owns the insert gate (or the store is unavailable) the
        # refusal must not escape into the tick — the episode blocks through
        # the same one-shot path as every other cancel blocker instead of
        # starving the monitor and the issue 152 session pipeline.
        try:
            self.store.lp_create_session(
                LP_RESERVED_MANUAL_SESSION_ID,
                LP_RESERVED_MANUAL_SESSION_ID,
                state="complete",
                payload={"context": "first_seen_protection_audit"},
            )
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            return self._blocked_first_seen_cancel(
                episode,
                updated,
                "anchor_session_failed",
                f"锚点审计会话创建失败（{detail}）",
                None,
            )
        for order_id in targets:
            self.store.lp_upsert_action(
                LP_RESERVED_MANUAL_SESSION_ID,
                action_keys[order_id],
                state="pending",
                payload=action_payload(order_id),
            )

        canceled: list[str] = []
        failed: list[str] = []
        failure_error: str | None = None
        for order_id in targets:
            try:
                if self._cancel_order(order_id):
                    canceled.append(order_id)
                    confirmed_times.setdefault(order_id, _iso(self._now()))
                else:
                    failed.append(order_id)
            except Exception as exc:
                failed.append(order_id)
                failure_error = type(exc).__name__

        for order_id in targets:
            receipt = action_payload(order_id)
            if order_id in failed:
                receipt["canceled"] = []
                receipt["failed"] = [order_id]
                receipt["error"] = failure_error or "cancel_not_acknowledged"
                self.store.lp_upsert_action(
                    LP_RESERVED_MANUAL_SESSION_ID,
                    action_keys[order_id],
                    state="pending",
                    payload=receipt,
                )
            else:
                receipt["canceled"] = [order_id]
                receipt["failed"] = []
                self.store.lp_upsert_action(
                    LP_RESERVED_MANUAL_SESSION_ID,
                    action_keys[order_id],
                    state="accepted",
                    payload=receipt,
                )

        # Cancel-time remaining is summed from the per-target values
        # persisted at request time over every order in the episode's
        # canceled set, never re-derived from post-cancel reads.
        episode_canceled = self._merge_order_id_lists(
            episode.get("canceled_order_ids"), canceled
        )
        episode_remaining = self._episode_canceled_remaining(
            persisted_remaining, episode_canceled
        )

        updated["state"] = "canceling"
        updated["cancel_reason"] = reason
        updated["cancel_targets"] = episode_targets
        updated["cancel_failed"] = failed
        updated["cancel_target_remaining"] = persisted_remaining
        updated["order_placement_times"] = placement_times
        updated["order_cancel_confirmed_at"] = confirmed_times
        updated["canceled_order_ids"] = episode_canceled
        updated["canceled_remaining"] = episode_remaining
        updated["cancel_requested_at"] = _iso(self._now())
        if failed:
            updated["cancel_failure"] = failure_error or "cancel_not_acknowledged"
        canceled_set = set(episode_canceled)
        episode_complete = bool(episode_targets) and all(
            order_id in canceled_set for order_id in episode_targets
        )
        if episode_complete:
            manual_count = len(episode_canceled)
            title, message, xiaoai = self._queue_protection_success_notification(
                updated,
                episode,
                canceled_count=len(episode_canceled),
                manual_count=manual_count,
                canceled_remaining=episode_remaining,
                title="LP 位置保护撤单（首见基线）",
                trigger_prefix="首见基线 · ",
            )
            self._notify_protection(title, message, xiaoai)
            updated["notification_sent"] = True
        return updated

    @staticmethod
    def _session_order_ids(session: Mapping[str, object]) -> list[str]:
        """Return every order owned by this opening in stable insertion order."""

        result: list[str] = []
        seen: set[str] = set()

        def add(value: object) -> None:
            order_id = str(value or "")
            if order_id and order_id not in seen:
                seen.add(order_id)
                result.append(order_id)

        for key in (
            "entry_order_id",
            "passive_exit_order_id",
            "protected_exit_order_id",
        ):
            add(session.get(key))
        for value in _items(session.get("augment_order_ids")):
            add(value)
        for value in _items(session.get("owned_order_ids")):
            add(value)
        history = session.get("order_history")
        if isinstance(history, Mapping):
            for value in history:
                add(value)
        elif isinstance(history, Sequence) and not isinstance(history, (str, bytes)):
            for value in history:
                add(_field(value, "order_id", _field(value, "id", "")))
        return result

    @staticmethod
    def _order_history(session: Mapping[str, object]) -> dict[str, dict[str, object]]:
        raw = session.get("order_history")
        history: dict[str, dict[str, object]] = {}
        if isinstance(raw, Mapping):
            for order_id, value in raw.items():
                if isinstance(value, Mapping):
                    history[str(order_id)] = dict(value)
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for value in raw:
                if not isinstance(value, Mapping):
                    continue
                order_id = str(_field(value, "order_id", _field(value, "id", "")) or "")
                if order_id:
                    history[order_id] = dict(value)
        return history

    def _sync_order_history(
        self, session: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> dict[str, object]:
        """Merge exact owned order receipts without trusting aggregate hints.

        The authenticated adapter may expose only open orders on one read and
        a terminal order on a later read.  A terminal receipt is therefore
        retained, while a previously live/missing receipt becomes UNKNOWN and
        can never prove that all orders ended.
        """

        session_id = str(session["session_id"])
        order_ids = self._session_order_ids(session)
        history = self._order_history(session)
        rows_by_id: dict[str, object] = {}
        for order in _items(snapshot.get("orders")):
            order_id = self._order_id(order)
            if not order_id or order_id not in order_ids:
                continue
            if order_id in rows_by_id:
                # Duplicate rows from paginated account reads are harmless
                # only when they carry the same order identity.
                continue
            rows_by_id[order_id] = order

        expected_token = str(session.get("token_id") or "")
        expected_sides = {
            str(session.get("entry_order_id") or ""): "BUY",
        }
        for key in ("passive_exit_order_id", "protected_exit_order_id"):
            expected_sides[str(session.get(key) or "")] = "SELL"
        # Issue 158: augment orders are session-owned BUYs; their receipts
        # must reconcile like the entry order's.
        for value in _items(session.get("augment_order_ids")):
            augment_id = str(value or "")
            if augment_id:
                expected_sides[augment_id] = "BUY"
        for order_id in order_ids:
            previous = dict(history.get(order_id, {}))
            order = rows_by_id.get(order_id)
            if order is None:
                previous_status = str(previous.get("status") or "").upper()
                if previous_status not in TERMINAL_ORDER_STATES:
                    previous["status"] = "UNKNOWN"
                previous.setdefault("order_id", order_id)
                history[order_id] = previous
                continue
            token = _field(order, "token_id", _field(order, "asset_id", None))
            if token not in (None, "", expected_token):
                raise ValueError("owned_order_token_mismatch")
            side = str(_field(order, "side", "")).upper()
            expected_side = expected_sides.get(order_id)
            if expected_side and side and side != expected_side:
                raise ValueError("owned_order_side_mismatch")
            status = str(_field(order, "status", "")).upper()
            if not status:
                raise ValueError("owned_order_status_unknown")
            current = dict(previous)
            current.update(
                {
                    "order_id": order_id,
                    "status": status,
                    "token_id": token or expected_token,
                    "side": side or expected_side,
                }
            )
            for name in (
                "price",
                "original_size",
                "size_matched",
                "remaining_size",
                "size",
                "quantity",
                "expiration",
            ):
                value = _field(order, name, None)
                if value is not None:
                    current[name] = value
            history[order_id] = current
        terminal = bool(order_ids) and all(
            str(history.get(order_id, {}).get("status") or "").upper()
            in TERMINAL_ORDER_STATES
            for order_id in order_ids
        )
        return self.store.lp_update_session(
            session_id,
            patch={
                "owned_order_ids": order_ids,
                "order_history": history,
                "orders_terminal": terminal,
            },
        )

    def _create_limit(self, **kwargs: object) -> object:
        self._require_mutation()
        direct = getattr(self.exchange, "lp_create_limit_order", None)
        if callable(direct):
            return direct(**kwargs)
        method = getattr(self.exchange, "create_limit_order", None)
        if not callable(method):
            raise RuntimeError("limit_order_adapter_unavailable")
        return method(**kwargs)

    def _post_limit(self, signed: object) -> object:
        self._require_mutation()
        direct = getattr(self.exchange, "lp_post_order", None)
        if callable(direct):
            return direct(signed)
        method = getattr(self.exchange, "post_order", None)
        if not callable(method):
            raise RuntimeError("order_post_adapter_unavailable")
        return method(signed)

    @staticmethod
    def _order_response(response: object) -> tuple[bool, str]:
        order_id = _field(response, "order_id", _field(response, "id", ""))
        order = str(order_id or "")
        accepted = _field(response, "accepted", None)
        ok = _field(response, "ok", None)
        status = str(_field(response, "status", "")).upper()
        if (
            accepted is False
            or ok is False
            or status in {"REJECTED", "FAILED", "CANCELLED", "CANCELED", "EXPIRED"}
        ):
            return False, order
        if accepted is True or order or status in {"LIVE", "OPEN", "ACCEPTED", "MATCHED", "FILLED"}:
            return True, order
        return False, order

    def _queue_level_order_exempt(
        self, session: Mapping[str, object], order: object
    ) -> bool:
        """Whether a same-token BUY belongs to the active protection episodes.

        Issue 152: manual BUY quotes on the protected token (any price) are
        expected while an episode is open — they are monitored and, on
        trigger, canceled at the bucket price.  Issue 167: the episodes are
        the per-price buckets; the exemption stays on while at least one
        bucket is still live (none settled).  SELL rows and other tokens
        remain governed by the unowned-order guard, and the submit boundary
        keeps rejecting outside orders exactly as before.
        """

        protection = session.get("queue_protection")
        buckets = self._queue_protection_levels(session)
        if not isinstance(protection, Mapping) or not buckets:
            return False
        if all(
            str(bucket.get("state")) in {"canceled", "partially_filled"}
            for bucket in buckets.values()
        ):
            return False
        if all(
            bucket.get("cancel_scope") != "own_buys_at_level"
            for bucket in buckets.values()
        ):
            return False
        if str(_field(order, "side", "")).upper() != "BUY":
            return False
        row_token = str(_field(order, "token_id", _field(order, "asset_id", "")) or "")
        expected = str(session.get("token_id") or "")
        return bool(row_token) and row_token == expected

    def _unowned_target_order_reason(
        self, session: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> str | None:
        account = snapshot.get("account")
        if not isinstance(account, Mapping):
            return None
        expected_token = str(session.get("token_id") or "")
        expected_market = str(session.get("market_id") or "")
        owned_ids = set(self._session_order_ids(session))
        for order in _items(account.get("open_orders")):
            order_id = self._order_id(order)
            token_value = _field(order, "token_id", _field(order, "asset_id", None))
            # 生产 raw 挂单行恒含 market_id 键但值为 None；_field 是 dict.get，
            # 键存在值 None 不走 default，嵌套写法的 market/condition_id 兜底
            # 全成死代码 → 跨市场单被判成本市场单。这里必须是「第一个非空值」。
            market_value = (
                _field(order, "market_id")
                or _field(order, "market")
                or _field(order, "condition_id")
            )
            token = str(token_value or "")
            market = str(market_value or "")
            target = token == expected_token or market in {expected_market, str(session.get("condition_id") or "")}
            if not target and (not token or not market):
                # Without both identities the account read cannot establish
                # that this open order is outside the selected exposure.
                target = True
            if target and (not order_id or order_id not in owned_ids):
                if self._queue_level_order_exempt(session, order):
                    continue
                return "unowned_target_order"
            if order_id in owned_ids and token and token != expected_token:
                return "unowned_target_order"
        return None

    def _snapshot_trade_events(
        self,
        session: Mapping[str, object],
        snapshot: Mapping[str, object],
    ) -> list[dict[str, object]]:
        """Retain official matched trade facts needed for report cutoffs."""

        own_order_ids = set(self._session_order_ids(session))
        market = snapshot.get("market")
        market_facts = market if isinstance(market, Mapping) else {}
        events: list[dict[str, object]] = []
        for trade in _items(snapshot.get("trades")):
            status = str(_field(trade, "status", "")).upper()
            if status == "FAILED":
                continue
            if status != "CONFIRMED":
                continue
            trade_id = str(_field(trade, "trade_id", _field(trade, "id", "")) or "")
            if not trade_id:
                continue
            maker_orders = _items(_field(trade, "maker_orders", ()))
            taker_order_id = str(_field(trade, "taker_order_id", "") or "")
            candidates: list[tuple[object, str, str]] = []
            for order in maker_orders:
                order_id = self._order_id(order)
                if order_id in own_order_ids:
                    candidates.append((order, "maker", order_id))
            if taker_order_id in own_order_ids:
                candidates.append((trade, "taker", taker_order_id))
            for order, role, order_id in candidates:
                side = str(
                    _field(order, "side", _field(trade, "side", ""))
                ).upper()
                amount = _maybe_decimal(
                    _field(
                        order,
                        "matched_amount",
                        _field(order, "size", _field(order, "quantity", _field(trade, "size"))),
                    )
                )
                price = _maybe_decimal(_field(order, "price", _field(trade, "price")))
                if side not in {"BUY", "SELL"} or amount is None or price is None:
                    continue
                fee = _maybe_decimal(
                    _field(
                        order,
                        "fee",
                        _field(order, "fees", _field(trade, "fee", _field(trade, "fees"))),
                    )
                )
                if fee is None and role == "taker":
                    rate = _maybe_decimal(
                        market_facts.get("taker_fee_rate", market_facts.get("fee_rate"))
                    )
                    exponent = _maybe_decimal(market_facts.get("fee_exponent", 1))
                    if rate is not None and exponent is not None and rate >= 0 and exponent >= 0:
                        fee = (
                            amount
                            * rate
                            * (price * (Decimal("1") - price)) ** exponent
                        ).quantize(Decimal("0.00001"))
                elif fee is None and role == "maker":
                    maker_fee = _maybe_decimal(market_facts.get("fee"))
                    if market_facts.get("fees_enabled") is False:
                        fee = Decimal("0")
                    elif maker_fee == 0:
                        fee = Decimal("0")
                matched_at_value = _field(
                    trade,
                    "matched_at",
                    _field(
                        trade,
                        "match_time",
                        _field(trade, "updated_at", _field(trade, "timestamp")),
                    ),
                )
                try:
                    matched_at = _iso(_timestamp(matched_at_value))
                except ValueError:
                    matched_at = None
                events.append(
                    {
                        "trade_id": trade_id,
                        "order_id": order_id,
                        "matched_at": matched_at,
                        "status": "CONFIRMED",
                        "side": side,
                        "quantity": amount,
                        "price": price,
                        "fee": fee,
                    }
                )
        return events

    def _merge_report_trade_events(
        self,
        session: Mapping[str, object],
        snapshot: Mapping[str, object],
    ) -> list[dict[str, object]]:
        merged: dict[tuple[str, str], dict[str, object]] = {}
        existing = session.get("trade_events")
        for raw_event in _items(existing):
            if not isinstance(raw_event, Mapping):
                continue
            trade_id = str(raw_event.get("trade_id") or "")
            order_id = str(raw_event.get("order_id") or "")
            if trade_id:
                merged[(trade_id, order_id)] = dict(raw_event)
        for event in self._snapshot_trade_events(session, snapshot):
            key = (str(event["trade_id"]), str(event["order_id"]))
            current = merged.setdefault(key, {})
            current.update({name: value for name, value in event.items() if value is not None})
            if event.get("matched_at") is None:
                current.setdefault("matched_at", None)
            if event.get("fee") is None:
                current.setdefault("fee", None)
        return list(merged.values())

    @staticmethod
    def _report_fee_total(
        trade_events: Sequence[Mapping[str, object]], side: str
    ) -> Decimal | None:
        fees = [
            _maybe_decimal(event.get("fee"))
            for event in trade_events
            if str(event.get("side") or "").upper() == side
        ]
        if any(fee is None for fee in fees):
            return None
        return sum((fee for fee in fees if fee is not None), Decimal("0"))

    def _fill_patch(
        self, session: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> dict[str, object]:
        account = snapshot.get("account")
        if not isinstance(account, Mapping):
            raise ValueError("account_unknown")
        if "positions" not in account or account.get("positions") is None:
            raise ValueError("position_unknown")
        book = snapshot.get("book")
        if not isinstance(book, Mapping):
            raise ValueError("book_unknown")
        book_received_at = book.get("received_at")
        if book_received_at is None:
            raise ValueError("book_freshness_unknown")
        _freshness(book_received_at, self._now(), "book_freshness")
        token_id = session["token_id"]
        entry_order_id = str(session.get("entry_order_id") or "")
        history = self._order_history(session)
        # Issue 167: BUY economics merge across the group — the entry order
        # plus every augment order (each price level) feeds buy_filled /
        # buy_cost, so a fill at any level triggers D3 and the $5 stop loss.
        buy_order_ids = sorted(
            order_id
            for order_id in self._session_order_ids(session)
            if order_id
            and str(history.get(order_id, {}).get("side") or "").upper() == "BUY"
        )
        quantity = Decimal("0")
        cost = Decimal("0")
        for buy_order_id in buy_order_ids:
            current_quantity, current_cost = self._trade_totals(
                snapshot,
                buy_order_id,
                "BUY",
                token_id=str(token_id),
            )
            quantity += current_quantity
            cost += current_cost
        sell_order_ids = {
            order_id
            for order_id in self._session_order_ids(session)
            if order_id != entry_order_id
            and str(history.get(order_id, {}).get("side") or "SELL").upper() == "SELL"
        }
        sold_quantity = Decimal("0")
        sold_revenue = Decimal("0")
        for order_id in sell_order_ids - {""}:
            current_quantity, current_revenue = self._trade_totals(
                snapshot, order_id, "SELL", token_id=str(token_id)
            )
            sold_quantity += current_quantity
            sold_revenue += current_revenue
        residual = self._position_quantity(account, str(token_id))
        residual_value = self._executable_bid_value(snapshot, residual)
        trade_events = self._merge_report_trade_events(session, snapshot)
        buy_fees = self._report_fee_total(trade_events, "BUY")
        sell_fees = self._report_fee_total(trade_events, "SELL")
        fees = self._known_trade_fees(snapshot, session, token_id=str(token_id))
        projected_fee = self._projected_taker_fee(snapshot, residual)
        fees_known = fees is not None and projected_fee is not None
        if fees is not None and projected_fee is not None:
            fees += projected_fee
        else:
            fees = None
        # Issue 167: the ordered ceiling is the group's total BUY quantity
        # (entry + augments); single-order groups fall back to ``quantity``.
        requested_quantity = _maybe_decimal(session.get("group_buy_quantity")) or (
            _maybe_decimal(session.get("quantity"))
        )
        if requested_quantity is None:
            raise ValueError("opening_quantity_unknown")
        if quantity > requested_quantity:
            raise ValueError("opening_quantity_exceeded")
        if sold_quantity > quantity:
            raise ValueError("sold_quantity_exceeded")
        expected_residual = quantity - sold_quantity
        position_reconciled = (quantity == 0 and residual == 0) or residual == expected_residual
        patch: dict[str, object] = {
            "buy_filled_quantity": quantity,
            "buy_cost": cost,
            "sold_quantity": sold_quantity,
            "sold_revenue": sold_revenue,
            "buy_fees": buy_fees,
            "sell_fees": sell_fees,
            "residual_quantity": residual,
            "residual_exit_value": residual_value,
            "projected_exit_fee": projected_fee,
            "trade_events": trade_events,
            "fees": fees,
            "fee_status": "known" if fees_known else "unknown",
            "position_reconciled": position_reconciled,
            "account_checked_at": snapshot.get("account_checked_at", self._now()),
            "book_checked_at": book_received_at,
            "orders_terminal": bool(session.get("orders_terminal")),
        }
        if not position_reconciled:
            patch["reconciliation"] = "position_mismatch"
        reward = snapshot.get("reward_status")
        if reward in {"known", "unknown"}:
            patch["reward_status"] = reward
        return patch

    @staticmethod
    def _position_quantity(account: Mapping[str, object], token_id: str) -> Decimal:
        total = Decimal("0")
        for position in _items(account.get("positions")):
            token = _field(position, "token_id", _field(position, "asset_id"))
            if token != token_id:
                continue
            amount = _maybe_decimal(_field(position, "size", _field(position, "quantity", 0)))
            if amount is None:
                raise ValueError("position_size_unknown")
            if amount < 0:
                raise ValueError("position_invalid")
            total += amount
        return total

    def _trade_totals(
        self,
        snapshot: Mapping[str, object],
        order_id: str,
        side: str,
        *,
        token_id: str | None = None,
    ) -> tuple[Decimal, Decimal]:
        if not order_id:
            return Decimal("0"), Decimal("0")
        quantity = Decimal("0")
        proceeds = Decimal("0")
        seen_trades: set[tuple[str, str]] = set()
        for trade in _items(snapshot.get("trades")):
            status = str(_field(trade, "status", "")).upper()
            maker_orders = _items(_field(trade, "maker_orders", ()))
            taker_order = str(_field(trade, "taker_order_id", "") or "")
            candidates: list[tuple[object, str]] = []
            for order in maker_orders:
                if self._order_id(order) == order_id:
                    candidates.append((order, "maker"))
            if taker_order == order_id:
                candidates.append((trade, "taker"))
            if not candidates:
                continue
            if not status:
                raise ValueError("trade_status_unknown")
            if status == "FAILED":
                continue
            if status != "CONFIRMED":
                raise ValueError("trade_not_confirmed")
            trade_id = str(_field(trade, "trade_id", _field(trade, "id", "")) or "")
            if not trade_id:
                raise ValueError("trade_identity_unknown")
            for order, role in candidates:
                identity = (
                    trade_id,
                    order_id if role == "taker" else self._order_id(order),
                )
                if identity in seen_trades:
                    continue
                seen_trades.add(identity)
                current_token = _field(
                    order,
                    "token_id",
                    _field(order, "asset_id", _field(trade, "token_id", _field(trade, "asset_id"))),
                )
                if token_id is not None:
                    if current_token in (None, ""):
                        raise ValueError("trade_token_unknown")
                    if str(current_token) != token_id:
                        raise ValueError("trade_token_mismatch")
                current_side = str(_field(order, "side", _field(trade, "side", ""))).upper()
                if not current_side:
                    raise ValueError("trade_side_unknown")
                if current_side != side:
                    raise ValueError("trade_side_mismatch")
                amount = _maybe_decimal(
                    _field(
                        order,
                        "matched_amount",
                        _field(order, "size", _field(order, "quantity", None)),
                    )
                )
                price = _maybe_decimal(_field(order, "price", _field(trade, "price", None)))
                if amount is None or amount <= 0:
                    raise ValueError("trade_amount_unknown")
                if price is None or price <= 0:
                    raise ValueError("trade_price_unknown")
                quantity += amount
                proceeds += amount * price
        return quantity, proceeds

    def _known_trade_fees(
        self,
        snapshot: Mapping[str, object],
        session: Mapping[str, object],
        *,
        token_id: str,
    ) -> Decimal | None:
        own_ids = set(self._session_order_ids(session))
        seen: set[tuple[str, str]] = set()
        total = Decimal("0")
        for trade in _items(snapshot.get("trades")):
            trade_id = str(_field(trade, "trade_id", _field(trade, "id", "")) or "")
            status = str(_field(trade, "status", "")).upper()
            maker_orders = list(_items(_field(trade, "maker_orders", ())))
            taker_order = str(_field(trade, "taker_order_id", "") or "")
            candidates: list[tuple[object, str]] = [
                (order, "maker")
                for order in maker_orders
                if self._order_id(order) in own_ids
            ]
            if taker_order in own_ids:
                candidates.append((trade, "taker"))
            if not candidates:
                continue
            if not trade_id or not status:
                return None
            if status == "FAILED":
                continue
            if status != "CONFIRMED":
                return None
            for order, role in candidates:
                order_id = taker_order if role == "taker" else self._order_id(order)
                identity = (trade_id, order_id)
                if identity in seen:
                    continue
                seen.add(identity)
                current_token = _field(
                    order,
                    "token_id",
                    _field(order, "asset_id", _field(trade, "token_id", _field(trade, "asset_id"))),
                )
                if current_token in (None, "") or str(current_token) != token_id:
                    return None
                amount = _maybe_decimal(
                    _field(
                        order,
                        "matched_amount",
                        _field(order, "size", _field(trade, "size", None)),
                    )
                )
                price = _maybe_decimal(_field(order, "price", _field(trade, "price", None)))
                if amount is None or price is None or amount <= 0 or price <= 0:
                    return None
                fee = _maybe_decimal(
                    _field(
                        order,
                        "fee",
                        _field(order, "fees", _field(trade, "fee", _field(trade, "fees"))),
                    )
                )
                if fee is None:
                    market = snapshot.get("market")
                    if not isinstance(market, Mapping):
                        return None
                    if role == "taker":
                        rate = _maybe_decimal(
                            market.get("taker_fee_rate", market.get("fee_rate"))
                        )
                        exponent = _maybe_decimal(market.get("fee_exponent", 1))
                        if rate is None or exponent is None or rate < 0 or exponent < 0:
                            return None
                        fee = (
                            amount
                            * rate
                            * (price * (Decimal("1") - price)) ** exponent
                        ).quantize(Decimal("0.00001"))
                    else:
                        maker_fee = _maybe_decimal(market.get("fee"))
                        if market.get("fees_enabled") is False:
                            maker_fee = Decimal("0")
                        if maker_fee is None or maker_fee < 0 or maker_fee != 0:
                            return None
                        fee = Decimal("0")
                total += fee
        return total

    @staticmethod
    def _projected_taker_fee(
        snapshot: Mapping[str, object], residual: Decimal
    ) -> Decimal | None:
        return _projected_taker_fee(snapshot, residual)

    @staticmethod
    def _executable_bid_value(
        snapshot: Mapping[str, object], quantity: Decimal
    ) -> Decimal | None:
        return _executable_bid_value(snapshot, quantity)

    def _orders_terminal(
        self, snapshot: Mapping[str, object], session: Mapping[str, object]
    ) -> bool:
        del snapshot
        order_ids = self._session_order_ids(session)
        if not order_ids:
            return False
        history = self._order_history(session)
        return all(
            str(history.get(order_id, {}).get("status") or "").upper()
            in TERMINAL_ORDER_STATES
            for order_id in order_ids
        )

    def _order_terminal(
        self,
        snapshot: Mapping[str, object],
        order_id: str,
        session: Mapping[str, object] | None = None,
    ) -> bool:
        if not order_id:
            return False
        for order in _items(snapshot.get("orders")):
            current_id = self._order_id(order)
            if current_id != order_id:
                continue
            status = str(_field(order, "status", "")).upper()
            return status in TERMINAL_ORDER_STATES
        if session is not None:
            status = str(self._order_history(session).get(order_id, {}).get("status") or "").upper()
            return status in TERMINAL_ORDER_STATES
        return False

    @staticmethod
    def _scoring_reset_patch(
        order_id: str | None = None, role: str | None = None
    ) -> dict[str, object]:
        return {
            "scoring_status": "unknown",
            "scoring_checked_at": None,
            "scoring_order_id": order_id or None,
            "scoring_order_role": role if order_id else None,
            "scoring_lost_at": None,
        }

    def _update_scoring(self, session: Mapping[str, object], snapshot: Mapping[str, object]) -> None:
        del snapshot
        state = str(session.get("state") or "")
        entry_id = str(session.get("entry_order_id") or "")
        passive_id = str(session.get("passive_exit_order_id") or "")
        protected_id = str(session.get("protected_exit_order_id") or "")
        if state.startswith("entry"):
            order_id, role = entry_id, "entry"
        elif state == "passive_exit":
            order_id, role = passive_id, "passive_exit"
        elif state == "stop_loss_exit":
            order_id, role = protected_id, "protected_exit"
        else:
            order_id, role = "", ""

        now = self._now()
        previous_id = str(session.get("scoring_order_id") or "")
        previous_role = str(session.get("scoring_order_role") or "")
        previous_checked = session.get("scoring_checked_at")
        target_changed = previous_id != order_id or previous_role != role
        previous_status = str(session.get("scoring_status") or "unknown")
        value: bool | None = None
        checked_at = previous_checked
        queried = False
        if order_id:
            due = target_changed or previous_checked is None
            if not due and previous_checked is not None:
                try:
                    age = (now - _timestamp(previous_checked, name="scoring_checked_at")).total_seconds()
                    due = age >= float(SCORING_POLL_SECONDS)
                except ValueError:
                    due = True
            if due:
                queried = True
                method = getattr(self.exchange, "get_order_scoring", None)
                if callable(method):
                    try:
                        response = method(order_id)
                        value = response if response is True or response is False else None
                    except Exception:
                        value = None
                checked_at = _iso(now)
            elif not target_changed and previous_status in {"true", "false"}:
                try:
                    age = (now - _timestamp(previous_checked, name="scoring_checked_at")).total_seconds()
                    if 0 <= age <= float(SCORING_STALE_SECONDS):
                        value = previous_status == "true"
                except ValueError:
                    value = None
        status = "true" if value is True else "false" if value is False else "unknown"
        lost_at = session.get("scoring_lost_at")
        if role != "entry":
            lost_at = None
        elif value is True:
            # Only fresh evidence for this exact current order can clear the
            # continuous-loss window.
            if queried or (previous_checked is not None and status == "true"):
                lost_at = None
        elif lost_at is None:
            lost_at = _iso(now)
        patch: dict[str, object] = {
            "scoring_status": status,
            "scoring_checked_at": checked_at,
            "scoring_order_id": order_id or None,
            "scoring_order_role": role if order_id else None,
            "scoring_lost_at": lost_at,
        }
        updated = self.store.lp_update_session(str(session["session_id"]), patch=patch)
        if role != "entry" or value is True:
            return
        lost_at_value = updated.get("scoring_lost_at")
        if lost_at_value is None:
            return
        try:
            age = (now - _timestamp(lost_at_value, name="scoring_lost_at")).total_seconds()
        except ValueError:
            age = 0
        if age < float(SCORING_FAILURE_WINDOW_SECONDS):
            return
        # The window ends the opening stage.  Loss/account reconciliation
        # remains independent and is handled by the normal tick path.
        current = self.store.lp_session(str(session["session_id"])) or updated
        try:
            self._cancel_entry_order(current)
        except Exception as exc:
            self.store.lp_update_session(
                str(session["session_id"]),
                state="needs_attention",
                patch={
                    "reconciliation": f"scoring_cancel_{type(exc).__name__}",
                    "resume_state": "entry_open",
                },
            )
            return
        self.store.lp_update_session(
            str(session["session_id"]),
            state="review",
            patch={"review_status": "scoring_continuously_lost"},
        )

    def _cancel_entry_order(self, session: Mapping[str, object]) -> None:
        order_id = str(session.get("entry_order_id") or "")
        if not order_id:
            return
        if not self._cancel_order(order_id):
            raise RuntimeError("cancel_not_acknowledged")

    def _request_entry_cancel(self, session: Mapping[str, object]) -> None:
        order_id = str(session.get("entry_order_id") or "")
        if not order_id:
            return
        try:
            if not self._cancel_order(order_id):
                raise RuntimeError("cancel_not_acknowledged")
        except Exception as exc:
            self.store.lp_update_session(
                str(session["session_id"]),
                state="needs_attention",
                patch={"reconciliation": f"entry_cancel_{type(exc).__name__}"},
            )
            return
        self.store.lp_update_session(
            str(session["session_id"]), patch={"entry_cancel_requested": True}
        )

    def _cancel_passive_order(self, session: Mapping[str, object]) -> None:
        order_id = str(session.get("passive_exit_order_id") or "")
        if order_id:
            self._cancel_order(order_id)

    def _request_passive_cancel(self, session: Mapping[str, object]) -> dict[str, object]:
        order_id = str(session.get("passive_exit_order_id") or "")
        if not order_id or bool(session.get("passive_cancel_requested")):
            return dict(session)
        try:
            if not self._cancel_order(order_id):
                raise RuntimeError("cancel_not_acknowledged")
        except Exception as exc:
            return self.store.lp_update_session(
                str(session["session_id"]),
                state="needs_attention",
                patch={"reconciliation": f"passive_cancel_{type(exc).__name__}"},
            )
        return self.store.lp_update_session(
            str(session["session_id"]), patch={"passive_cancel_requested": True}
        )

    def _cancel_owned_orders(self, session: Mapping[str, object]) -> None:
        current = dict(session)
        history = self._order_history(current)
        errors: list[BaseException] = []
        for key, requested_key, role in (
            ("entry_order_id", "entry_cancel_requested", "entry"),
            ("passive_exit_order_id", "passive_cancel_requested", "passive_exit"),
        ):
            order_id = str(current.get(key) or "")
            if not order_id:
                continue
            if bool(current.get(requested_key)):
                continue
            if str(history.get(order_id, {}).get("status") or "").upper() in TERMINAL_ORDER_STATES:
                continue
            try:
                if not self._cancel_order(order_id):
                    raise RuntimeError("cancel_not_acknowledged")
            except Exception as exc:
                errors.append(exc)
                continue
            self.store.lp_upsert_action(
                str(current["session_id"]),
                self._action_key(str(current["session_id"]), f"{role}-cancel", order_id),
                state="accepted",
                payload={"role": role, "order_id": order_id},
            )
            current = self.store.lp_update_session(
                str(current["session_id"]), patch={requested_key: True}
            )
            history = self._order_history(current)
        # Issue 158: augment orders join the same review/stop cancel sweep so
        # an entry plus its augments always end (or complete) together.
        augment_requested = [
            str(value) for value in _items(current.get("augment_cancel_requested"))
        ]
        for value in _items(current.get("augment_order_ids")):
            order_id = str(value or "")
            if not order_id or order_id in augment_requested:
                continue
            if str(history.get(order_id, {}).get("status") or "").upper() in TERMINAL_ORDER_STATES:
                continue
            try:
                if not self._cancel_order(order_id):
                    raise RuntimeError("cancel_not_acknowledged")
            except Exception as exc:
                errors.append(exc)
                continue
            self.store.lp_upsert_action(
                str(current["session_id"]),
                self._action_key(str(current["session_id"]), "augment-cancel", order_id),
                state="accepted",
                payload={"role": "augment-cancel", "order_id": order_id},
            )
            augment_requested = [*augment_requested, order_id]
            current = self.store.lp_update_session(
                str(current["session_id"]),
                patch={"augment_cancel_requested": list(augment_requested)},
            )
            history = self._order_history(current)
        if errors:
            raise RuntimeError(type(errors[0]).__name__) from errors[0]

    @staticmethod
    def _cancel_acknowledged(response: object, order_id: str) -> bool:
        """Accept only a response that names the requested canceled order."""

        target = str(order_id)
        if not target or response is None or isinstance(response, bool):
            return False
        if isinstance(response, str):
            return response == target
        if isinstance(response, Mapping):
            for key in (
                "canceled",
                "cancelled",
                "canceled_order_ids",
                "cancelled_order_ids",
                "order_ids",
            ):
                values = _field(response, key, None)
                if isinstance(values, str):
                    if values == target:
                        return True
                elif values is not None:
                    try:
                        if target in {str(value) for value in values}:
                            return True
                    except TypeError:
                        pass
            not_canceled = _field(response, "not_canceled", _field(response, "not_cancelled", None))
            if isinstance(not_canceled, Mapping) and target in {str(key) for key in not_canceled}:
                return False
            response_id = _field(response, "order_id", _field(response, "id", ""))
            status = str(_field(response, "status", "")).upper()
            return str(response_id or "") == target and status in TERMINAL_ORDER_STATES
        try:
            values = tuple(response)  # type: ignore[arg-type]
        except TypeError:
            response_id = _field(response, "order_id", _field(response, "id", ""))
            status = str(_field(response, "status", "")).upper()
            return str(response_id or "") == target and status in TERMINAL_ORDER_STATES
        return target in {str(value) for value in values}

    def _cancel_order(self, order_id: str) -> bool:
        self._require_mutation()
        direct = getattr(self.exchange, "cancel_order", None)
        if callable(direct):
            response = direct(order_id)
            return self._cancel_acknowledged(response, order_id)
        method = getattr(self.exchange, "cancel_orders", None)
        if callable(method):
            response = method((order_id,))
            return self._cancel_acknowledged(response, order_id)
        raise RuntimeError("cancel_adapter_unavailable")

    @staticmethod
    def _has_unresolved_submission(session: Mapping[str, object]) -> bool:
        """Return whether any durable submit intent still lacks a terminal receipt."""

        if str(session.get("state") or "") == "entry_submit_pending":
            return True
        if str(session.get("submit_status") or "") in {
            "pending",
            "unknown",
            "accepted_without_order_id",
        }:
            return True
        return any(
            str(session.get(key) or "")
            in {"pending", "unknown", "accepted_without_order_id"}
            for key in (
                "passive_exit_attempt_state",
                "protected_exit_attempt_state",
            )
        )

    def _ensure_passive_exit(
        self, session: Mapping[str, object], snapshot: Mapping[str, object], quantity: Decimal
    ) -> None:
        # A submit that may have reached the venue is durable before the POST.
        # Pending, unknown, and accepted-without-ID states are never safe to
        # replay merely because a monitoring process restarted.
        if self._has_unresolved_submission(session):
            return
        attempt_state = str(session.get("passive_exit_attempt_state") or "")
        if attempt_state in {"pending", "unknown", "accepted_without_order_id"}:
            return
        book = snapshot.get("book")
        if not isinstance(book, Mapping):
            return
        stamp = book.get("received_at")
        if stamp is None:
            return
        try:
            _freshness(stamp, self._now(), "book_freshness")
            asks = self._external_asks(session, snapshot)
        except ValueError as exc:
            self.store.lp_update_session(
                str(session["session_id"]),
                state="needs_attention",
                patch={"reconciliation": str(exc), "resume_state": "passive_exit"},
            )
            return
        if not asks:
            return
        price = asks[0][0]
        old_price = _maybe_decimal(session.get("passive_exit_price"))
        old_order = str(session.get("passive_exit_order_id") or "")
        if (
            old_order
            and old_price == price
            and not bool(session.get("passive_cancel_requested"))
            and not self._order_terminal(snapshot, old_order, session)
        ):
            return
        if old_order:
            if not bool(session.get("passive_cancel_requested")):
                try:
                    if not self._cancel_order(old_order):
                        raise RuntimeError("cancel_not_acknowledged")
                except Exception as exc:
                    self.store.lp_update_session(
                        str(session["session_id"]),
                        state="needs_attention",
                        patch={"reconciliation": f"passive_cancel_{type(exc).__name__}"},
                    )
                    return
                self.store.lp_update_session(
                    str(session["session_id"]),
                    patch={"passive_cancel_requested": True},
                )
                return
            if not self._order_terminal(snapshot, old_order, session):
                return
            session = self.store.lp_update_session(
                str(session["session_id"]),
                patch={
                    "passive_exit_order_id": None,
                    "passive_exit_price": None,
                    "passive_cancel_requested": False,
                },
            )
        expiration = expiration_for_review(
            _timestamp(session["review_at"], name="review_at"), now=self._now()
        )
        session_id = str(session["session_id"])
        attempt_key = self._action_key(
            session_id, "passive-submit", uuid.uuid4().hex
        )
        self.store.lp_upsert_action(
            session_id,
            attempt_key,
            state="pending",
            payload={
                "role": "passive_exit",
                "side": "SELL",
                "token_id": session["token_id"],
                "price": price,
                "quantity": quantity,
            },
        )
        self.store.lp_update_session(
            session_id,
            state="passive_exit",
            patch={
                "passive_exit_attempt_key": attempt_key,
                "passive_exit_attempt_state": "pending",
                "passive_exit_retryable": False,
                **self._scoring_reset_patch(),
            },
        )
        try:
            signed = self._create_limit(
                token_id=str(session["token_id"]),
                price=price,
                quantity=quantity,
                side="SELL",
                post_only=True,
                expiration=expiration,
            )
            response = self._post_limit(signed)
        except Exception as exc:
            self.store.lp_upsert_action(
                session_id,
                attempt_key,
                state="unknown",
                payload={"role": "passive_exit", "side": "SELL", "error": type(exc).__name__},
            )
            self.store.lp_update_session(
                session_id,
                state="needs_attention",
                patch={
                    "reconciliation": f"passive_submit_{type(exc).__name__}",
                    "resume_state": "passive_exit",
                    "passive_exit_attempt_state": "unknown",
                    "passive_exit_retryable": False,
                    **self._scoring_reset_patch(),
                },
            )
            return
        accepted, order_id = self._order_response(response)
        if not accepted:
            self.store.lp_upsert_action(
                session_id,
                attempt_key,
                state="rejected",
                payload={
                    "role": "passive_exit",
                    "side": "SELL",
                    "order_id": order_id,
                    "reason": "explicit_zero_fill_rejection",
                },
            )
            self.store.lp_update_session(
                session_id,
                state="passive_exit",
                patch={
                    "reconciliation": "passive_rejected",
                    "passive_exit_attempt_state": "rejected",
                    "passive_exit_retryable": True,
                    **self._scoring_reset_patch(),
                },
            )
            return
        if not order_id:
            self.store.lp_upsert_action(
                session_id,
                attempt_key,
                state="accepted",
                payload={"role": "passive_exit", "side": "SELL", "order_id": ""},
            )
            self.store.lp_update_session(
                session_id,
                state="needs_attention",
                patch={
                    "reconciliation": "passive_order_id_unknown",
                    "resume_state": "passive_exit",
                    "passive_exit_attempt_state": "accepted_without_order_id",
                    "passive_exit_retryable": False,
                    **self._scoring_reset_patch(),
                },
            )
            return
        history = self._order_history(session)
        history[order_id] = {
            "order_id": order_id,
            "token_id": session["token_id"],
            "side": "SELL",
            "status": str(_field(response, "status", "LIVE")).upper() or "LIVE",
            "price": price,
            "quantity": quantity,
            "expiration": expiration,
        }
        order_ids = self._session_order_ids(session)
        if order_id not in order_ids:
            order_ids.append(order_id)
        self.store.lp_upsert_action(
            session_id,
            attempt_key,
            state="accepted",
            payload={
                "role": "passive_exit",
                "side": "SELL",
                "order_id": order_id,
                "price": price,
                "quantity": quantity,
            },
        )
        self.store.lp_update_session(
            session_id,
            state="passive_exit",
            patch={
                "passive_exit_order_id": order_id,
                "passive_exit_price": price,
                "passive_cancel_requested": False,
                "passive_exit_attempt_key": attempt_key,
                "passive_exit_attempt_state": "accepted",
                "passive_exit_retryable": False,
                **self._scoring_reset_patch(order_id, "passive_exit"),
                "owned_order_ids": order_ids,
                "order_history": history,
            },
        )

    def _external_asks(
        self, session: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> list[tuple[Decimal, Decimal]]:
        book = snapshot.get("book")
        if not isinstance(book, Mapping):
            raise ValueError("book_unknown")
        levels = self._levels(book.get("asks"), "asks")
        aggregate: dict[Decimal, Decimal] = {}
        for price, size in levels:
            aggregate[price] = aggregate.get(price, Decimal("0")) + size
        history = self._order_history(session)
        own_sizes: dict[Decimal, Decimal] = {}
        current_passive_id = str(session.get("passive_exit_order_id") or "")
        for order_id in self._session_order_ids(session):
            record = history.get(order_id, {})
            side = str(record.get("side") or "").upper()
            if side != "SELL" or str(record.get("status") or "").upper() in TERMINAL_ORDER_STATES:
                continue
            order = next(
                (candidate for candidate in _items(snapshot.get("orders")) if self._order_id(candidate) == order_id),
                None,
            )
            price = _maybe_decimal(_field(order, "price", record.get("price")))
            if price is None and order_id == current_passive_id:
                price = _maybe_decimal(session.get("passive_exit_price"))
            if price is None:
                continue
            remaining = _maybe_decimal(
                _field(
                    order,
                    "remaining_size",
                    _field(order, "size", _field(order, "quantity", None)),
                )
            )
            if remaining is None:
                original = _maybe_decimal(_field(order, "original_size", record.get("quantity")))
                matched = _maybe_decimal(_field(order, "size_matched", Decimal("0")))
                if original is not None and matched is not None:
                    remaining = max(Decimal("0"), original - matched)
            if remaining is None and order_id == current_passive_id:
                remaining = _maybe_decimal(session.get("residual_quantity"))
            if remaining is not None and remaining > 0:
                own_sizes[price] = own_sizes.get(price, Decimal("0")) + remaining
        result: list[tuple[Decimal, Decimal]] = []
        for price, size in sorted(aggregate.items()):
            external_size = size - own_sizes.get(price, Decimal("0"))
            if external_size > 0:
                result.append((price, external_size))
        return result

    def _reconcile_protected_exit(
        self, session: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> dict[str, object]:
        order_id = str(session.get("protected_exit_order_id") or "")
        if not order_id:
            return session
        for order in _items(snapshot.get("orders")):
            current_id = str(_field(order, "order_id", _field(order, "id", "")))
            if current_id != order_id:
                continue
            status = str(_field(order, "status", "")).upper()
            if status not in TERMINAL_ORDER_STATES:
                return session
            session_id = str(session["session_id"])
            if status in {"REJECTED", "FAILED", "CANCELED", "CANCELLED", "EXPIRED"}:
                return self.store.lp_update_session(
                    session_id,
                    patch={
                        "protected_exit_order_id": None,
                        "protected_exit_attempt_state": "rejected",
                        "protected_exit_retryable": True,
                    },
                )
            prior_sold = _maybe_decimal(
                session.get("protected_exit_submit_sold_quantity")
            )
            prior_residual = _maybe_decimal(
                session.get("protected_exit_submit_residual_quantity")
            )
            residual = _maybe_decimal(session.get("residual_quantity"))
            submit_quantity = _maybe_decimal(
                session.get("protected_exit_submit_quantity")
            )
            current_fill = Decimal("0")
            try:
                current_fill, _ = self._trade_totals(
                    snapshot,
                    order_id,
                    "SELL",
                    token_id=str(session.get("token_id") or ""),
                )
            except ValueError:
                current_fill = Decimal("0")
            reconciled = (
                session.get("position_reconciled") is True
                and prior_residual is not None
                and residual is not None
                and submit_quantity is not None
                and current_fill >= submit_quantity
                and residual <= prior_residual - current_fill
            )
            if not reconciled:
                # A terminal venue status alone is not a fill receipt. Keep the
                # active ID until this order's own fills and the actual position
                # facts reconcile.
                return self.store.lp_update_session(
                    session_id,
                    patch={"protected_exit_attempt_state": "terminal"},
                )
            return self.store.lp_update_session(
                session_id,
                patch={
                    "protected_exit_order_id": None,
                    "protected_exit_attempt_state": "terminal",
                    "protected_exit_retryable": False,
                    "protected_exit_submit_quantity": None,
                    "protected_exit_submit_sold_quantity": None,
                    "protected_exit_submit_residual_quantity": None,
                },
            )
        return session

    def _submit_protected_exit(
        self,
        session: Mapping[str, object],
        quantity: Decimal,
        snapshot: Mapping[str, object] | None = None,
    ) -> None:
        method = getattr(self.exchange, "submit_protected_sell", None)
        if quantity <= 0 or session.get("protected_exit_order_id"):
            return
        if self._has_unresolved_submission(session):
            return
        available = _maybe_decimal(session.get("residual_quantity"))
        if session.get("position_reconciled") is not True or available is None or quantity > available:
            self.store.lp_update_session(
                str(session["session_id"]),
                patch={
                    "exit_error": "quantity_reconciliation_unknown",
                    "protected_exit_retryable": False,
                },
            )
            return
        attempt_state = str(session.get("protected_exit_attempt_state") or "")
        if attempt_state in {"pending", "unknown", "accepted_without_order_id"}:
            return
        if attempt_state == "rejected" and session.get("protected_exit_retryable") is not True:
            return
        if not callable(method):
            method = getattr(self.exchange, "submit_fok_sell", None)
        if not callable(method):
            self.store.lp_update_session(
                str(session["session_id"]),
                state="needs_attention",
                patch={"exit_error": "protected_exit_adapter_unavailable"},
            )
            return
        if not self._mutation_allowed("submit"):
            self.store.lp_update_session(
                str(session["session_id"]),
                state="needs_attention",
                patch={
                    "exit_error": "mutation_blocked",
                    "resume_state": "stop_loss_exit",
                    "protected_exit_attempt_state": "unknown",
                },
            )
            return
        min_price = Decimal("0")
        submit_quantity = quantity
        if snapshot is not None:
            book = snapshot.get("book")
            if not isinstance(book, Mapping):
                self.store.lp_update_session(
                    str(session["session_id"]),
                    patch={"exit_error": "book_unknown", "protected_exit_retryable": False},
                )
                return
            book_received_at = book.get("received_at")
            if book_received_at is None:
                self.store.lp_update_session(
                    str(session["session_id"]),
                    patch={"exit_error": "book_freshness_unknown", "protected_exit_retryable": False},
                )
                return
            try:
                _freshness(book_received_at, self._now(), "book_freshness")
            except ValueError as exc:
                self.store.lp_update_session(
                    str(session["session_id"]),
                    patch={"exit_error": str(exc), "protected_exit_retryable": False},
                )
                return
            bids = self._levels(book.get("bids"), "bids")
            if not bids:
                self.store.lp_update_session(
                    str(session["session_id"]),
                    patch={"exit_error": "bid_unknown", "protected_exit_retryable": False},
                )
                return
            # A protected FOK must be bounded by the fresh executable depth.
            # Use the lowest price included in the quantity as the floor so a
            # multi-level fill remains protected, and never submit an
            # unbounded or zero-price order when the book is incomplete.
            remaining = quantity
            used_levels: list[tuple[Decimal, Decimal]] = []
            for price, size in sorted(bids, reverse=True):
                used = min(size, remaining)
                if used <= 0:
                    continue
                used_levels.append((price, used))
                remaining -= used
                if remaining <= 0:
                    break
            if not used_levels:
                self.store.lp_update_session(
                    str(session["session_id"]),
                    patch={"exit_error": "bid_unknown", "protected_exit_retryable": False},
                )
                return
            submit_quantity = sum(size for _, size in used_levels)
            minimum = _maybe_decimal(
                _field(session.get("preflight"), "minimum_order_size")
            )
            if minimum is not None and submit_quantity < minimum:
                self.store.lp_update_session(
                    str(session["session_id"]),
                    patch={
                        "exit_error": "exit_depth_below_minimum",
                        "protected_exit_retryable": False,
                    },
                )
                return
            min_price = used_levels[-1][0]
        if min_price <= 0 or submit_quantity <= 0:
            self.store.lp_update_session(
                str(session["session_id"]),
                patch={"exit_error": "bid_unknown", "protected_exit_retryable": False},
            )
            return
        session_id = str(session["session_id"])
        attempt_key = self._action_key(
            session_id, "protected-submit", uuid.uuid4().hex
        )
        prior_sold = _maybe_decimal(session.get("sold_quantity"))
        prior_residual = _maybe_decimal(session.get("residual_quantity"))
        self.store.lp_upsert_action(
            session_id,
            attempt_key,
            state="pending",
            payload={
                "role": "protected_exit",
                "side": "SELL",
                "token_id": session["token_id"],
                "quantity": submit_quantity,
                "min_price": min_price,
            },
        )
        self.store.lp_update_session(
            session_id,
            patch={
                "protected_exit_attempt_key": attempt_key,
                "protected_exit_attempt_state": "pending",
                "protected_exit_retryable": False,
                "protected_exit_submit_quantity": submit_quantity,
                "protected_exit_submit_sold_quantity": prior_sold,
                "protected_exit_submit_residual_quantity": prior_residual,
                **self._scoring_reset_patch(),
            },
        )
        try:
            response = method(
                token_id=str(session["token_id"]),
                quantity=submit_quantity,
                min_price=min_price,
            )
        except Exception as exc:
            self.store.lp_upsert_action(
                session_id,
                attempt_key,
                state="unknown",
                payload={"role": "protected_exit", "side": "SELL", "error": type(exc).__name__},
            )
            self.store.lp_update_session(
                session_id,
                patch={
                    "protected_exit_attempt_state": "unknown",
                    "protected_exit_retryable": False,
                    "exit_error": type(exc).__name__,
                    **self._scoring_reset_patch(),
                },
            )
            return
        accepted, order_id = self._order_response(response)
        if not accepted:
            self.store.lp_upsert_action(
                session_id,
                attempt_key,
                state="rejected",
                payload={
                    "role": "protected_exit",
                    "side": "SELL",
                    "order_id": order_id,
                    "reason": "explicit_zero_fill_rejection",
                },
            )
            self.store.lp_update_session(
                session_id,
                patch={
                    "exit_error": "protected_exit_rejected",
                    "protected_exit_attempt_state": "rejected",
                    "protected_exit_retryable": True,
                    **self._scoring_reset_patch(),
                },
            )
            return
        if not order_id:
            self.store.lp_upsert_action(
                session_id,
                attempt_key,
                state="accepted",
                payload={"role": "protected_exit", "side": "SELL", "order_id": ""},
            )
            self.store.lp_update_session(
                session_id,
                patch={
                    "protected_exit_attempt_state": "accepted_without_order_id",
                    "protected_exit_retryable": False,
                    "exit_error": "order_id_unknown",
                    **self._scoring_reset_patch(),
                },
            )
            return
        history = self._order_history(session)
        history[order_id] = {
            "order_id": order_id,
            "token_id": session["token_id"],
            "side": "SELL",
            "status": str(_field(response, "status", "LIVE")).upper() or "LIVE",
            "quantity": submit_quantity,
            "min_price": min_price,
        }
        order_ids = self._session_order_ids(session)
        if order_id not in order_ids:
            order_ids.append(order_id)
        self.store.lp_upsert_action(
            session_id,
            attempt_key,
            state="accepted",
            payload={
                "role": "protected_exit",
                "side": "SELL",
                "order_id": order_id,
                "quantity": submit_quantity,
                "min_price": min_price,
            },
        )
        self.store.lp_update_session(
            session_id,
            patch={
                "protected_exit_order_id": order_id,
                "protected_exit_attempt_key": attempt_key,
                "protected_exit_attempt_state": "accepted",
                "protected_exit_retryable": False,
                "protected_exit_submit_quantity": submit_quantity,
                "protected_exit_submit_sold_quantity": prior_sold,
                "protected_exit_submit_residual_quantity": prior_residual,
                **self._scoring_reset_patch(order_id, "protected_exit"),
                "owned_order_ids": order_ids,
                "order_history": history,
            },
        )

    def _review_iteration(
        self, session: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> dict[str, object]:
        if (
            session.get("stop_requested") is True
            or session.get("review_status") == "awaiting_reconciliation"
        ):
            try:
                self._cancel_owned_orders(session)
            except Exception as exc:
                updated = self.store.lp_update_session(
                    str(session["session_id"]),
                    state="needs_attention",
                    patch={
                        "reconciliation": f"review_cancel_{type(exc).__name__}",
                        "resume_state": "review",
                    },
                )
                return self._status_payload(updated)
            session = self.store.lp_session(str(session["session_id"])) or session
        updated = self.store.lp_update_session(
            str(session["session_id"]),
            patch={"review_status": "awaiting_reconciliation"},
        )
        loss = self._opening_loss_from_session(updated)
        updated = self.store.lp_update_session(
            str(session["session_id"]), patch={"opening_loss": loss}
        )
        latched = bool(updated.get("stop_loss_latched"))
        if loss is None and not latched:
            return self._complete_if_flat(updated, snapshot)
        if (loss is not None and loss >= STOP_LOSS) or latched:
            updated = self.store.lp_update_session(
                str(session["session_id"]),
                state="stop_loss_exit",
                patch=self._stop_loss_latch_patch(updated, loss),
            )
            updated = self._request_passive_cancel(updated)
            if not updated.get("passive_exit_order_id") or bool(updated.get("orders_terminal")):
                self._submit_protected_exit(
                    updated,
                    _decimal(updated.get("residual_quantity", 0), "residual_quantity"),
                    snapshot,
                )
            updated = self.store.lp_session(str(session["session_id"])) or updated
        return self._complete_if_flat(updated, snapshot)

    def _complete_if_flat(
        self, session: Mapping[str, object], snapshot: Mapping[str, object]
    ) -> dict[str, object]:
        del snapshot
        if self._has_unresolved_submission(session):
            return self._status_payload(session)
        residual = _decimal(session.get("residual_quantity", 0), "residual_quantity")
        bought = _decimal(session.get("buy_filled_quantity", 0), "buy_filled_quantity")
        sold = _decimal(session.get("sold_quantity", 0), "sold_quantity")
        if (
            residual == 0
            and bought == sold
            and bool(session.get("position_reconciled"))
            and bool(session.get("orders_terminal"))
        ):
            sold_revenue = _maybe_decimal(session.get("sold_revenue"))
            buy_cost = _maybe_decimal(session.get("buy_cost"))
            fees = _maybe_decimal(session.get("fees"))
            pnl = (
                sold_revenue - buy_cost - fees
                if sold_revenue is not None and buy_cost is not None and fees is not None
                else None
            )
            updated = self.store.lp_update_session(
                str(session["session_id"]),
                state="complete",
                patch={"trade_pnl": pnl, "total_pnl": None},
            )
            return self._status_payload(updated)
        return self._status_payload(session)

    def _status_payload(self, session: Mapping[str, object]) -> dict[str, object]:
        result = dict(session)
        result.setdefault("session_id", session.get("session_id"))
        # Issue 158: informational-only default quantity for the augment
        # "加 5%" option (the entry candidate row's estimated target).  It is
        # never validated and never used for order sizing — every augment
        # quantity goes through augment_preview/augment validation.
        result["estimated_target_quantity"] = _maybe_decimal(
            result.get("estimated_target_quantity")
        )
        result["reward_observation"] = self._reward_status_payload(session)
        # Issue 167: expose the normalized per-bucket view (v2 + the
        # single-bucket projection for one-level groups) so every read
        # path — status, tick aggregation, dashboard — speaks one shape.
        protection = result.get("queue_protection")
        if isinstance(protection, Mapping) and protection:
            result["queue_protection"] = queue_protection_status_view(protection)
        for key in (
            "price",
            "quantity",
            "buy_filled_quantity",
            "buy_cost",
            "sold_quantity",
            "sold_revenue",
            "residual_quantity",
            "residual_exit_value",
            "fees",
            "opening_loss",
            "stop_loss_triggered_loss",
            "paid_rewards",
            "trade_pnl",
            "total_pnl",
        ):
            value = result.get(key)
            if isinstance(value, str):
                parsed = _maybe_decimal(value)
                if parsed is not None:
                    result[key] = parsed
        return result

    def _reward_status_payload(self, session: Mapping[str, object]) -> dict[str, object]:
        raw = session.get("reward_observation")
        if isinstance(raw, Mapping):
            observation = dict(raw)
        else:
            observation = {
                "status": "unknown",
                "threshold_status": "unknown",
                "reward_date": self._session_reward_date(session),
                "condition_id": _text(session.get("condition_id")),
                "source": "platform_earnings",
                "currency": "USD",
                "paid": False,
            }
        for key in ("market_amount", "account_amount", "gap"):
            value = observation.get(key)
            if isinstance(value, str):
                parsed = _maybe_decimal(value)
                if parsed is not None:
                    observation[key] = parsed
        status = str(observation.get("status") or "unknown").lower()
        checked_at = observation.get("last_success_at", observation.get("checked_at"))
        if status in {"below", "met"} and checked_at is not None:
            try:
                age = (self._now() - _timestamp(checked_at, name="reward_checked_at")).total_seconds()
            except ValueError:
                age = float("inf")
            if age < 0 or age > float(REWARD_STALE_SECONDS):
                observation["status"] = "unknown"
                observation["threshold_status"] = "unknown"
                observation["stale"] = True
        return observation


__all__ = [
    "GTD_REVIEW_BUFFER_SECONDS",
    "PolymarketLPService",
    "SCORING_FAILURE_WINDOW_SECONDS",
    "SCORING_STALE_SECONDS",
    "SDK_MIN_EXPIRATION_SECONDS",
    "STOP_LOSS",
    "expiration_for_review",
]
