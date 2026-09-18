"""Durable, single-market Polymarket liquidity-provider session."""

from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

from .polymarket_lp_risk import (
    BOOK_FRESHNESS_SECONDS,
    TERMINAL_ORDER_STATES,
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
)
from .prediction_arbitrage_store import PredictionArbitrageStore


STOP_LOSS = Decimal("5")
SCORING_STALE_SECONDS = Decimal("15")
SCORING_POLL_SECONDS = Decimal("5")
SCORING_FAILURE_WINDOW_SECONDS = Decimal("60")
GTD_REVIEW_BUFFER_SECONDS = 60
SDK_MIN_EXPIRATION_SECONDS = 180
PREVIEW_TTL_SECONDS = 10
REWARD_THRESHOLD = Decimal("1")
REWARD_STALE_SECONDS = Decimal("180")
LP_CANDIDATE_REFRESH_SECONDS = Decimal("300")
LP_RECOMMENDATION_REFRESH_SECONDS = Decimal("60")
_LP_BOOK_SAMPLE_BATCH_SIZE = 100
_LP_BOOK_SAMPLE_MAX_CONCURRENCY = 8
_LP_PRICE_HISTORY_BATCH_SIZE = 20
_LP_PRICE_HISTORY_MAX_CONCURRENCY = 4
_LP_PRICE_HISTORY_WINDOW = timedelta(hours=24)
_LP_PRICE_HISTORY_OVERLAP = timedelta(minutes=1)
_LP_METADATA_BATCH_SIZE = 1500
_LP_PREPARATION_RETRY_SECONDS = 300
_BEIJING = ZoneInfo("Asia/Shanghai")
TERMINAL_TRADE_STATES = frozenset({"CONFIRMED", "FAILED"})


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


def _lp_funnel_conditions() -> dict[str, object]:
    """Return the user-facing rules applied by every LP funnel batch."""

    return {
        "read": {
            "来源": "奖励目录与市场资料",
            "完整性": "完整目录；部分结果可参与筛选；缺失资料=UNKNOWN",
        },
        "filter": {
            "奖励": "奖励启用且日奖池>0",
            "市场": "接受订单",
            "参与": "没有已知订单或持仓",
            "窗口": "24h",
            "粒度": "1m",
            "振幅": "不超过1¢",
            "刷新": "每小时",
            "有效期": "24h",
            "缺失": "UNKNOWN",
        },
        "risk": {
            "奖励与市场资料": "60s内",
            "盘口与账户": "10s内；订单与持仓资料完整",
            "事件": "开始前30分钟、进行中、结束后1h冷却；结束后筛选必须通过；缺失=UNKNOWN",
            "入场压力": "最小数量、奖励价带、资金预留、含费压力退出不超过10%",
            "排序": "日奖池降序，同额按市场ID升序",
            "上限": 50,
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
        self._mutex = threading.RLock()
        self._reward_refresh_lock = threading.Lock()
        self._price_history_refresh_lock = threading.Lock()
        self._report_lock = threading.Lock()
        self._candidate_refresh_lock = threading.Lock()
        self._candidate_state_lock = threading.RLock()
        self._preparation_lock = threading.RLock()
        self._sample_target_lock = threading.Lock()
        self._sample_targets: tuple[tuple[str, str], ...] = ()
        self._sample_target_version = 0
        self._candidate_attempted_at: datetime | None = None
        self._prepared_inputs: dict[str, object] | None = None
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
        }
        self._preparation: dict[str, object] | None = None
        self._restore_preparation()
        self._restore_candidate_snapshot()

    def set_mutation_guard(self, guard: Callable[..., bool] | None) -> None:
        """Attach the existing execution breaker to exchange writes."""

        self._mutation_guard = guard

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

    def _prepared_input_snapshot(self) -> dict[str, object] | None:
        with self._candidate_state_lock:
            prepared = self._prepared_inputs
        return deepcopy(prepared) if isinstance(prepared, Mapping) else None

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

    def _preparation_result(
        self,
        state: Mapping[str, object],
        *,
        outcome: str,
        reason: str | None = None,
        display_state: str | None = None,
        alert_pending: bool = False,
    ) -> dict[str, object]:
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
        if alert_pending:
            result["alert_pending"] = True
        return result

    @staticmethod
    def _safe_error_type(value: object) -> str:
        text = str(value or "unknown_error")
        return text if text.replace("_", "").isalnum() and text[0:1].isalpha() else "unknown_error"

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
        if attempt >= 2:
            # A process may stop after persisting an attempt and before its
            # external read returns.  The persisted attempt budget covers the
            # whole generation, regardless of the state written by that read;
            # do not turn reconstruction into another unbounded attempt.
            last_error = self._safe_error_type(current.get("last_error"))
            if last_error == "unknown_error":
                last_error = "preparation_attempt_budget_exhausted"
            state = self._save_preparation(
                {
                    "state": "paused",
                    "paused": True,
                    "failure_count": max(2, failures),
                    "last_failure_at": now,
                    "next_retry_at": None,
                    "last_error": last_error,
                },
                expected_generation=generation,
            )
            if state.get("alert_attempted") is not True:
                claimer = getattr(self.store, "lp_claim_preparation_alert", None)
                claimed = (
                    claimer(expected_generation=generation)
                    if callable(claimer)
                    else state
                )
                if isinstance(claimed, Mapping):
                    with self._preparation_lock:
                        self._preparation = deepcopy(dict(claimed))
                    state = deepcopy(dict(claimed))
            return state
        return self._save_preparation(
            {
                "state": "preparing",
                "stage": "catalog",
                "attempt": attempt + 1,
                "last_attempt_at": now,
                "next_retry_at": None,
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
        paused = failures >= 2
        retry_at = None if paused else now + timedelta(seconds=_LP_PREPARATION_RETRY_SECONDS)
        state = self._save_preparation(
            {
                "state": "paused" if paused else "waiting_retry",
                "stage": stage,
                "failure_count": failures,
                "paused": paused,
                "last_failure_at": now,
                "last_error": self._safe_error_type(error_type),
                "next_retry_at": retry_at,
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

    def candidate_snapshot(self) -> dict[str, object]:
        """Return the latest cached candidate projection without external reads."""

        with self._candidate_state_lock:
            snapshot = deepcopy(self._candidate_snapshot)
        now = self._now()
        checked_at = snapshot.get("checked_at")
        if isinstance(checked_at, datetime):
            checked = checked_at
        elif isinstance(checked_at, str):
            try:
                checked = _timestamp(checked_at, name="candidate_checked_at")
            except ValueError:
                checked = None
        else:
            checked = None
        if checked is not None:
            age = Decimal(str((now - checked).total_seconds()))
            # Candidate guidance is refreshed on the minute cadence.  Keep
            # the batch counts and timestamp when that cadence is missed, but
            # present the risk projection as historical at the exact boundary.
            snapshot["stale"] = age < 0 or age >= LP_RECOMMENDATION_REFRESH_SECONDS
        else:
            snapshot["stale"] = True
        snapshot_state = str(snapshot.get("state") or "")
        if snapshot_state not in {"ready", "incomplete", "scanning"} or (
            snapshot.get("state") == "incomplete"
            and snapshot.get("candidate_rows_fresh") is not True
        ):
            snapshot["stale"] = True
        raw_selected_results = snapshot.get("selected_results")
        selected_results = (
            [row for row in raw_selected_results if isinstance(row, dict)]
            if isinstance(raw_selected_results, (list, tuple))
            else []
        )
        if not selected_results:
            selected_results = [
                dict(row)
                for row in snapshot.get("recommendations", ())
                if isinstance(row, Mapping)
            ]
        projection_available = (
            snapshot.get("stale") is not True
            and snapshot.get("state") not in {"stale", "unknown"}
        )
        current_recommendations: list[dict[str, object]] = []
        expired_reasons: list[dict[str, object]] = []
        projection_reasons: list[dict[str, object]] = []
        direction_counts = {"passed": 0, "rejected": 0, "unknown": 0}
        market_counts = {"passed": 0, "rejected": 0, "unknown": 0}
        for row in selected_results:
            directions = row.get("directions")
            if not isinstance(directions, Mapping):
                row["state"] = "unknown"
                market_counts["unknown"] += 1
                continue
            has_eligible = False
            has_unknown = False
            has_expired = False
            direction_states: list[str] = []
            for outcome, raw_direction in directions.items():
                if not isinstance(raw_direction, dict):
                    continue
                direction = raw_direction
                state = str(direction.get("state") or "unknown")
                if state == "eligible" and direction.get("eligible") is True:
                    guidance = direction.get("guidance")
                    if not _lp_guidance_is_usable(guidance):
                        state = "unknown"
                        direction["state"] = state
                        direction["eligible"] = False
                        direction["reason_codes"] = ["guidance_unknown"]
                        projection_reasons.append(
                            {
                                "market_id": row.get("market_id"),
                                "condition_id": row.get("condition_id"),
                                "outcome": str(outcome),
                                "code": "guidance_unknown",
                            }
                        )
                    else:
                        try:
                            expires_at = _timestamp(
                                guidance.get("expires_at"),
                                name="guidance_expiry",
                            )
                        except ValueError:
                            expires_at = None
                        if expires_at is None or now >= expires_at:
                            state = "expired"
                            direction["state"] = state
                            direction["eligible"] = False
                            direction["reason_codes"] = ["guidance_expired"]
                            has_expired = True
                            expired_reasons.append(
                                {
                                    "market_id": row.get("market_id"),
                                    "condition_id": row.get("condition_id"),
                                    "outcome": str(outcome),
                                    "code": "guidance_expired",
                                }
                            )
                if state == "eligible" and not projection_available:
                    state = "unknown"
                    direction["state"] = state
                    direction["eligible"] = False
                    direction["reason_codes"] = ["candidate_snapshot_stale"]
                    projection_reasons.append(
                        {
                            "market_id": row.get("market_id"),
                            "condition_id": row.get("condition_id"),
                            "outcome": str(outcome),
                            "code": "candidate_snapshot_stale",
                        }
                    )
                if state == "expired":
                    has_expired = True
                if state == "unknown":
                    has_unknown = True
                if state == "eligible" and direction.get("eligible") is True and _lp_guidance_is_usable(direction.get("guidance")):
                    has_eligible = True
                direction_states.append(state)
                if state == "eligible" and direction.get("eligible") is True and _lp_guidance_is_usable(direction.get("guidance")):
                    direction_counts["passed"] += 1
                elif state == "rejected":
                    direction_counts["rejected"] += 1
                elif state in {"unknown", "expired"}:
                    direction_counts["unknown"] += 1
            if has_eligible:
                row["state"] = "eligible"
                market_counts["passed"] += 1
                current_recommendations.append(row)
            elif has_unknown:
                row["state"] = "unknown"
                market_counts["unknown"] += 1
            elif has_expired:
                row["state"] = "expired"
                market_counts["unknown"] += 1
            elif direction_states:
                row["state"] = "rejected"
                market_counts["rejected"] += 1
            else:
                row["state"] = "unknown"
                market_counts["unknown"] += 1
        snapshot["selected_results"] = selected_results
        snapshot["recommendations"] = current_recommendations
        funnel = snapshot.get("funnel")
        has_funnel_risk_evidence = isinstance(funnel, Mapping) and (
            "selected" in funnel or "risk" in funnel
        )
        if isinstance(funnel, Mapping) and (
            selected_results or has_funnel_risk_evidence
        ):
            projected_funnel = deepcopy(dict(funnel))
            projected_funnel["selected"] = len(selected_results)
            projected_funnel["risk"] = market_counts
            projected_funnel["risk_directions"] = direction_counts
            reasons = projected_funnel.get("reasons")
            if isinstance(reasons, Mapping):
                projected_reasons = deepcopy(dict(reasons))
                risk_reasons = projected_reasons.get("risk")
                projected_risk_reasons = (
                    [dict(reason) for reason in risk_reasons if isinstance(reason, Mapping)]
                    if isinstance(risk_reasons, (list, tuple))
                    else []
                )
                for reason in [*expired_reasons, *projection_reasons]:
                    if reason not in projected_risk_reasons:
                        projected_risk_reasons.append(reason)
                projected_reasons["risk"] = projected_risk_reasons
                projected_funnel["reasons"] = projected_reasons
            snapshot["funnel"] = projected_funnel
        # Keep preparation lifecycle state adjacent to the cached candidate
        # projection.  It is a small durable row, so readers can show a
        # pending/retry/paused reason without re-running the external funnel.
        snapshot["preparation"] = self.preparation_snapshot()
        return snapshot

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
        try:
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
            catalog_failure_error: str | None = None
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
                raw_markets = catalog.get("markets")
                if not isinstance(raw_markets, (list, tuple)):
                    catalog_failure_error = safe_catalog_error
                    raise ValueError("history_catalog_unknown")
                if catalog.get("state") != "known":
                    catalog_failure_error = safe_catalog_error
                    raise ValueError("history_catalog_unknown")
                market_rows = [row for row in raw_markets if isinstance(row, Mapping)]
                if catalog.get("complete") is not True:
                    if not market_rows:
                        catalog_failure_error = (
                            safe_catalog_error
                            if safe_catalog_error != "history_catalog_unknown"
                            else "history_catalog_unknown"
                        )
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
                metadata_condition_ids: list[str] = []
                for condition_id in condition_ids:
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
                    catalog_failure_error = "history_catalog_unknown"
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
                    reason=self._safe_error_type(error_type),
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
                        for field in ("error", "reason", "code", "type", "status"):
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
                    raw_deferred = batch_value.get("deferred_ids")
                    deferred_ids = {
                        str(value)
                        for value in raw_deferred
                        if isinstance(value, str) and value in requested
                    } if isinstance(raw_deferred, Sequence) and not isinstance(raw_deferred, (str, bytes)) else set()
                    for condition_id, failure in failed_ids.items():
                        metadata_failures[condition_id] = batch_failure_code(failure)
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
                            "last_error": last_error,
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
            market_failures: dict[str, tuple[str, str, str | None]] = {}
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
                for condition_id, (stage, error_type, token_id) in market_failures.items():
                    if condition_id in recorded_failure_conditions:
                        continue
                    preparation_failure_writer(
                        condition_id,
                        generation=generation,
                        stage=stage,
                        error=error_type,
                        failed_at=failed_at,
                        token_id=token_id,
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
                if not requested:
                    return ()
                returned_markets: dict[str, Mapping[str, object]] = {}
                failed_ids: dict[str, str] = {}
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
                                    str(key): self._safe_error_type(value)
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
                    market = returned_markets.get(condition_id)
                    if not isinstance(market, Mapping):
                        failed_ids.setdefault(
                            condition_id, "metadata_batch_incomplete"
                        )
                        market_failures.setdefault(
                            condition_id,
                            (
                                "metadata",
                                self._safe_error_type(failed_ids[condition_id]),
                                None,
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
                            ("metadata", "metadata_market_unusable", None),
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
                            ("metadata", "metadata_market_unusable", None),
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
                            if failure_code is not None:
                                market_failures.setdefault(
                                    condition_id,
                                    ("history", self._safe_error_type(failure_code), token_id),
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
                                summary: dict[str, object] = {
                                    "state": "known",
                                    "amplitude": max(prices) - min(prices),
                                    "checked_at": now,
                                    "window_start": window_start,
                                    "window_end": window_end,
                                "sample_count": len(bounded),
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
                            "last_error": error_type,
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
            for item in remaining_items:
                if not isinstance(item, Mapping) or item.get("state") != "waiting_retry":
                    continue
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
                    "last_success_at": now,
                    "last_progress_at": now,
                    "completed_count": len(targets),
                    "total_count": len(targets),
                    "next_retry_at": next_retry_at,
                    "last_error": None,
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
            self._price_history_refresh_lock.release()

    def _restore_candidate_snapshot(
        self, saved: Mapping[str, object] | None = None
    ) -> None:
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
        # Snapshots written before the light-funnel contract do not carry the
        # stage counts or selected-market boundary. They cannot be presented
        # as a successful result from the new flow after a restart.
        if (
            "funnel" not in saved
            or "selected_market_ids" not in saved
            or not isinstance(saved.get("funnel"), Mapping)
        ):
            with self._candidate_state_lock:
                self._candidate_snapshot.update(
                    {
                        "state": "unknown",
                        "complete": False,
                        "scanning": False,
                        "candidates": [],
                        "recommendations": [],
                        "selected_results": [],
                        "checked_at": None,
                        "last_success_at": None,
                        "last_attempt_at": saved.get("last_attempt_at"),
                        "candidate_rows_fresh": False,
                        "missing_metadata_condition_ids": [],
                        "missing_book_token_ids": [],
                        "catalog_complete": False,
                        "funnel": {},
                        "selected_market_ids": [],
                        "candidate_retention_reason": "legacy_snapshot_unusable",
                    }
                )
            self._candidate_attempted_at = None
            return
        with self._candidate_state_lock:
            for key in (
                "state",
                "complete",
                "candidates",
                "recommendations",
                "selected_results",
                "checked_at",
                "last_success_at",
                "last_attempt_at",
                "candidate_rows_fresh",
                "missing_metadata_condition_ids",
                "missing_book_token_ids",
                "catalog_complete",
                "event_end_confirmations",
                "retention_reason",
                "scan_started_at",
                "funnel",
                "selected_market_ids",
                "candidate_retention_reason",
            ):
                if key in saved:
                    self._candidate_snapshot[key] = deepcopy(saved[key])
            self._candidate_snapshot["scanning"] = False
            started_at = saved.get("scan_started_at")
            try:
                self._candidate_attempted_at = _timestamp(
                    started_at, name="scan_started_at"
                )
            except ValueError:
                self._candidate_attempted_at = None

    def refresh_candidates(
        self,
        *,
        stop_event: threading.Event | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        """Refresh the light shortlist, then risk-check only selected markets."""

        if not self._candidate_refresh_lock.acquire(blocking=False):
            snapshot = self.candidate_snapshot()
            snapshot["scanning"] = True
            return snapshot
        try:
            scan_started_at = self._now()
            with self._candidate_state_lock:
                attempted_at = self._candidate_attempted_at
                if (
                    not force
                    and attempted_at is not None
                    and Decimal(
                        str((scan_started_at - attempted_at).total_seconds())
                    )
                    < LP_RECOMMENDATION_REFRESH_SECONDS
                ):
                    return self.candidate_snapshot()
                previous = dict(self._candidate_snapshot)
                self._candidate_attempted_at = scan_started_at
                self._candidate_snapshot = {
                    **previous,
                    "state": "scanning",
                    "complete": False,
                    "scanning": True,
                    "last_attempt_at": scan_started_at,
                }

            if stop_event is not None and stop_event.is_set():
                return self._finish_candidate_scan(
                    previous,
                    state="unknown",
                    complete=False,
                    checked_at=scan_started_at,
                    scan_started_at=scan_started_at,
                    retention_reason="scan_cancelled",
                )
            catalog_reader = getattr(self.exchange, "lp_reward_catalog", None)
            metadata_reader = getattr(self.exchange, "lp_market_metadata", None)
            account_reader = getattr(self.exchange, "lp_account_snapshot", None)
            books_reader = getattr(self.exchange, "lp_order_books", None)
            if (
                not callable(catalog_reader)
                or not callable(metadata_reader)
                or not callable(account_reader)
            ):
                raise ValueError("candidate_readers_unavailable")
            prepared = self._prepared_input_snapshot()
            if not isinstance(prepared, Mapping):
                return self._finish_candidate_scan(
                    previous,
                    state="stale" if previous.get("last_success_at") else "unknown",
                    complete=False,
                    checked_at=self._now(),
                    scan_started_at=scan_started_at,
                    retention_reason="catalog_preparation_pending",
                )
            catalog = prepared.get("catalog")
            metadata_value = prepared.get("metadata")
            if not isinstance(catalog, Mapping):
                return self._finish_candidate_scan(
                    previous,
                    state="stale" if previous.get("last_success_at") else "unknown",
                    complete=False,
                    checked_at=self._now(),
                    scan_started_at=scan_started_at,
                    retention_reason="reward_catalog_unknown",
                    catalog_complete=False,
                )
            catalog_is_known = (
                isinstance(catalog, Mapping) and catalog.get("state") == "known"
            )
            raw_markets = (
                catalog.get("markets")
                if isinstance(catalog, Mapping)
                else None
            )
            if not isinstance(raw_markets, (list, tuple)):
                return self._finish_candidate_scan(
                    previous,
                    state="stale" if previous.get("last_success_at") else "unknown",
                    complete=False,
                    checked_at=(
                        catalog.get("checked_at")
                        if isinstance(catalog, Mapping)
                        else None
                    )
                    or self._now(),
                    scan_started_at=scan_started_at,
                    retention_reason="reward_catalog_unknown",
                    catalog_complete=False,
                )
            market_rows = [dict(row) for row in raw_markets if isinstance(row, Mapping)]
            if not market_rows:
                if not catalog_is_known or catalog.get("complete") is not True:
                    return self._finish_candidate_scan(
                        previous,
                        state="stale" if previous.get("last_success_at") else "incomplete",
                        complete=False,
                        checked_at=(
                            catalog.get("checked_at")
                            if isinstance(catalog, Mapping)
                            else None
                        )
                        or self._now(),
                        scan_started_at=scan_started_at,
                        retention_reason="reward_catalog_unknown",
                        catalog_complete=False,
                    )
                self._publish_sample_targets(())
                completed_at = self._now()
                return self._finish_candidate_scan(
                    previous,
                    state="ready",
                    complete=True,
                    checked_at=completed_at,
                    scan_started_at=scan_started_at,
                    last_success_at=completed_at,
                    candidates=[],
                    recommendations=[],
                    selected_results=[],
                    missing_metadata_condition_ids=(),
                    missing_book_token_ids=(),
                    catalog_complete=True,
                    funnel={
                        "catalog_read": 0,
                        "base_pass": 0,
                        "volatility_pass": 0,
                        "selected": 0,
                        "risk": {"passed": 0, "rejected": 0, "unknown": 0},
                        "risk_directions": {"passed": 0, "rejected": 0, "unknown": 0},
                        "selected_market_ids": [],
                        "conditions": _lp_funnel_conditions(),
                        "reasons": {
                            "catalog": [],
                            "base": [],
                            "volatility": [],
                            "selected": [],
                            "risk": [],
                        },
                    },
                    selected_market_ids=(),
                )

            condition_ids = tuple(
                dict.fromkeys(
                    str(row.get("condition_id") or "").strip()
                    for row in market_rows
                    if str(row.get("condition_id") or "").strip()
                )
            )
            metadata_observed_at = self._now()
            if not isinstance(metadata_value, Mapping):
                raise ValueError("candidate_market_facts_unknown")
            metadata_by_condition = {
                str(key): value
                for key, value in metadata_value.items()
                if isinstance(key, str) and isinstance(value, Mapping)
            }
            missing_metadata_condition_ids = tuple(
                condition_id for condition_id in condition_ids
                if condition_id not in metadata_by_condition
            )
            account: Mapping[str, object] | None = None
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
            reservations = self._candidate_reservations()
            cache_batch_reader = getattr(
                self.store, "lp_price_history_summaries", None
            )
            cache_reader = getattr(self.store, "lp_price_history_summary", None)
            saved_screening = getattr(self.store, "lp_screening_snapshot", lambda: None)()
            saved_confirmations = (
                saved_screening.get("event_end_confirmations", {})
                if isinstance(saved_screening, Mapping) else {}
            )
            event_end_confirmations = deepcopy(dict(saved_confirmations)) if isinstance(saved_confirmations, Mapping) else {}
            direction_facts: list[dict[str, object]] = []
            complete = catalog_is_known and catalog.get("complete") is True
            complete = complete and not missing_metadata_condition_ids
            cache_identities = tuple(
                (
                    condition_id,
                    str(
                        outcome.get("token_id") or ""
                    ).strip(),
                )
                for reward_market in market_rows
                for condition_id in (str(reward_market.get("condition_id") or "").strip(),)
                for market_meta in (metadata_by_condition.get(condition_id),)
                if isinstance(market_meta, Mapping)
                and isinstance(market_meta.get("outcomes"), Mapping)
                for outcome in cast(Mapping[object, object], market_meta["outcomes"]).values()
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
                for outcome_key, raw_outcome in raw_outcomes.items():
                    if str(outcome_key).lower() not in {"yes", "no"} or not isinstance(raw_outcome, Mapping):
                        continue
                    token_id = str(raw_outcome.get("token_id") or "").strip()
                    if not token_id:
                        complete = False
                        continue
                    market = {
                        **dict(market_meta),
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "outcome": str(raw_outcome.get("label") or outcome_key).upper(),
                        "reward_min_size": reward_minimum,
                        "reward_max_spread": reward_spread,
                    }
                    summary: Mapping[str, object] | None = None
                    cache_key = (condition_id, token_id)
                    cached_summary = cached_summaries.get(cache_key)
                    if isinstance(cached_summary, Mapping):
                        summary = cached_summary
                    elif not callable(cache_batch_reader) and callable(cache_reader):
                        try:
                            cached = cache_reader(condition_id, token_id, now=checked_at)
                        except Exception:
                            cached = None
                        if isinstance(cached, Mapping):
                            summary = cached
                    direction: dict[str, object] = {
                        "market": market,
                        "reward_active": (
                            reward_market.get("reward_active")
                            if isinstance(reward_market.get("reward_active"), bool)
                            else None
                        ),
                        "daily_pool_usd": reward_market.get("daily_pool_usd"),
                        "reward_checked_at": catalog.get("checked_at"),
                        "reward_guidance_deadline": self._reward_guidance_deadline(reward_market),
                        "event_end_confirmation": confirmation,
                    }
                    if summary is not None:
                        direction["history_summary"] = dict(summary)
                    if account is not None and _has_market_order(account, market):
                        direction["known_participation"] = True
                    direction_facts.append(direction)

            from .polymarket_lp_views import _lp_shortlist_rows

            light_rows = _lp_shortlist_rows(direction_facts, now=checked_at)
            shortlist = light_rows[:50]
            selected_condition_ids = tuple(
                str(row.get("condition_id") or "") for row in shortlist
            )
            selected_metadata_by_condition: Mapping[str, object] = {}
            selected_reward_by_condition: dict[str, Mapping[str, object]] = {}
            selected_reward_error_conditions: set[str] = set()
            selected_reward_error_reasons: dict[str, list[str]] = {}
            selected_reward_checked_at: object | None = None
            selected_metadata_error = False
            if selected_condition_ids:
                selected_metadata_reader = getattr(
                    self.exchange, "lp_market_metadata_fresh", None
                )
                if not callable(selected_metadata_reader):
                    selected_metadata_reader = metadata_reader
                try:
                    selected_metadata_value = selected_metadata_reader(
                        selected_condition_ids,
                        stop_event=stop_event,
                    )
                except Exception:
                    selected_metadata_value = None
                if isinstance(selected_metadata_value, Mapping):
                    selected_metadata_by_condition = {
                        str(key): value
                        for key, value in selected_metadata_value.items()
                        if isinstance(key, str) and isinstance(value, Mapping)
                    }
                else:
                    selected_metadata_error = True
                try:
                    selected_reward_value = catalog_reader(
                        condition_ids=selected_condition_ids,
                        stop_event=stop_event,
                    )
                except Exception:
                    selected_reward_value = None
                if isinstance(selected_reward_value, Mapping):
                    selected_reward_checked_at = selected_reward_value.get("checked_at")
                    raw_selected_rewards = selected_reward_value.get("markets")
                    if isinstance(raw_selected_rewards, (list, tuple)):
                        selected_reward_by_condition = {
                            str(row.get("condition_id")): row
                            for row in raw_selected_rewards
                            if isinstance(row, Mapping)
                            and str(row.get("condition_id") or "")
                        }
                        for condition_id in selected_condition_ids:
                            selected_reward = selected_reward_by_condition.get(condition_id)
                            if (
                                selected_reward is None
                                or selected_reward.get("state") == "unknown"
                                or selected_reward.get("complete") is False
                            ):
                                selected_reward_error_conditions.add(condition_id)
                                if isinstance(selected_reward, Mapping):
                                    selected_reward_error_reasons[condition_id] = (
                                        self._safe_selected_reward_reasons(selected_reward)
                                    )
                    else:
                        selected_reward_error_conditions.update(selected_condition_ids)
                else:
                    selected_reward_error_conditions.update(selected_condition_ids)
            selected_event_end_confirmations: dict[str, object] = {}
            selected_event_observation_bound = self._now()
            for condition_id, selected_market_value in selected_metadata_by_condition.items():
                existing_confirmation = event_end_confirmations.get(condition_id)
                selected_confirmation: object = existing_confirmation
                if (
                    selected_market_value.get("event_ended") is True
                    and selected_market_value.get("event_finished_at") is None
                ):
                    try:
                        selected_observed_at = _timestamp(
                            selected_market_value.get("metadata_checked_at"),
                            name="selected_metadata_checked_at",
                        )
                    except ValueError:
                        selected_observed_at = None
                    if (
                        selected_observed_at is not None
                        and selected_observed_at <= selected_event_observation_bound
                    ):
                        confirmation = self._observe_event_end(
                            condition_id,
                            selected_market_value,
                            existing=existing_confirmation,
                            observed_at=selected_observed_at,
                        )
                        if confirmation is not None:
                            selected_confirmation = confirmation
                            event_end_confirmations[condition_id] = confirmation
                elif selected_market_value.get("event_finished_at") is not None:
                    selected_confirmation = None
                selected_event_end_confirmations[condition_id] = selected_confirmation
            selected_direction_keys: set[tuple[str, str, str]] = set()
            for row in shortlist:
                condition_id = str(row.get("condition_id") or "")
                raw_directions = row.get("directions")
                if not isinstance(raw_directions, Sequence):
                    continue
                for row_direction in raw_directions:
                    if not isinstance(row_direction, Mapping):
                        continue
                    outcome = str(row_direction.get("outcome") or "").upper()
                    token_id = str(row_direction.get("token_id") or "")
                    if outcome and token_id:
                        selected_direction_keys.add((condition_id, outcome, token_id))
            selected_directions = [
                direction for direction in direction_facts
                if isinstance(direction.get("market"), Mapping)
                and (
                    str(cast(Mapping[str, object], direction["market"]).get("condition_id") or ""),
                    str(cast(Mapping[str, object], direction["market"]).get("outcome") or "").upper(),
                    str(cast(Mapping[str, object], direction["market"]).get("token_id") or ""),
                ) in selected_direction_keys
            ]
            for direction in selected_directions:
                market = direction.get("market")
                if not isinstance(market, Mapping):
                    continue
                condition_id = str(market.get("condition_id") or "")
                outcome_key = str(market.get("outcome") or "").strip().lower()
                selected_market = selected_metadata_by_condition.get(condition_id)
                selected_outcome: Mapping[str, object] | None = None
                if isinstance(selected_market, Mapping):
                    raw_outcomes = selected_market.get("outcomes")
                    if isinstance(raw_outcomes, Mapping):
                        raw = raw_outcomes.get(outcome_key)
                        if isinstance(raw, Mapping):
                            selected_outcome = raw
                        else:
                            for candidate in raw_outcomes.values():
                                if not isinstance(candidate, Mapping):
                                    continue
                                if (
                                    str(candidate.get("label") or "")
                                    .strip()
                                    .lower()
                                    == outcome_key
                                ):
                                    selected_outcome = candidate
                                    break
                original_token_id = str(market.get("token_id") or "").strip()
                selected_token_id = (
                    str(
                        selected_outcome.get("token_id")
                        or selected_outcome.get("tokenId")
                        or ""
                    ).strip()
                    if selected_outcome is not None
                    else ""
                )
                if selected_outcome is None or not selected_token_id:
                    direction["selected_metadata_unknown"] = True
                    direction["selected_metadata_reason"] = "market_metadata_unknown"
                elif selected_token_id != original_token_id:
                    direction["selected_metadata_unknown"] = True
                    direction["selected_metadata_reason"] = "market_identity_changed"
                elif selected_market.get("accepting_orders") is not True:
                    direction["selected_metadata_unknown"] = True
                    direction["selected_metadata_reason"] = "market_not_accepting_orders"
                else:
                    selected_market_value = {
                        **dict(selected_market),
                        "condition_id": condition_id,
                        "token_id": selected_token_id,
                        "outcome": str(
                            selected_outcome.get("label") or market.get("outcome") or ""
                        ).upper(),
                        "market_id": selected_market.get(
                            "market_id", market.get("market_id")
                        ),
                    }
                    direction["market"] = selected_market_value
                if condition_id in selected_event_end_confirmations:
                    direction["event_end_confirmation"] = (
                        selected_event_end_confirmations[condition_id]
                    )
                selected_reward = selected_reward_by_condition.get(condition_id)
                if condition_id in selected_reward_error_conditions:
                    direction["selected_reward_unknown"] = True
                    direction["selected_reward_reason_codes"] = list(
                        selected_reward_error_reasons.get(condition_id, ())
                    )
                else:
                    assert selected_reward is not None
                    reward_minimum, reward_spread = _lp_reward_terms(
                        selected_market
                        if isinstance(selected_market, Mapping)
                        else {},
                        selected_reward,
                    )
                    selected_market_value = direction.get("market")
                    if isinstance(selected_market_value, dict):
                        selected_market_value["reward_min_size"] = reward_minimum
                        selected_market_value["reward_max_spread"] = reward_spread
                    direction.update(
                        {
                            "reward_active": selected_reward.get("reward_active"),
                            "daily_pool_usd": selected_reward.get("daily_pool_usd"),
                            "reward_checked_at": selected_reward.get(
                                "checked_at", selected_reward_checked_at
                            ),
                            "reward_guidance_deadline": self._reward_guidance_deadline(
                                selected_reward
                            ),
                        }
                    )
            if selected_metadata_error:
                for direction in selected_directions:
                    direction["selected_metadata_unknown"] = True
            books_by_token: Mapping[str, object] = {}
            missing_book_token_ids: list[str] = []
            token_ids = tuple(
                dict.fromkeys(
                    str(cast(Mapping[str, object], direction["market"]).get("token_id") or "")
                    for direction in selected_directions
                    if isinstance(direction.get("market"), Mapping)
                    and str(cast(Mapping[str, object], direction["market"]).get("token_id") or "")
                )
            )
            if token_ids and callable(books_reader):
                try:
                    books_value = books_reader(token_ids, stop_event=stop_event)
                except Exception:
                    # A complete light shortlist remains useful when the
                    # selected risk-book batch is unavailable; risk is then
                    # UNKNOWN for each selected direction.
                    books_value = {}
                if isinstance(books_value, Mapping):
                    books_by_token = books_value
            try:
                risk_account_value = account_reader()
            except Exception:
                # A fresh account read is required for risk, but an outage
                # must preserve the light shortlist and report UNKNOWN risk.
                risk_account_value = None
            risk_account = (
                risk_account_value
                if isinstance(risk_account_value, Mapping)
                and risk_account_value.get("authenticated") is True
                else {}
            )
            # Account and book reads define the risk evaluation instant. Keep
            # the scan timestamp separate so a just-returned fact is never
            # rejected as being in the future.
            evaluation_at = self._now()
            risk_results: dict[tuple[str, str], dict[str, object]] = {}
            from .polymarket_lp_risk import evaluate_lp_entry
            for direction in selected_directions:
                market = direction.get("market")
                if not isinstance(market, Mapping):
                    continue
                condition_id = str(market.get("condition_id") or "")
                token_id = str(market.get("token_id") or "")
                book = books_by_token.get(token_id)
                if direction.get("selected_metadata_unknown"):
                    result = {
                        "state": "unknown",
                        "reason_codes": [
                            str(
                                direction.get(
                                    "selected_metadata_reason",
                                    "market_metadata_unknown",
                                )
                            )
                        ],
                        "guidance": None,
                    }
                elif direction.get("selected_reward_unknown"):
                    reward_reasons = list(
                        direction.get("selected_reward_reason_codes", ())
                    )
                    if "reward_data_unknown" not in reward_reasons:
                        reward_reasons.append("reward_data_unknown")
                    result = {
                        "state": "unknown",
                        "reason_codes": reward_reasons,
                        "guidance": None,
                    }
                elif not isinstance(book, Mapping):
                    missing_book_token_ids.append(token_id)
                    result = {"state": "unknown", "reason_codes": ["book_unknown"], "guidance": None}
                else:
                    summary = direction.get("history_summary")
                    direction["screening"] = {
                        "state": "eligible",
                        "reason_codes": [],
                        "checked_at": summary.get("checked_at")
                        if isinstance(summary, Mapping)
                        else None,
                    }
                    result = evaluate_lp_entry(
                        {**direction, "book": dict(book)},
                        account=risk_account,
                        now=evaluation_at,
                        reservations=reservations,
                    )
                risk_results[(condition_id, str(market.get("outcome") or "").upper())] = result

            selected_results: list[dict[str, object]] = []
            for shortlist_row in shortlist:
                condition_id = str(shortlist_row.get("condition_id") or "")
                directions: dict[str, object] = {}
                for direction in selected_directions:
                    market = direction.get("market")
                    if not isinstance(market, Mapping) or str(market.get("condition_id") or "") != condition_id:
                        continue
                    outcome = str(market.get("outcome") or "").upper()
                    result = risk_results.get((condition_id, outcome), {"state": "unknown", "reason_codes": ["risk_unknown"], "guidance": None})
                    directions[outcome] = {
                        "token_id": market.get("token_id"),
                        "state": result.get("state"),
                        "eligible": result.get("state") == "eligible",
                        "reason_codes": list(result.get("reason_codes", ())),
                        "screening": direction.get("history_summary"),
                        "guidance": result.get("guidance"),
                    }
                states = [
                    str(value.get("state") or "unknown")
                    for value in directions.values() if isinstance(value, Mapping)
                ]
                market_state = "eligible" if "eligible" in states else "unknown" if "unknown" in states else "rejected"
                selected_result = {
                    **dict(shortlist_row),
                    "state": market_state,
                    "selected": True,
                    "directions": directions,
                }
                selected_reward = selected_reward_by_condition.get(condition_id)
                if (
                    selected_reward is not None
                    and condition_id not in selected_reward_error_conditions
                ):
                    selected_result.update(
                        {
                            "daily_pool_usd": selected_reward.get("daily_pool_usd"),
                            "reward_active": selected_reward.get("reward_active"),
                            "reward_checked_at": selected_reward.get(
                                "checked_at", selected_reward_checked_at
                            ),
                            "rewards_min_size": selected_reward.get(
                                "rewards_min_size"
                            ),
                            "rewards_max_spread": selected_reward.get(
                                "rewards_max_spread"
                            ),
                        }
                    )
                selected_results.append(selected_result)
            recommendations = [
                row
                for row in selected_results
                if any(
                    isinstance(direction, Mapping)
                    and direction.get("state") == "eligible"
                    and _lp_guidance_is_usable(direction.get("guidance"))
                    for direction in (
                        row.get("directions", {}).values()
                        if isinstance(row.get("directions"), Mapping)
                        else ()
                    )
                )
            ]
            complete = complete and not any(
                row.get("state") == "unknown" for row in selected_results
            )
            risk_counts = {
                "passed": sum(1 for row in selected_results if row.get("state") == "eligible"),
                "rejected": sum(1 for row in selected_results if row.get("state") == "rejected"),
                "unknown": sum(1 for row in selected_results if row.get("state") == "unknown"),
            }
            direction_risk_counts = {
                "passed": sum(
                    1
                    for result in risk_results.values()
                    if result.get("state") == "eligible"
                ),
                "rejected": sum(
                    1
                    for result in risk_results.values()
                    if result.get("state") == "rejected"
                ),
                "unknown": sum(
                    1
                    for result in risk_results.values()
                    if result.get("state") == "unknown"
                ),
            }
            base_markets = {
                str(cast(Mapping[str, object], direction["market"]).get("condition_id") or "")
                for direction in direction_facts
                if direction.get("reward_active") is True
                and not direction.get("known_participation")
                and _maybe_decimal(direction.get("daily_pool_usd")) is not None
                and cast(Decimal, _maybe_decimal(direction.get("daily_pool_usd"))) > 0
                and isinstance(direction.get("market"), Mapping)
                and cast(Mapping[str, object], direction["market"]).get("accepting_orders") is True
            }
            volatility_markets = {
                str(row.get("condition_id") or "") for row in shortlist
            }
            reason_rows: dict[str, list[dict[str, object]]] = {
                "catalog": [],
                "base": [],
                "volatility": [],
                "selected": [],
                "risk": [],
            }
            seen_reasons: set[tuple[str, str, str, str]] = set()

            def add_reason(
                stage: str,
                direction: Mapping[str, object] | None,
                code: str,
                *,
                outcome: str | None = None,
            ) -> None:
                market = direction.get("market") if isinstance(direction, Mapping) else None
                market_map = market if isinstance(market, Mapping) else {}
                condition_id = str(
                    market_map.get("condition_id")
                    or (direction or {}).get("condition_id")
                    or ""
                ).strip()
                market_id = str(
                    market_map.get("market_id")
                    or condition_id
                ).strip()
                if not market_id:
                    return
                outcome_value = str(
                    outcome
                    or market_map.get("outcome")
                    or (direction or {}).get("outcome")
                    or ""
                ).upper()
                identity = (stage, market_id, outcome_value, code)
                if identity in seen_reasons:
                    return
                seen_reasons.add(identity)
                row: dict[str, object] = {
                    "market_id": market_id,
                    "condition_id": condition_id,
                    "code": code,
                }
                if outcome_value:
                    row["outcome"] = outcome_value
                reason_rows[stage].append(row)

            directions_by_condition: dict[str, list[dict[str, object]]] = {}
            for direction in direction_facts:
                market = direction.get("market")
                market_map = market if isinstance(market, Mapping) else {}
                condition_id = str(market_map.get("condition_id") or "").strip()
                if condition_id:
                    directions_by_condition.setdefault(condition_id, []).append(direction)
            for condition_id in condition_ids:
                if condition_id not in metadata_by_condition:
                    add_reason(
                        "catalog",
                        {"market": {"condition_id": condition_id, "market_id": condition_id}},
                        "market_metadata_unknown",
                    )
            if catalog.get("complete") is not True:
                for condition_id in condition_ids:
                    add_reason(
                        "catalog",
                        {"market": {"condition_id": condition_id, "market_id": condition_id}},
                        "catalog_incomplete",
                    )
            for condition_id, directions in directions_by_condition.items():
                if condition_id in base_markets:
                    continue
                for direction in directions:
                    market = direction.get("market")
                    market_map = market if isinstance(market, Mapping) else {}
                    reward_active = direction.get("reward_active")
                    if reward_active is None:
                        add_reason("base", direction, "reward_status_unknown")
                    elif reward_active is False:
                        add_reason("base", direction, "reward_inactive")
                    pool = _maybe_decimal(direction.get("daily_pool_usd"))
                    if pool is None:
                        add_reason("base", direction, "reward_pool_unknown")
                    elif pool <= 0:
                        add_reason("base", direction, "reward_pool_empty")
                    if market_map.get("accepting_orders") is not True:
                        add_reason(
                            "base",
                            direction,
                            "market_not_accepting_orders"
                            if market_map.get("accepting_orders") is False
                            else "market_status_unknown",
                        )
                    if any(
                        direction.get(key) is True or market_map.get(key) is True
                        for key in (
                            "participating",
                            "already_participating",
                            "known_participation",
                        )
                    ):
                        add_reason("base", direction, "market_already_participating")
            for condition_id in base_markets:
                if condition_id in volatility_markets:
                    continue
                for direction in directions_by_condition.get(condition_id, ()):
                    summary = direction.get("history_summary")
                    if not isinstance(summary, Mapping):
                        add_reason("volatility", direction, "history_summary_unknown")
                        continue
                    state = str(summary.get("state") or "").lower()
                    if state not in {"known", "ready", "eligible"}:
                        add_reason("volatility", direction, "history_summary_unknown")
                        continue
                    amplitude = _maybe_decimal(summary.get("amplitude"))
                    if amplitude is None:
                        add_reason("volatility", direction, "history_amplitude_unknown")
                    elif amplitude < 0 or amplitude > Decimal("0.01"):
                        add_reason("volatility", direction, "history_amplitude_exceeded")
                    try:
                        history_checked_at = _timestamp(
                            summary.get("checked_at", summary.get("updated_at")),
                            name="history_checked_at",
                        )
                        history_age = (checked_at - history_checked_at).total_seconds()
                    except ValueError:
                        add_reason("volatility", direction, "history_time_unknown")
                    else:
                        if history_age < 0 or history_age >= _LP_PRICE_HISTORY_WINDOW.total_seconds():
                            add_reason("volatility", direction, "history_summary_expired")
                        valid_until = summary.get("valid_until")
                        if valid_until is not None:
                            try:
                                if checked_at >= _timestamp(valid_until, name="history_valid_until"):
                                    add_reason("volatility", direction, "history_summary_expired")
                            except ValueError:
                                add_reason("volatility", direction, "history_time_unknown")
            for row in light_rows[50:]:
                add_reason("selected", {"market": row}, "shortlist_cap")
            for row in selected_results:
                condition_id = str(row.get("condition_id") or "")
                for outcome, direction in (
                    row.get("directions", {}).items()
                    if isinstance(row.get("directions"), Mapping)
                    else ()
                ):
                    if not isinstance(direction, Mapping) or direction.get("state") == "eligible":
                        continue
                    reasons = direction.get("reason_codes")
                    if isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes)) and reasons:
                        for reason in reasons:
                            add_reason(
                                "risk",
                                {"market": {"condition_id": condition_id, "market_id": row.get("market_id"), "outcome": outcome}},
                                str(reason),
                                outcome=str(outcome),
                            )
                    else:
                        add_reason(
                            "risk",
                            {"market": {"condition_id": condition_id, "market_id": row.get("market_id"), "outcome": outcome}},
                            "risk_unknown",
                            outcome=str(outcome),
                        )
            funnel = {
                "catalog_read": len(condition_ids),
                "base_pass": len(base_markets),
                "volatility_pass": len(light_rows),
                "selected": len(shortlist),
                "risk": risk_counts,
                "risk_directions": direction_risk_counts,
                "selected_market_ids": [str(row.get("market_id") or "") for row in shortlist],
                "conditions": _lp_funnel_conditions(),
                "reasons": reason_rows,
            }
            self._publish_sample_targets(())
            completed_at = evaluation_at
            return self._finish_candidate_scan(
                previous,
                state="ready" if complete else "incomplete",
                complete=complete,
                checked_at=completed_at,
                scan_started_at=scan_started_at,
                last_success_at=completed_at if complete else None,
                candidates=[],
                recommendations=recommendations,
                selected_results=selected_results,
                missing_metadata_condition_ids=missing_metadata_condition_ids,
                missing_book_token_ids=tuple(dict.fromkeys(missing_book_token_ids)),
                catalog_complete=catalog_is_known and catalog.get("complete") is True,
                event_end_confirmations=event_end_confirmations,
                funnel=funnel,
                selected_market_ids=tuple(
                    str(row.get("market_id") or "") for row in shortlist
                ),
            )
        except Exception:
            previous = self.candidate_snapshot()
            return self._finish_candidate_scan(
                previous,
                state="stale" if previous.get("last_success_at") else "unknown",
                complete=False,
                checked_at=self._now(),
                scan_started_at=scan_started_at,
                retention_reason="candidate_refresh_failed",
            )
        finally:
            self._candidate_refresh_lock.release()

    def _candidate_reservations(self) -> tuple[dict[str, object], ...]:
        reservations: list[dict[str, object]] = []
        session = self.store.lp_active_session()
        if session is not None:
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

    @staticmethod
    def _merge_recommendations(
        previous_rows: object,
        fresh_rows: Sequence[Mapping[str, object]],
        *,
        observations: Mapping[tuple[str, str], Mapping[str, object]],
        retention_reason: str,
    ) -> list[dict[str, object]]:
        current = [deepcopy(dict(row)) for row in fresh_rows if isinstance(row, Mapping)]
        current_by_condition = {
            str(row.get("condition_id") or ""): row
            for row in current
            if str(row.get("condition_id") or "")
        }
        for row in current:
            directions = row.get("directions")
            if isinstance(directions, dict):
                for direction in directions.values():
                    if isinstance(direction, dict):
                        direction["eligible"] = direction.get("state") == "eligible"

        previous = (
            [deepcopy(dict(row)) for row in previous_rows if isinstance(row, Mapping)]
            if isinstance(previous_rows, (list, tuple))
            else []
        )
        for old_row in previous:
            condition_id = str(old_row.get("condition_id") or "")
            if not condition_id:
                continue
            old_directions = old_row.get("directions")
            if not isinstance(old_directions, Mapping):
                continue
            new_row = current_by_condition.get(condition_id)
            if new_row is None:
                new_row = deepcopy(old_row)
                new_row["directions"] = {}
                new_row["state"] = "expired"
                current.append(new_row)
                current_by_condition[condition_id] = new_row
            new_directions = new_row.get("directions")
            if not isinstance(new_directions, dict):
                new_directions = {}
                new_row["directions"] = new_directions
            for outcome, old_direction in old_directions.items():
                outcome_key = str(outcome).upper()
                if outcome_key in new_directions or not isinstance(old_direction, Mapping):
                    continue
                expired = deepcopy(dict(old_direction))
                observed = observations.get((condition_id, outcome_key))
                reasons = (
                    list(observed.get("reason_codes", ()))
                    if isinstance(observed, Mapping)
                    else []
                )
                expired.update(
                    {
                        "state": "expired",
                        "eligible": False,
                        "reason_codes": reasons or [retention_reason],
                    }
                )
                new_directions[outcome_key] = expired
            if new_directions and all(
                not isinstance(direction, Mapping)
                or direction.get("eligible") is not True
                for direction in new_directions.values()
            ):
                new_row["state"] = "expired"

        return current

    def _fresh_candidate_row(
        self,
        identity: Mapping[str, object],
        snapshot: Mapping[str, object],
        *,
        now: datetime,
    ) -> Mapping[str, object]:
        """Reapply the catalog's shared eligibility rules to live facts."""

        account = snapshot.get("account")
        market = snapshot.get("market")
        book = snapshot.get("book")
        if not isinstance(account, Mapping) or not isinstance(market, Mapping):
            raise ValueError("candidate_facts_unknown")
        if not isinstance(book, Mapping):
            raise ValueError("candidate_facts_unknown")

        catalog_reader = getattr(self.exchange, "lp_reward_catalog", None)
        if not callable(catalog_reader):
            raise ValueError("candidate_reward_unknown")
        try:
            catalog = catalog_reader()
        except Exception as exc:
            raise ValueError("candidate_reward_unknown") from exc
        if (
            not isinstance(catalog, Mapping)
            or catalog.get("state") != "known"
            or catalog.get("complete") is not True
        ):
            raise ValueError("candidate_reward_unknown")

        condition_id = str(identity.get("condition_id") or "")
        reward_market = next(
            (
                row
                for row in _items(catalog.get("markets"))
                if isinstance(row, Mapping)
                and str(row.get("condition_id") or "") == condition_id
            ),
            None,
        )
        if not isinstance(reward_market, Mapping):
            raise ValueError("candidate_reward_inactive")
        pool = _maybe_decimal(reward_market.get("daily_pool_usd"))
        if pool is None or pool <= 0:
            raise ValueError("candidate_reward_unknown")

        from .polymarket_lp_views import lp_candidate_rows

        rows = lp_candidate_rows(
            [
                {
                    "market": market,
                    "book": book,
                    "reward_active": True,
                    "daily_pool_usd": pool,
                    "reward_checked_at": catalog.get("checked_at"),
                }
            ],
            account=account,
            now=now,
            reservations=self._candidate_reservations(),
        )
        for row in rows:
            if all(
                str(row.get(key) or "") == str(identity.get(key) or "")
                for key in ("market_id", "condition_id", "token_id", "outcome")
            ):
                return row
        raise ValueError("candidate_not_eligible")

    def _finish_candidate_scan(
        self,
        previous: Mapping[str, object],
        *,
        state: str,
        complete: bool,
        checked_at: object,
        scan_started_at: datetime | None = None,
        last_success_at: object | None = None,
        candidates: Sequence[Mapping[str, object]] | None = None,
        recommendations: Sequence[Mapping[str, object]] | None = None,
        selected_results: Sequence[Mapping[str, object]] | None = None,
        missing_metadata_condition_ids: Sequence[str] | None = None,
        missing_book_token_ids: Sequence[str] | None = None,
        catalog_complete: bool | None = None,
        event_end_confirmations: Mapping[str, object] | None = None,
        funnel: Mapping[str, object] | None = None,
        selected_market_ids: Sequence[str] | None = None,
        retention_reason: str = "candidate_refresh_failed",
    ) -> dict[str, object]:
        attempted: datetime
        try:
            attempted = _timestamp(checked_at, name="candidate_checked_at")
        except ValueError:
            attempted = self._now()
        has_new_rows = candidates is not None
        successful = complete and has_new_rows
        try:
            last_successful_check = (
                _timestamp(
                    previous.get("checked_at"),
                    name="candidate_checked_at",
                )
                if not has_new_rows
                else attempted
            )
        except ValueError:
            last_successful_check = None
        rows = (
            [deepcopy(dict(row)) for row in candidates]
            if candidates is not None
            else [
                deepcopy(dict(row))
                for row in previous.get("candidates", ())
                if isinstance(row, Mapping)
            ]
        )
        previous_recommendations = previous.get("recommendations")
        recommendation_rows = (
            [deepcopy(dict(row)) for row in recommendations if isinstance(row, Mapping)]
            if recommendations is not None
            else self._merge_recommendations(
                previous_recommendations,
                (),
                observations={},
                retention_reason=retention_reason,
            )
        )
        selected_result_rows = (
            [
                deepcopy(dict(row))
                for row in selected_results
                if isinstance(row, Mapping)
            ]
            if selected_results is not None
            else [
                deepcopy(dict(row))
                for row in previous.get("selected_results", ())
                if isinstance(row, Mapping)
            ]
        )
        try:
            scan_started = (
                scan_started_at.astimezone(UTC)
                if isinstance(scan_started_at, datetime)
                else _timestamp(scan_started_at, name="scan_started_at")
            )
        except ValueError:
            scan_started = attempted
        if missing_metadata_condition_ids is None:
            missing_metadata = list(previous.get("missing_metadata_condition_ids", ()))
        else:
            missing_metadata = list(missing_metadata_condition_ids)
        if missing_book_token_ids is None:
            missing_books = list(previous.get("missing_book_token_ids", ()))
        else:
            missing_books = list(missing_book_token_ids)
        previous_confirmations = previous.get("event_end_confirmations")
        confirmations = (
            dict(event_end_confirmations)
            if isinstance(event_end_confirmations, Mapping)
            else dict(previous_confirmations)
            if isinstance(previous_confirmations, Mapping)
            else {}
        )
        if catalog_complete is None:
            catalog_complete = previous.get("catalog_complete") is True
        prior_success = previous.get("last_success_at")
        snapshot = {
            "state": state,
            "complete": complete,
            "scanning": False,
            "candidates": rows,
            "recommendations": recommendation_rows,
            "selected_results": selected_result_rows,
            "checked_at": last_successful_check,
            "last_success_at": (last_success_at or attempted)
            if successful
            else prior_success,
            "last_attempt_at": self._now(),
            "candidate_rows_fresh": has_new_rows,
            "scan_started_at": _iso(scan_started),
            "missing_metadata_condition_ids": missing_metadata,
            "missing_book_token_ids": missing_books,
            "catalog_complete": catalog_complete,
            "event_end_confirmations": confirmations,
            "retention_reason": None if recommendations is not None else retention_reason,
            "funnel": deepcopy(dict(funnel)) if funnel is not None else deepcopy(previous.get("funnel", {})),
            "selected_market_ids": list(selected_market_ids) if selected_market_ids is not None else list(previous.get("selected_market_ids", ())),
            "candidate_retention_reason": "background_candidates_retired",
        }
        writer = getattr(self.store, "lp_save_screening_snapshot", None)
        if callable(writer):
            try:
                saved = writer(snapshot)
            except Exception:
                saved = None
            if isinstance(saved, Mapping):
                try:
                    saved_started = _timestamp(
                        saved.get("scan_started_at"), name="scan_started_at"
                    )
                except ValueError:
                    saved_started = scan_started
                if saved_started > scan_started:
                    self._restore_candidate_snapshot(saved)
                    return self.candidate_snapshot()
                snapshot = deepcopy(dict(saved))
        with self._candidate_state_lock:
            self._candidate_snapshot = snapshot
        return self.candidate_snapshot()

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
            facts = self._validate_snapshot(normalized, snapshot, now=self._now())
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
            snapshot = self._read_snapshot(identity)
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
            facts = self._validate_snapshot(request, snapshot, now=now)
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
        }

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
                snapshot = self._read_snapshot(request)
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
                facts = self._validate_snapshot(request, snapshot, now=now)
                expiration = expiration_for_review(
                    _timestamp(request["review_at"], name="review_at"), now=now
                )
                reward_date = self._now().date().isoformat()
            except ValueError as exc:
                return {"state": "rejected", "reason": str(exc)}
            try:
                self.store.consume_lp_preview(preview_id)
            except ValueError as exc:
                return {"state": "rejected", "reason": str(exc)}
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

    def refresh_rewards(
        self,
        session_id: str | None = None,
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, object]:
        """Refresh the persisted platform-earnings observation for one session."""

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
        with self._mutex:
            session = (
                self.store.lp_session(session_id)
                if session_id
                else self.store.lp_active_session()
            )
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
                patch={"stop_requested": True, "review_status": "awaiting_reconciliation"},
            )
            return self._status_payload(updated)

    def tick(self) -> dict[str, object]:
        """Run one deterministic monitoring/reconciliation iteration."""

        with self._mutex:
            session = self.store.lp_active_session()
            if session is None:
                return {"state": "none", "session_id": None}
            state = str(session.get("state"))
            if state in {"entry_rejected", "complete"}:
                return self._status_payload(session)
            try:
                request = self._normalize_request(session)
                snapshot = self._read_snapshot(request)
            except ValueError as exc:
                updated = self.store.lp_update_session(
                    str(session["session_id"]),
                    state="needs_attention",
                    patch={
                        "reconciliation": str(exc),
                        "resume_state": state
                        if state != "needs_attention"
                        else session.get("resume_state"),
                    },
                )
                return self._status_payload(updated)
            try:
                patch = self._fill_patch(session, snapshot)
            except ValueError as exc:
                updated = self.store.lp_update_session(
                    str(session["session_id"]),
                    state="needs_attention",
                    patch={
                        "reconciliation": str(exc),
                        "resume_state": state
                        if state != "needs_attention"
                        else session.get("resume_state"),
                    },
                )
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
                    patch={"reconciliation": None, "resume_state": None},
                )
                state = resume_state
            session = self._reconcile_protected_exit(session, snapshot)
            if _decimal(session.get("buy_filled_quantity", 0), "buy_filled_quantity") > 0:
                entry_terminal = self._order_terminal(
                    snapshot, str(session.get("entry_order_id") or ""), session
                )
                if (
                    not bool(session.get("entry_cancel_requested"))
                    and not entry_terminal
                ):
                    self._request_entry_cancel(session)
                    session = self.store.lp_session(str(session["session_id"])) or session
                if not entry_terminal:
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
        for order in _items(account.get("open_orders")):
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
        if balance_d < 0 or allowance_d < 0 or balance_d < price * quantity or allowance_d < price * quantity:
            raise ValueError("balance_insufficient")
        book = snapshot.get("book")
        if not isinstance(book, Mapping):
            raise ValueError("book_unknown")
        stamp = book.get("received_at")
        if stamp is None:
            raise ValueError("book_freshness_unknown")
        _freshness(stamp, now, "book_freshness")
        asks = cls._levels(book.get("asks"), "asks")
        bids = cls._levels(book.get("bids"), "bids")
        if not asks or not bids:
            raise ValueError("book_invalid")
        candidate_policy = request.get("candidate_policy")
        if candidate_policy not in (None, "best_bid_minimum"):
            raise ValueError("candidate_policy_invalid")
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
            market_value = _field(order, "market_id", _field(order, "market", _field(order, "condition_id", None)))
            token = str(token_value or "")
            market = str(market_value or "")
            target = token == expected_token or market in {expected_market, str(session.get("condition_id") or "")}
            if not target and (not token or not market):
                # Without both identities the account read cannot establish
                # that this open order is outside the selected exposure.
                target = True
            if target and (not order_id or order_id not in owned_ids):
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
        quantity, cost = self._trade_totals(
            snapshot,
            entry_order_id,
            "BUY",
            token_id=str(token_id),
        )
        history = self._order_history(session)
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
        requested_quantity = _maybe_decimal(session.get("quantity"))
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
        result["reward_observation"] = self._reward_status_payload(session)
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
