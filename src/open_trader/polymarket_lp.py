"""Durable, single-market Polymarket liquidity-provider session."""

from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from collections.abc import Callable, Mapping, Sequence
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
        self._report_lock = threading.Lock()
        self._candidate_refresh_lock = threading.Lock()
        self._candidate_state_lock = threading.RLock()
        self._sample_target_lock = threading.Lock()
        self._sample_targets: tuple[tuple[str, str], ...] = ()
        self._sample_target_version = 0
        self._candidate_attempted_at: datetime | None = None
        self._candidate_snapshot: dict[str, object] = {
            "state": "unknown",
            "complete": False,
            "scanning": False,
            "candidates": [],
            "recommendations": [],
            "checked_at": None,
            "last_success_at": None,
            "last_attempt_at": None,
            "candidate_rows_fresh": False,
            "missing_metadata_condition_ids": [],
        }
        self._restore_candidate_snapshot()

    def set_mutation_guard(self, guard: Callable[..., bool] | None) -> None:
        """Attach the existing execution breaker to exchange writes."""

        self._mutation_guard = guard

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
            snapshot["stale"] = age < 0 or age > LP_CANDIDATE_REFRESH_SECONDS
        else:
            snapshot["stale"] = True
        if snapshot.get("state") in {"stale", "unknown"} or (
            snapshot.get("state") == "incomplete"
            and snapshot.get("candidate_rows_fresh") is not True
        ):
            snapshot["stale"] = True
        recommendations = snapshot.get("recommendations")
        if isinstance(recommendations, (list, tuple)):
            for row in recommendations:
                if not isinstance(row, dict):
                    continue
                directions = row.get("directions")
                if not isinstance(directions, Mapping):
                    continue
                for direction in directions.values():
                    if not isinstance(direction, dict):
                        continue
                    if direction.get("eligible") is not True:
                        continue
                    guidance = direction.get("guidance")
                    if not isinstance(guidance, Mapping):
                        continue
                    try:
                        expires_at = _timestamp(
                            guidance.get("expires_at"), name="guidance_expiry"
                        )
                    except ValueError:
                        expires_at = None
                    if expires_at is None or now >= expires_at:
                        direction["state"] = "expired"
                        direction["eligible"] = False
                        direction["reason_codes"] = ["guidance_expired"]
                if directions:
                    row["state"] = (
                        "eligible"
                        if any(
                            isinstance(direction, Mapping)
                            and direction.get("eligible") is True
                            for direction in directions.values()
                        )
                        else "expired"
                    )
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
        with self._candidate_state_lock:
            for key in (
                "state",
                "complete",
                "candidates",
                "recommendations",
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
        """Refresh managed candidates and read-only manual entry guidance."""

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
            if not callable(catalog_reader):
                raise ValueError("reward_catalog_unavailable")
            catalog = catalog_reader(stop_event=stop_event)
            if (
                not isinstance(catalog, Mapping)
                or catalog.get("state") != "known"
                or catalog.get("complete") is not True
            ):
                checked_at = (
                    catalog.get("checked_at")
                    if isinstance(catalog, Mapping)
                    else None
                )
                return self._finish_candidate_scan(
                    previous,
                    state="incomplete",
                    complete=False,
                    checked_at=checked_at or self._now(),
                    scan_started_at=scan_started_at,
                    retention_reason="reward_catalog_unknown",
                    catalog_complete=False,
                )
            raw_markets = catalog.get("markets")
            if not isinstance(raw_markets, (list, tuple)):
                raise ValueError("reward_catalog_unknown")
            market_rows = [
                dict(row) for row in raw_markets if isinstance(row, Mapping)
            ]
            if not market_rows:
                self._publish_sample_targets(())
                completed_at = self._now()
                recommendations = self._merge_recommendations(
                    previous.get("recommendations"),
                    (),
                    observations={},
                    retention_reason="reward_inactive",
                )
                return self._finish_candidate_scan(
                    previous,
                    state="ready",
                    complete=True,
                    checked_at=completed_at,
                    scan_started_at=scan_started_at,
                    last_success_at=completed_at,
                    candidates=[],
                    recommendations=recommendations,
                    missing_metadata_condition_ids=(),
                    catalog_complete=True,
                )

            condition_ids = tuple(
                dict.fromkeys(
                    str(row.get("condition_id") or "").strip()
                    for row in market_rows
                    if str(row.get("condition_id") or "").strip()
                )
            )
            metadata_reader = getattr(self.exchange, "lp_market_metadata", None)
            account_reader = getattr(self.exchange, "lp_account_snapshot", None)
            books_reader = getattr(self.exchange, "lp_order_books", None)
            if not callable(metadata_reader) or not callable(account_reader) or not callable(books_reader):
                raise ValueError("candidate_readers_unavailable")
            metadata_value = metadata_reader(condition_ids)
            metadata_observed_at = self._now()
            if not isinstance(metadata_value, Mapping):
                raise ValueError("candidate_market_facts_unknown")
            metadata_by_condition = {
                str(key): value
                for key, value in metadata_value.items()
                if isinstance(key, str) and isinstance(value, Mapping)
            }
            missing_metadata_condition_ids = tuple(
                condition_id
                for condition_id in condition_ids
                if condition_id not in metadata_by_condition
            )
            self._publish_sample_targets(
                tuple(
                    (condition_id, str(outcome.get("token_id") or "").strip())
                    for condition_id in condition_ids
                    for outcome in (
                        metadata_by_condition.get(condition_id, {})
                        .get("outcomes", {})
                        .values()
                        if isinstance(
                            metadata_by_condition.get(condition_id, {}).get(
                                "outcomes"
                            ),
                            Mapping,
                        )
                        else ()
                    )
                    if isinstance(outcome, Mapping)
                    and str(outcome.get("token_id") or "").strip()
                )
            )
            account = account_reader()
            if not isinstance(account, Mapping) or account.get("authenticated") is not True:
                raise ValueError("candidate_market_facts_unknown")
            token_ids = tuple(
                dict.fromkeys(
                    str(outcome.get("token_id") or "").strip()
                    for market in metadata_by_condition.values()
                    for outcome in (
                        market.get("outcomes", {}).values()
                        if isinstance(market.get("outcomes"), Mapping)
                        else ()
                    )
                    if isinstance(outcome, Mapping)
                    and str(outcome.get("token_id") or "").strip()
                )
            )
            books_value = books_reader(token_ids, stop_event=stop_event)
            if not isinstance(books_value, Mapping):
                raise ValueError("candidate_books_unknown")
            books_by_token = {
                str(key): value
                for key, value in books_value.items()
                if isinstance(key, str) and isinstance(value, Mapping)
            }
            direction_facts: list[dict[str, object]] = []
            complete = not missing_metadata_condition_ids
            missing_book_token_ids: list[str] = []
            catalog_checked_at = catalog.get("checked_at")
            saved_screening = getattr(self.store, "lp_screening_snapshot", lambda: None)()
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
            for reward_market in market_rows:
                condition_id = str(reward_market.get("condition_id") or "")
                reward_guidance_deadline = self._reward_guidance_deadline(
                    reward_market
                )
                market_meta = metadata_by_condition.get(condition_id)
                if market_meta is None:
                    complete = False
                    continue
                raw_outcomes = market_meta.get("outcomes")
                if not isinstance(raw_outcomes, Mapping):
                    complete = False
                    continue
                reward_minimum = market_meta.get(
                    "reward_min_size", reward_market.get("rewards_min_size")
                )
                reward_spread = market_meta.get(
                    "reward_max_spread", reward_market.get("rewards_max_spread")
                )
                if reward_spread is not None and market_meta.get("reward_max_spread") is None:
                    raw_spread = _maybe_decimal(reward_spread)
                    reward_spread = None if raw_spread is None else raw_spread / Decimal("100")
                if reward_minimum is None or reward_spread is None:
                    complete = False
                confirmation = self._observe_event_end(
                    condition_id,
                    market_meta,
                    existing=event_end_confirmations.get(condition_id),
                    observed_at=metadata_observed_at,
                )
                if confirmation is not None:
                    event_end_confirmations[condition_id] = confirmation
                for outcome_key in ("yes", "no"):
                    outcome = raw_outcomes.get(outcome_key)
                    if not isinstance(outcome, Mapping):
                        complete = False
                        continue
                    token_id = str(outcome.get("token_id") or "")
                    book = books_by_token.get(token_id)
                    if not token_id or not isinstance(book, Mapping):
                        complete = False
                        if token_id:
                            missing_book_token_ids.append(token_id)
                        continue
                    market = {
                        **dict(market_meta),
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "outcome": str(outcome.get("label") or outcome_key).upper(),
                        "reward_min_size": reward_minimum,
                        "reward_max_spread": reward_spread,
                    }
                    direction_facts.append(
                        {
                            "market": market,
                            "book": dict(book),
                            "reward_active": True,
                            "daily_pool_usd": reward_market.get("daily_pool_usd"),
                            "reward_checked_at": catalog_checked_at,
                            "reward_guidance_deadline": reward_guidance_deadline,
                            "event_end_confirmation": confirmation,
                        }
                    )

            from .polymarket_lp_views import (
                lp_candidate_rows,
                lp_recommendation_rows,
                screen_lp_direction,
            )
            from .polymarket_lp_risk import evaluate_lp_entry

            reservations = self._candidate_reservations()
            checked_at = self._now()
            histories: dict[tuple[str, str], object] = {}
            history_reader = getattr(self.store, "lp_book_samples", None)
            for direction in direction_facts:
                market = direction.get("market")
                if not isinstance(market, Mapping) or not callable(history_reader):
                    continue
                condition_id = str(market.get("condition_id") or "")
                token_id = str(market.get("token_id") or "")
                try:
                    histories[(condition_id, token_id)] = history_reader(
                        condition_id,
                        token_id,
                        since=checked_at - timedelta(hours=1),
                        until=checked_at,
                    )
                except Exception:
                    histories[(condition_id, token_id)] = None

            observations: dict[tuple[str, str], dict[str, object]] = {}
            for direction in direction_facts:
                market = direction.get("market")
                if not isinstance(market, Mapping):
                    continue
                identity = (
                    str(market.get("condition_id") or ""),
                    str(market.get("outcome") or "").upper(),
                )
                screening = screen_lp_direction(
                    direction,
                    history=histories.get(
                        (
                            identity[0],
                            str(market.get("token_id") or ""),
                        )
                    ),
                    now=checked_at,
                )
                if screening.get("state") == "eligible":
                    entry = evaluate_lp_entry(
                        {**direction, "screening": screening},
                        account=account,
                        now=checked_at,
                        reservations=reservations,
                    )
                    observations[identity] = {
                        "state": entry.get("state"),
                        "reason_codes": list(entry.get("reason_codes", ())),
                    }
                else:
                    observations[identity] = {
                        "state": screening.get("state"),
                        "reason_codes": list(screening.get("reason_codes", ())),
                    }

            candidates = lp_candidate_rows(
                direction_facts,
                account=account,
                now=checked_at,
                reservations=reservations,
            )
            fresh_recommendations = lp_recommendation_rows(
                direction_facts,
                histories=histories,
                account=account,
                now=checked_at,
                reservations=reservations,
            )
            recommendations = self._merge_recommendations(
                previous.get("recommendations"),
                fresh_recommendations,
                observations=observations,
                retention_reason=(
                    "market_metadata_missing"
                    if missing_metadata_condition_ids
                    else "market_facts_unknown"
                ),
            )
            completed_at = checked_at
            return self._finish_candidate_scan(
                previous,
                state="ready" if complete else "incomplete",
                complete=complete,
                checked_at=completed_at,
                scan_started_at=scan_started_at,
                last_success_at=completed_at if complete else None,
                candidates=candidates,
                recommendations=recommendations,
                missing_metadata_condition_ids=missing_metadata_condition_ids,
                missing_book_token_ids=tuple(dict.fromkeys(missing_book_token_ids)),
                catalog_complete=True,
                event_end_confirmations=event_end_confirmations,
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
        for field in ("native_reward_configs", "sponsored_reward_configs"):
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
        missing_metadata_condition_ids: Sequence[str] | None = None,
        missing_book_token_ids: Sequence[str] | None = None,
        catalog_complete: bool | None = None,
        event_end_confirmations: Mapping[str, object] | None = None,
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
