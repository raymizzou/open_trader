"""Serialized, server-owned execution of one prediction-market pair."""

from __future__ import annotations

import fcntl
import inspect
import sqlite3
import threading
import time
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .daily_premarket import send_notification_with_results
from .notifications import Notifier, render_prediction_opportunity_notification
from .polymarket_trading import (
    LegResult,
    PairSubmission,
    PolymarketTradingClient,
    ThresholdHedgeSubmission,
    ThresholdLegResult,
)
from .prediction_arbitrage import (
    MAX_EMERGENCY_LOSS,
    MAX_NORMAL_COST,
    MAX_WALLET_BALANCE,
    MIN_ESTIMATED_PROFIT,
    PROTECTED_BUY_SHARE_PRECISION,
    PairIntent,
    ThresholdHedgeIntent,
    ThresholdHedgeLeg,
    protected_buy_quantity,
)
from .prediction_arbitrage_store import PredictionArbitrageStore


PREVIEW_TTL = timedelta(seconds=10)
BOOK_FRESHNESS_SECONDS = Decimal("10")
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
ExecutionIntent = PairIntent | ThresholdHedgeIntent


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
    ) -> None:
        self._store = store
        self._monitor = monitor
        self._trading = trading
        self._notifier = notifier
        self._lock_path = Path(lock_path)
        self._dashboard_url = str(dashboard_url)
        self._process_lock = _PROCESS_LOCK
        # A newly constructed process has not reconciled its dedicated wallet;
        # only a clean startup/reset path may clear this lock.
        self._breaker_open = True
        self._first_live_order_verified = False
        self._threads: dict[str, threading.Thread] = {}
        self._notification_lock = threading.Lock()
        self._clock = time.monotonic
        self._sleep = time.sleep

    def preview(self, opportunity_id: str) -> dict[str, object]:
        """Freshly validate one server-issued opportunity and persist a preview."""

        prepared = self._prepare_opportunity(str(opportunity_id))
        if isinstance(prepared, dict):
            return prepared
        opportunity, intent, account = prepared

        now = _utc_now()
        expires_at = now + PREVIEW_TTL
        payload = self._preview_payload(
            opportunity, intent, account=account, expires_at=expires_at
        )
        preview_id = self._store.create_preview(
            payload, expires_at=_timestamp(expires_at)
        )
        result = dict(payload)
        result.update(
            {
                "id": preview_id,
                "preview_id": preview_id,
                "state": "previewed",
                "expires_at": _timestamp(expires_at),
            }
        )
        return result

    def _prepare_opportunity(
        self, opportunity_id: str
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

        opportunity = self._fresh_opportunity(str(opportunity_id))
        intent = self._intent_from_opportunity(opportunity)
        if opportunity is None or intent is None:
            return {"state": "rejected", "reason": "opportunity_unavailable"}
        reason = self._validate_opportunity(opportunity, intent)
        if reason is not None:
            return {"state": "rejected", "reason": reason}
        account, reason = self._volatile_checks(intent)
        if account is None:
            return {"state": "rejected", "reason": reason or "readiness_unavailable"}
        return opportunity, intent, account

    def notify_ready_opportunity(
        self, opportunity_id: str, signal_id: str
    ) -> dict[str, object]:
        """Deliver one observation-only alert after a fresh no-submit proof."""

        signal = self._store.signal(str(signal_id))
        if signal is None:
            return {"state": "ignored", "reason": "signal_unavailable"}
        if signal.get("ended_at") is not None:
            return {"state": "ignored", "reason": "signal_closed"}
        if signal.get("notification_state") == "sent":
            return {"state": "ignored", "reason": "already_sent"}
        if signal.get("notification_state") == "sending":
            return {"state": "ignored", "reason": "notification_in_flight"}
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
            return {"state": "failed", "reason": "preflight_failed"}

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

        with self._notification_lock:
            current = self._store.signal(str(signal_id))
            if current is None or current.get("ended_at") is not None:
                return {"state": "ignored", "reason": "signal_closed"}
            if current.get("notification_state") == "sent":
                return {"state": "ignored", "reason": "already_sent"}
            if current.get("notification_state") == "sending":
                return {"state": "ignored", "reason": "notification_in_flight"}
            current_attempts = _decimal(current.get("notification_attempts")) or Decimal("0")
            if current_attempts >= 3:
                return {"state": "ignored", "reason": "notification_attempts_exhausted"}
            reserved_attempts = int(current_attempts) + 1
            try:
                current = self._store.update_signal(
                    str(signal_id),
                    {
                        "notification_attempts": reserved_attempts,
                        "notification_state": "sending",
                    },
                )
            except Exception:
                return {"state": "failed", "reason": "notification_state_unavailable"}

        final = dict(final)
        final.setdefault("order_ready_at", _timestamp(_utc_now()))
        title, message = render_prediction_opportunity_notification(
            final, current, dashboard_url=self._dashboard_url
        )
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
            error_code = "delivery_failed"
        else:
            error_code = "delivery_failed"
        feishu_success = any(
            getattr(item, "channel", "") in {"feishu", "feishu_app"}
            and getattr(item, "success", False)
            for item in attempts_result
        )
        if not feishu_success and fallback_target is not None:
            feishu_success = any(getattr(item, "success", False) for item in attempts_result)
        if feishu_success:
            self._store.update_signal(
                str(signal_id),
                {
                    "notification_state": "sent",
                    "notification_sent_at": _timestamp(_utc_now()),
                },
            )
            return {"state": "sent", "signal_id": str(signal_id)}
        self._store.update_signal(
            str(signal_id),
            {"notification_state": "failed", "notification_error": error_code},
        )
        return {"state": "failed", "reason": "notification_failed"}

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

    def reconcile_startup(self) -> dict[str, object]:
        """Reconcile live account state before allowing a new opportunity."""
        self._breaker_open = True
        snapshot = self._fresh_account_snapshot()
        if snapshot is None:
            return {"state": "locked", "reason": "account_unavailable"}
        if self._store.unacknowledged_incident() is not None:
            return {"state": "locked", "reason": "unacknowledged_incident"}

        active = self._store.active_execution()
        active_id = str(active.get("execution_id", "")) if active else ""
        open_orders = self._order_ids(snapshot.get("open_order_ids", ()))
        if open_orders:
            cancel = getattr(self._trading, "cancel_orders", None)
            try:
                canceled = _call(cancel, tuple(open_orders))
            except Exception:
                canceled = ()
            # A cancel response is not proof that the venue has settled the
            # account.  Re-read the complete account snapshot with freshness
            # validation before deciding whether the startup incident is
            # contained.
            after_cancel = self._fresh_account_snapshot()
            remaining = self._order_ids(
                after_cancel.get("open_order_ids", ()) if after_cancel else open_orders
            )
            evidence = {
                "phase": "startup_open_orders",
                "open_orders": open_orders,
                "canceled": self._safe_sequence(canceled),
                "remaining": remaining,
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
            self._store.write_runtime(
                {
                    "prediction_arbitrage": "ready",
                    "reconciled_at": _timestamp(_utc_now()),
                    "readiness": "reconciled",
                }
            )
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
        self._store.write_runtime(
            {"prediction_arbitrage": "ready", "reconciled_at": _timestamp(_utc_now())}
        )
        return {"state": "ready", "readiness": "fresh"}

    def reset_breaker(self, incident_id: str) -> dict[str, object]:
        incident = next(
            (
                row
                for row in self._store.histories("incidents")
                if str(row.get("incident_id", "")) == str(incident_id)
                and row.get("acknowledged") is not True
            ),
            None,
        )
        if incident is None:
            self._breaker_open = True
            return {"state": "locked", "reason": "incident_not_found", "incident_id": str(incident_id)}
        snapshot = self._account_snapshot()
        reasons: list[str] = []
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
            totals = self._position_totals(snapshot, intent)
            if totals["unknown"]:
                reasons.append("unknown_external_state")
            if totals["yes"] != 0 or totals["no"] != 0:
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
            active_id = str(incident.get("execution_id", ""))
            update_incident = getattr(self._store, "update_incident", None)
            if callable(update_incident):
                try:
                    update_incident(
                        str(incident_id),
                        {
                            "last_reset_denial": {
                                "reason": reason,
                                "blocking_reasons": reasons,
                                "at": _timestamp(_utc_now()),
                            }
                        },
                    )
                except Exception:
                    pass
            if active_id:
                self._transition(
                    active_id,
                    "reset_denied",
                    {
                        "phase": "reset_denied",
                        "incident_id": str(incident_id),
                        "reason": reason,
                    },
                )
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

    def _run_execution(self, execution_id: str, lock: tuple[threading.Lock, Any]) -> None:
        try:
            row = self.execution(execution_id)
            intent = self._intent_from_payload(row.get("intent"))
            if intent is None:
                self._finish_incident(execution_id, "invalid_persisted_intent")
                return
            self._transition(execution_id, "final_validating", {"phase": "final_validating"})
            opportunity = self._fresh_opportunity(str(row.get("opportunity_id", "")))
            current_intent = self._intent_from_opportunity(opportunity)
            if opportunity is None or current_intent is None:
                self._finish_rejected(execution_id, "opportunity_unavailable")
                return
            reason = self._validate_opportunity(opportunity, current_intent)
            account, volatile_reason = self._volatile_checks(current_intent)
            if reason is not None or account is None:
                self._finish_rejected(
                    execution_id, reason or volatile_reason or "readiness_unavailable"
                )
                return
            # The intent is rebuilt from current server data; browser and stale
            # preview economics never reach the authenticated client.
            intent = current_intent
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
                result = _call(preflight, intent, tick_size=tick_size)
                if not self._preflight_passed(result):
                    self._finish_rejected(execution_id, "preflight_failed")
                    return
            submit = getattr(self._trading, "submit_pair_once", None)
            try:
                submission = _call(submit, intent, tick_size=tick_size)
            except Exception:
                submission = self._ambiguous_submission()
            yes, no = self._submission_legs(submission)
            self._store.record_leg(execution_id, self._leg_payload(yes))
            self._store.record_leg(execution_id, self._leg_payload(no))
            self._transition(
                execution_id,
                "reconciling",
                {"phase": "reconciling", "post_attempted": True},
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
                self._finish_incident(execution_id, "reconciliation_timeout")
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
                self._store.write_runtime(
                    {
                        "prediction_arbitrage": "ready",
                        "first_live_order": "validated",
                        "validated_at": _timestamp(_utc_now()),
                    }
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
        try:
            result = _call(preflight, intent)
        except Exception:
            result = None
        if not self._preflight_passed(result):
            self._finish_rejected(execution_id, "preflight_failed")
            return
        submit = getattr(self._trading, "submit_threshold_hedge_once", None)
        if not callable(submit):
            self._finish_rejected(execution_id, "threshold_submission_unavailable")
            return
        try:
            submission = _call(submit, intent)
        except Exception:
            submission = self._ambiguous_threshold_submission(intent)
        leg_a, leg_b = self._threshold_submission_legs(submission, intent)
        self._store.record_leg(execution_id, self._threshold_leg_payload(leg_a))
        self._store.record_leg(execution_id, self._threshold_leg_payload(leg_b))
        self._transition(
            execution_id,
            "reconciling",
            {"phase": "reconciling", "post_attempted": True},
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
            self._finish_incident(execution_id, "reconciliation_timeout")
            return
        quantity_a, quantity_b, proof = known
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

    def _fresh_opportunity(self, opportunity_id: str) -> dict[str, object] | None:
        refreshed: object = None
        for name in ("refresh_once", "refresh_opportunity", "recheck_opportunity", "refresh"):
            refresh = getattr(self._monitor, name, None)
            if not callable(refresh):
                continue
            try:
                refreshed = _call(refresh, opportunity_id) if name != "refresh_once" else _call(refresh)
            except Exception:
                refreshed = None
            break
        if refreshed is None:
            snapshot = getattr(self._monitor, "snapshot", None)
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
        opportunity = getattr(self._monitor, "opportunity", None)
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
        self, intent: ExecutionIntent
    ) -> tuple[dict[str, object] | None, str | None]:
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

    def _startup_incident(
        self,
        execution_id: str,
        reason: str,
        evidence: Mapping[str, object],
        *,
        state: str = "directional_incident",
    ) -> str | None:
        if not execution_id:
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
        self, opportunity: Mapping[str, object], intent: ExecutionIntent
    ) -> str | None:
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
    def _intent_payload(intent: ExecutionIntent) -> dict[str, object]:
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
        if isinstance(value, ThresholdHedgeIntent):
            return value
        if isinstance(value, PairIntent):
            return value
        if not isinstance(value, Mapping):
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
            return bool(value is True)
        return value.get("result") in ("PASS", "pass", True)

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

    def _finish_rejected(self, execution_id: str, reason: str) -> None:
        self._transition(execution_id, "both_rejected", {"phase": "validation_rejected", "reason": reason})

    def _finish_incident(
        self,
        execution_id: str,
        reason: str,
        *,
        state: str = "directional_incident",
        incident_id: str | None = None,
        notify: bool = True,
    ) -> None:
        self._breaker_open = True
        if incident_id is None:
            incident_id = self._record_incident(
                execution_id,
                reason,
                state=state,
                evidence={"phase": "incident", "reason": reason},
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
        if isinstance(explicit, str) and explicit in {"macos", "feishu", "feishu_app"}:
            return explicit
        name = target.__class__.__name__.lower()
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
