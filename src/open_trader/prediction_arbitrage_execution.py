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

from .notifications import Notifier
from .polymarket_trading import LegResult, PairSubmission
from .prediction_arbitrage import (
    MAX_WALLET_BALANCE,
    PROTECTED_BUY_SHARE_PRECISION,
    PairIntent,
)
from .prediction_arbitrage_store import PredictionArbitrageStore


PREVIEW_TTL = timedelta(seconds=10)
BOOK_FRESHNESS_SECONDS = Decimal("10")
MAX_RECONCILIATION_SECONDS = 30
TERMINAL_STATES = {
    "both_rejected",
    "complete",
    "neutralized_incident",
    "directional_incident",
    "merge_incident",
}
_PROCESS_LOCK = threading.Lock()


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
    ) -> None:
        self._store = store
        self._monitor = monitor
        self._trading = trading
        self._notifier = notifier
        self._lock_path = Path(lock_path)
        self._process_lock = _PROCESS_LOCK
        self._breaker_open = False
        self._threads: dict[str, threading.Thread] = {}
        self._clock = time.monotonic
        self._sleep = time.sleep

    def preview(self, opportunity_id: str) -> dict[str, object]:
        """Freshly validate one server-issued opportunity and persist a preview."""

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
        # Task 6 owns startup reconciliation; remaining locked is deliberate.
        self._breaker_open = True
        return {"state": "locked", "reason": "startup_reconciliation_deferred"}

    def reset_breaker(self, incident_id: str) -> dict[str, object]:
        # Task 6 owns fresh live reconciliation and acknowledgement semantics.
        return {
            "state": "locked",
            "reason": "breaker_reset_deferred",
            "incident_id": str(incident_id),
        }

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
            if yes_quantity <= 0 or no_quantity <= 0 or yes_quantity != no_quantity:
                self._finish_incident(execution_id, "directional_imbalance")
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
            after = self._account_snapshot()
            before_balance = _decimal(account.get("p_usd_balance"))
            after_balance = _decimal(after.get("p_usd_balance")) if after else None
            if (
                before_balance is None
                or after_balance is None
                or after_balance <= before_balance
            ):
                self._finish_incident(execution_id, "collateral_reconciliation_failed", state="merge_incident")
                return
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
        self, intent: PairIntent
    ) -> tuple[dict[str, object] | None, str | None]:
        geoblock = getattr(self._trading, "geoblock_allowed", None)
        if not callable(geoblock):
            return None, "geoblock_unavailable"
        try:
            if _call(geoblock) is not True:
                return None, "geoblock_blocked"
        except Exception:
            return None, "geoblock_unavailable"
        account = self._account_snapshot()
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
        if not self._relayer_ready():
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

    def _relayer_ready(self) -> bool:
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
                return relayer in accepted and merge in accepted
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
                return relayer in accepted and merge in accepted
            return False
        return False

    def _validate_opportunity(
        self, opportunity: Mapping[str, object], intent: PairIntent
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
        intent: PairIntent,
        *,
        account: Mapping[str, object],
        expires_at: datetime,
    ) -> dict[str, object]:
        return {
            "opportunity_id": str(
                opportunity.get("opportunity_id", opportunity.get("id", ""))
            ),
            "event_id": intent.event_id,
            "market_id": intent.market_id,
            "condition_id": intent.condition_id,
            "question": str(opportunity.get("question", "")),
            "volume_24h": _safe_decimal(opportunity.get("volume_24h")) or "0",
            "intent": self._intent_payload(intent),
            "tick_size": _safe_decimal(self._tick_size(opportunity)) or "",
            "quantity": format(intent.quantity, "f"),
            "yes_max_price": format(intent.yes_max_price, "f"),
            "no_max_price": format(intent.no_max_price, "f"),
            "yes_max_cost": format(intent.yes_max_cost, "f"),
            "no_max_cost": format(intent.no_max_cost, "f"),
            "total_max_cost": format(intent.total_max_cost, "f"),
            "minimum_profit": format(intent.minimum_profit, "f"),
            "net_edge": format(intent.net_edge, "f"),
            "wallet_address": str(account.get("wallet_address", "")),
            "expires_at": _timestamp(expires_at),
        }

    @staticmethod
    def _intent_payload(intent: PairIntent) -> dict[str, object]:
        result: dict[str, object] = {}
        for field in fields(PairIntent):
            value = getattr(intent, field.name)
            result[field.name] = format(value, "f") if isinstance(value, Decimal) else value
        return result

    def _intent_from_opportunity(self, opportunity: Mapping[str, object] | None) -> PairIntent | None:
        if opportunity is None:
            return None
        value = opportunity.get("intent")
        intent = self._intent_from_payload(value)
        if intent is not None:
            return intent
        return self._intent_from_payload(opportunity)

    @staticmethod
    def _intent_from_payload(value: object) -> PairIntent | None:
        if isinstance(value, PairIntent):
            return value
        if not isinstance(value, Mapping):
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
            if quantities is not None and quantities[0] > 0 and quantities[1] > 0:
                return quantities
            if self._proof_claims_success(value):
                proof = value.get("execution_proof")
                return Decimal("0"), Decimal("0"), dict(proof)
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
            "ok", "confirmed", "filled", "complete"
        }:
            return None
        proof = value.get("execution_proof")
        if not isinstance(proof, Mapping) or proof.get("verified") is not True:
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
        self, execution_id: str, reason: str, *, state: str = "directional_incident"
    ) -> None:
        self._breaker_open = True
        payload = {"reason": reason, "breaker": "open", "state": state}
        try:
            incident_id = self._store.open_incident(execution_id, payload)
            payload["incident_id"] = incident_id
            self._transition(execution_id, state, payload)
            try:
                self._notifier.notify("预测市场执行事故", reason)
            except Exception:
                pass
        except Exception:
            # A persistence failure must not unlock the process; leave the
            # breaker open even when an incident row cannot be written.
            self._breaker_open = True

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
