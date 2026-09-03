"""Issue #64 Slice 5: the FIFO queue-head driver and the real submit path.

One in-process background thread (1 s tick, single consumer) drives the
manual-confirm queue: gates closed and no active batch -> take the queue
head -> pull fresh books for every leg -> preflight -> atomic admission
(version CAS + unsettled cap) -> submit each leg ONCE (FOK BUY, legs in
parallel, per-leg timeout) -> fold receipts through the durable reducer.

Fail-closed rules (approved rulings 7/8): a preflight failure or a stale
admission abandons the head row back to monitoring with zero side effects;
any timeout/exception/unrecognized receipt books an UNKNOWN receipt, opens
an incident, clears every PENDING row and never retries; a non-BUY leg
fails closed as UNSUPPORTED_ACTION before anything is sent.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Callable, Mapping

from open_trader.prediction_executable_cost import (
    AccountBalance,
    AccountSnapshot,
)
from open_trader.prediction_n_leg_execution import (
    ConfirmedHolding,
    NLegExecutionService,
    OrderReceipt,
    ReconciliationContext,
    SettlementCashFlow,
    partial_fill_proof_from_payload,
)
from open_trader.prediction_n_leg_mode import n_leg_mode_contract
from open_trader.prediction_n_leg_preflight import preflight

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 1.0
DEFAULT_SUBMIT_TIMEOUT_SECONDS = 15
#: Review round 2 (P1): an all-terminal batch awaiting venue reconciliation
#: must never be skipped silently forever; beyond this age it becomes a
#: visible blocked state (log + queue row flag + tick reason).
DEFAULT_RECONCILIATION_TIMEOUT_SECONDS = 60
#: The N-leg unit scale shared with the #117 economics pipeline.
NLEG_UNITS_PER_DOLLAR = 1_000_000

#: Abandon reasons surfaced to the operator (stable literals).
VERSION_STALE = "VERSION_STALE"
UNSETTLED_CAP = "UNSETTLED_CAP"
UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
BOOK_UNAVAILABLE = "BOOK_UNAVAILABLE"
EXECUTION_INCIDENT_ACTIVE = "EXECUTION_INCIDENT_ACTIVE"
#: Visible blocked reason (P1): reconciliation did not complete in time.
RECONCILIATION_TIMEOUT = "RECONCILIATION_TIMEOUT"
#: Visible blocked reason (F2): an ACTIVE (non-AWAITING) batch outlived the
#: reconciliation window — same visibility, never a re-drive.
ACTIVE_BATCH_OVERDUE = "ACTIVE_BATCH_OVERDUE"
#: Abandon reason (P3): the frozen row carries no partial-fill proof payload.
PARTIAL_FILL_PROOF_REQUIRED = "PARTIAL_FILL_PROOF_REQUIRED"


class NLegOrderQueueDriver:
    """Single-consumer FIFO driver for manual-confirm N-leg orders."""

    def __init__(
        self,
        store: object,
        *,
        books_provider: Callable[[str], object | None],
        source_factory: Callable[[Mapping[str, object]], object],
        trading: object,
        reconciliation_context_factory: Callable[[str], object] | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        submit_timeout_seconds: int = DEFAULT_SUBMIT_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._books_provider = books_provider
        self._source_factory = source_factory
        self._trading = trading
        self._reconciliation_context_factory = reconciliation_context_factory
        self._poll_seconds = poll_seconds
        self._submit_timeout_seconds = submit_timeout_seconds
        self._service = NLegExecutionService(store)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="n-leg-order-queue-driver", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.wait(self._poll_seconds):
            try:
                self.tick()
            except Exception:
                logger.exception("n_leg_queue_driver tick failed")

    # -- one consumer cycle --------------------------------------------------

    def tick(self, *, now: datetime | None = None) -> dict[str, object]:
        moment = now or datetime.now(UTC)
        store = self._store
        control = store.n_leg_control()
        if control["breaker_open"]:
            return {"skipped": "GLOBAL_BREAKER_OPEN"}
        if store.unacknowledged_incident() is not None:
            return {"skipped": "EXECUTION_INCIDENT_ACTIVE"}
        if control["active_batch_id"] is not None:
            return self._reconciliation_watch(
                str(control["active_batch_id"]), moment
            )
        head = store.n_leg_request_head()
        if head is None:
            return {"skipped": "QUEUE_EMPTY"}

        frozen = head["payload"]
        if not self._all_legs_buy(frozen):
            return self._abandon(head, UNSUPPORTED_ACTION)
        # Review round 2 (P3): a row frozen without a proof payload can never
        # be admitted; abandon it with the stable literal instead of letting
        # a KeyError escape the abandon path and retry the head forever.
        proof_payload = frozen.get("partial_fill_proof")
        if not isinstance(proof_payload, Mapping):
            return self._abandon(head, PARTIAL_FILL_PROOF_REQUIRED)

        # Preflight against fresh books (ruling 7).
        books = self._books_provider(str(head["component_id"]))
        if books is None:
            return self._abandon(head, BOOK_UNAVAILABLE)
        safety = store.n_leg_safety_config_latest()
        safety_config = (
            dict(safety.get("config") or {})
            if isinstance(safety, Mapping)
            else {}
        )
        result = preflight(
            frozen,
            books,
            safety_config,
            self._policy(store, frozen),
            now=moment,
        )
        if not result["ok"]:
            return self._abandon(head, str(result["reason"]))

        # Atomic admission (Slice 4 CAS + unsettled cap).
        try:
            source = self._source_factory(frozen)
            batch = self._service.enter(
                opportunity_episode_id=str(frozen["opportunity_episode_id"]),
                episode_lineage_id=str(frozen["episode_lineage_id"]),
                execution_batch_id=str(frozen["execution_batch_id"]),
                source=source,
                partial_fill_proof=partial_fill_proof_from_payload(
                    dict(proof_payload)
                ),
                mode="MANUAL",
                cap_config_version=self._cap_config_version(frozen),
                expected_versions=self._expected_versions(frozen),
            )
        except ValueError as exc:
            return self._abandon(head, _admission_reason(str(exc)))
        store.n_leg_request_update(
            str(head["request_id"]),
            state="ADMITTED",
            payload_merge={
                "execution_batch_id": batch["execution_batch_id"],
                "admitted_at": moment.isoformat(),
            },
        )
        return self._submit_legs(str(batch["execution_batch_id"]), moment)

    # -- submit phase ---------------------------------------------------------

    def _submit_legs(self, batch_id: str, moment: datetime) -> dict[str, object]:
        store = self._store
        batch = store.n_leg_batch(batch_id)
        if batch is None:
            return {"skipped": "N_LEG_BATCH_NOT_FOUND"}
        legs = [dict(leg) for leg in batch.get("legs", []) if isinstance(leg, dict)]
        outcomes: dict[str, dict[str, object]] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(legs))) as executor:
            futures = {
                executor.submit(self._submit_one, batch_id, leg): leg for leg in legs
            }
            for future, leg in futures.items():
                client_order_id = str(leg.get("client_order_id"))
                # Review round 2 (P2): nothing from one leg may escape the
                # fold — a raising leg books an UNKNOWN outcome and the
                # incident stop-the-world path below necessarily runs.
                try:
                    outcomes[client_order_id] = future.result()
                except Exception:
                    logger.exception(
                        "n_leg_queue_driver leg submit raised batch=%s client=%s",
                        batch_id,
                        client_order_id,
                    )
                    outcomes[client_order_id] = {
                        "state": "UNKNOWN",
                        "error_code": "submit_error",
                    }
        for client_order_id in sorted(outcomes):
            try:
                receipt = self._receipt_for(
                    batch_id,
                    client_order_id,
                    _leg_by_id(batch, client_order_id),
                    outcomes[client_order_id],
                    moment,
                )
            except Exception:
                logger.exception(
                    "n_leg_queue_driver receipt build failed batch=%s client=%s",
                    batch_id,
                    client_order_id,
                )
                receipt = self._unknown_receipt(
                    batch_id, client_order_id, moment
                )
            try:
                self._service.apply_receipt(receipt)
            except Exception:
                logger.exception(
                    "n_leg_queue_driver receipt application failed batch=%s client=%s",
                    batch_id,
                    client_order_id,
                )
        batch = store.n_leg_batch(batch_id)
        if batch is None:
            return {"submitted": batch_id}
        if isinstance(batch.get("incident"), dict):
            # Stop the world: clear every PENDING row and keep the incident
            # state; the incident batch's own row never re-drives.
            store.n_leg_requests_abandon_pending(EXECUTION_INCIDENT_ACTIVE)
            return {"submitted": batch_id, "incident": batch["incident"]}
        if str(batch.get("state")) == "AWAITING_RECONCILIATION":
            if self._try_complete_reconciliation(batch_id, moment):
                self._mark_batch_rows_submitted(batch_id)
                return {"submitted": batch_id}
            self._stamp_awaiting_reconciliation(batch_id, moment)
            return {"submitted": batch_id}
        if str(batch.get("state", "")).startswith("RECONCILED"):
            self._mark_batch_rows_submitted(batch_id)
        return {"submitted": batch_id}

    def _submit_one(
        self, batch_id: str, leg: dict[str, object]
    ) -> dict[str, object]:
        client_order_id = str(leg.get("client_order_id"))
        try:
            outcome = self._trading.submit_n_leg_leg_once(
                client_order_id=client_order_id,
                token_id=str(leg.get("asset_id")),
                quantity_lots=int(leg.get("submitted_quantity") or 0),
                max_cost_units=int(leg.get("max_cost_units") or 0),
                timeout_seconds=self._configured_submit_timeout(),
            )
        except Exception:
            # Review round 2 (P2): an adapter raise (trading=None,
            # sqlite race, ...) is still ONE UNKNOWN attempt, never an escape.
            logger.exception(
                "n_leg_queue_driver submit attempt raised batch=%s client=%s",
                batch_id,
                client_order_id,
            )
            outcome = {"state": "UNKNOWN", "error_code": "submit_error"}
        try:
            self._store.n_leg_transition_append(
                batch_id,
                kind="SUBMISSION_ATTEMPT",
                idempotency_key=f"submission:{client_order_id}",
                payload={
                    "client_order_id": client_order_id,
                    "outcome": outcome.get("state"),
                    "error_code": outcome.get("error_code"),
                },
            )
        except Exception:
            logger.exception(
                "n_leg_queue_driver transition append failed batch=%s client=%s",
                batch_id,
                client_order_id,
            )
        return outcome

    def _configured_submit_timeout(self) -> int:
        """Ruling 8: the per-leg submit timeout is versioned config
        (``max_leg_submit_seconds``); the constructor value is the fallback
        when no valid config value is stored."""
        try:
            safety = self._store.n_leg_safety_config_latest()
            config = (
                safety.get("config") if isinstance(safety, Mapping) else None
            )
            value = int((config or {}).get("max_leg_submit_seconds", 0))
        except (TypeError, ValueError, AttributeError):
            return self._submit_timeout_seconds
        return value if value > 0 else self._submit_timeout_seconds

    def _unknown_receipt(
        self,
        batch_id: str,
        client_order_id: str,
        leg: Mapping[str, object] | None,
        moment: datetime,
    ) -> OrderReceipt:
        """Last-resort UNKNOWN receipt built only from trusted literals."""
        try:
            submitted = max(0, int((leg or {}).get("submitted_quantity") or 0))
        except (TypeError, ValueError):
            submitted = 0
        return OrderReceipt(
            receipt_id=f"{client_order_id}:1",
            execution_batch_id=batch_id,
            client_order_id=client_order_id,
            venue_id=str((leg or {}).get("venue_id") or ""),
            account_id=str((leg or {}).get("account_id") or ""),
            venue_order_id=None,
            submitted_quantity=submitted,
            cumulative_filled_quantity=0,
            cumulative_cost_units=0,
            cumulative_fee_units=0,
            state="UNKNOWN",
            sequence=None,
            rest_confirmed=True,
            rest_observation_version=1,
            observed_at=moment.isoformat(),
            venue_timestamp=moment.isoformat(),
        )

    # -- helpers ---------------------------------------------------------------

    def _reconciliation_watch(
        self, batch_id: str, moment: datetime
    ) -> dict[str, object]:
        """Review round 2 (P1) + repair round 3 (F1): a batch awaiting
        reconciliation is never skipped silently forever, and it is never
        wedged by a single failed attempt. Every tick the watch retries
        ``complete_reconciliation`` through a freshly built, verified context
        (a successful retry completes the batch and releases the active-batch
        ownership); inside the configured window a still-unreconciled batch
        is the ordinary batch-active skip, and beyond it the queue row is
        flagged and the tick returns a visible blocked state with a stable
        reason. Without a context factory there is nothing to retry with —
        the batch is never force-completed."""
        store = self._store
        batch = store.n_leg_batch(batch_id)
        if batch is None:
            return {"skipped": "EXECUTION_BATCH_ACTIVE"}
        if str(batch.get("state")) != "AWAITING_RECONCILIATION":
            # F2: the watchdog covers ANY active batch — a batch that dies
            # between admission and receipt folding (or whose receipts never
            # fold) must surface visibly, not skip silently forever.
            return self._active_batch_watchdog(batch, moment)
        row = self._batch_request_row(batch_id)
        if row is None:
            return {"skipped": "EXECUTION_BATCH_ACTIVE"}
        # F1: retry on every tick — one factory failure (the typical
        # FOK-just-filled / snapshot-not-yet-visible
        # N_LEG_RECONCILIATION_SOURCE_UNAVAILABLE) must never wedge the
        # queue head behind a permanently held reservation.
        if self._try_complete_reconciliation(batch_id, moment):
            self._mark_batch_rows_submitted(batch_id)
            return {"reconciled": batch_id}
        payload = (
            row["payload"] if isinstance(row.get("payload"), Mapping) else {}
        )
        stamped = payload.get("awaiting_reconciliation_at")
        if not isinstance(stamped, str):
            # Restart recovery: the stamp was lost; start the window from now.
            store.n_leg_request_update(
                str(row["request_id"]),
                payload_merge={
                    "awaiting_reconciliation_at": moment.isoformat()
                },
            )
            return {"skipped": "EXECUTION_BATCH_ACTIVE"}
        try:
            awaiting_at = datetime.fromisoformat(stamped)
        except ValueError:
            awaiting_at = moment
        age = (moment - awaiting_at).total_seconds()
        if age < self._reconciliation_timeout_seconds():
            return {"skipped": "EXECUTION_BATCH_ACTIVE"}
        logger.warning(
            "n_leg_queue_driver_reconciliation_overdue batch=%s "
            "awaiting_since=%s age_seconds=%.0f — manual reconciliation "
            "evidence is required; the batch stays visible and blocked "
            "(retrying every tick)",
            batch_id,
            stamped,
            age,
        )
        store.n_leg_request_update(
            str(row["request_id"]),
            payload_merge={
                "reconciliation_overdue": True,
                "reconciliation_overdue_at": moment.isoformat(),
            },
        )
        return {
            "blocked": RECONCILIATION_TIMEOUT,
            "execution_batch_id": batch_id,
        }

    def _active_batch_watchdog(
        self, batch: Mapping[str, object], moment: datetime
    ) -> dict[str, object]:
        """F2: visibility for an ACTIVE (non-AWAITING) batch that outlived
        the reconciliation window — same treatment as the AWAITING overdue
        case (row flag + log + visible blocked reason). Fail-closed: it only
        observes, it never re-drives a submission and never mutates the
        batch."""
        batch_id = str(batch.get("execution_batch_id"))
        row = self._batch_request_row(batch_id)
        if row is None:
            return {"skipped": "EXECUTION_BATCH_ACTIVE"}
        payload = (
            row["payload"] if isinstance(row.get("payload"), Mapping) else {}
        )
        stamped = payload.get("active_batch_since")
        if not isinstance(stamped, str):
            stamped = payload.get("admitted_at")
        if not isinstance(stamped, str):
            # Restart recovery: no usable age anchor; start the window now.
            self._store.n_leg_request_update(
                str(row["request_id"]),
                payload_merge={"active_batch_since": moment.isoformat()},
            )
            return {"skipped": "EXECUTION_BATCH_ACTIVE"}
        try:
            since = datetime.fromisoformat(stamped)
        except ValueError:
            since = moment
        age = (moment - since).total_seconds()
        if age < self._reconciliation_timeout_seconds():
            return {"skipped": "EXECUTION_BATCH_ACTIVE"}
        logger.warning(
            "n_leg_queue_driver_active_batch_overdue batch=%s state=%s "
            "since=%s age_seconds=%.0f — the batch outlived the "
            "reconciliation window while still active; it stays visible and "
            "blocked (no submission is re-driven)",
            batch_id,
            str(batch.get("state")),
            stamped,
            age,
        )
        self._store.n_leg_request_update(
            str(row["request_id"]),
            payload_merge={
                "active_batch_overdue": True,
                "active_batch_overdue_at": moment.isoformat(),
            },
        )
        return {
            "blocked": ACTIVE_BATCH_OVERDUE,
            "execution_batch_id": batch_id,
        }

    def _try_complete_reconciliation(
        self, batch_id: str, moment: datetime
    ) -> bool:
        """One verified reconciliation attempt: build a fresh context through
        the factory and complete the batch with it. Every failure is a
        logged ``ValueError`` — the batch is never completed without a
        verified context."""
        factory = self._reconciliation_context_factory
        if factory is None:
            return False
        try:
            context = factory(batch_id)
            batch = self._service.complete_reconciliation(
                batch_id, context=context
            )
        except ValueError:
            logger.exception(
                "n_leg_queue_driver reconciliation attempt failed batch=%s",
                batch_id,
            )
            return False
        return str(batch.get("state", "")).startswith("RECONCILED")

    def _mark_batch_rows_submitted(self, batch_id: str) -> None:
        for row in self._store.n_leg_requests():
            payload = row.get("payload")
            if (
                isinstance(payload, Mapping)
                and payload.get("execution_batch_id") == batch_id
            ):
                self._store.n_leg_request_update(
                    str(row["request_id"]), state="SUBMITTED"
                )

    def _reconciliation_timeout_seconds(self) -> float:
        try:
            safety = self._store.n_leg_safety_config_latest()
            config = (
                safety.get("config") if isinstance(safety, Mapping) else None
            )
            value = int(
                (config or {}).get(
                    "reconciliation_timeout_seconds",
                    DEFAULT_RECONCILIATION_TIMEOUT_SECONDS,
                )
            )
        except (TypeError, ValueError, AttributeError):
            return DEFAULT_RECONCILIATION_TIMEOUT_SECONDS
        return value if value > 0 else DEFAULT_RECONCILIATION_TIMEOUT_SECONDS

    def _stamp_awaiting_reconciliation(self, batch_id: str, moment: datetime) -> None:
        row = self._batch_request_row(batch_id)
        if row is None:
            return
        payload = (
            row["payload"] if isinstance(row.get("payload"), Mapping) else {}
        )
        if payload.get("awaiting_reconciliation_at") is None:
            self._store.n_leg_request_update(
                str(row["request_id"]),
                payload_merge={
                    "awaiting_reconciliation_at": moment.isoformat()
                },
            )

    def _batch_request_row(self, batch_id: str) -> Mapping[str, object] | None:
        for row in self._store.n_leg_requests():
            payload = row.get("payload")
            if (
                isinstance(payload, Mapping)
                and payload.get("execution_batch_id") == batch_id
            ):
                return row
        return None

    def _abandon(self, head: Mapping[str, object], reason: str) -> dict[str, object]:
        self._store.n_leg_request_update(
            str(head["request_id"]), state="ABANDONED", abandon_reason=reason
        )
        return {"abandoned": reason, "request_id": str(head["request_id"])}

    @staticmethod
    def _all_legs_buy(frozen: Mapping[str, object]) -> bool:
        execution = frozen.get("execution")
        legs = (
            execution.get("execution_legs")
            if isinstance(execution, Mapping)
            else None
        )
        if not isinstance(legs, (list, tuple)) or not legs:
            return True
        return all(
            str(leg.get("side") or "BUY").startswith("BUY")
            for leg in legs
            if isinstance(leg, Mapping)
        )

    @staticmethod
    def _policy(store: object, frozen: Mapping[str, object]) -> dict[str, object]:
        try:
            policy = n_leg_mode_contract(store)["qualification_policy"]
        except Exception:
            policy = None
        if isinstance(policy, Mapping) and isinstance(policy.get("policy"), Mapping):
            policy = policy["policy"]
        return dict(policy) if isinstance(policy, Mapping) else {}

    @staticmethod
    def _expected_versions(frozen: Mapping[str, object]) -> dict[str, object]:
        versions = (
            frozen.get("versions")
            if isinstance(frozen.get("versions"), Mapping)
            else {}
        )
        execution = (
            frozen.get("execution")
            if isinstance(frozen.get("execution"), Mapping)
            else {}
        )
        expected: dict[str, object] = {
            "breaker_closed": True,
            "mode": versions.get("mode"),
            "contract_generation": versions.get("contract_generation"),
            "qualification_policy_version": versions.get(
                "qualification_policy_version"
            ),
            "capability": versions.get("capability"),
            "scope_id": versions.get("scope_id"),
            "scope_version": versions.get("scope_version"),
            "caps_fingerprint": versions.get("caps_fingerprint"),
            # enter() stamps the batch with the decoded solution's own
            # fingerprint; the frozen execution payload carries exactly it.
            "execution_solution_fingerprint": execution.get("fingerprint"),
        }
        account_fp = execution.get("account_snapshot_fingerprint")
        if account_fp is not None:
            expected["account_snapshot_fingerprint"] = account_fp
        return expected

    @staticmethod
    def _cap_config_version(frozen: Mapping[str, object]) -> str:
        versions = (
            frozen.get("versions")
            if isinstance(frozen.get("versions"), Mapping)
            else {}
        )
        return f"caps-v{versions.get('safety_config_version') or 1}"

    def _receipt_for(
        self,
        batch_id: str,
        client_order_id: str,
        leg: Mapping[str, object] | None,
        outcome: Mapping[str, object],
        moment: datetime,
    ) -> OrderReceipt:
        state = str(outcome.get("state") or "UNKNOWN")
        submitted = int((leg or {}).get("submitted_quantity") or 0)
        max_cost = int((leg or {}).get("max_cost_units") or 0)
        if state == "FILLED":
            filled = submitted
            # The adapter may report the actual fill cost; otherwise the
            # FOK limit bound is booked conservatively.
            cost = min(
                max_cost,
                int(outcome.get("cost_units") or max_cost),
            )
        else:
            filled, cost = 0, 0
        return OrderReceipt(
            receipt_id=f"{client_order_id}:1",
            execution_batch_id=batch_id,
            client_order_id=client_order_id,
            venue_id=str((leg or {}).get("venue_id") or ""),
            account_id=str((leg or {}).get("account_id") or ""),
            venue_order_id=None,
            submitted_quantity=submitted,
            cumulative_filled_quantity=filled,
            cumulative_cost_units=cost,
            cumulative_fee_units=0,
            state=state,  # type: ignore[arg-type]
            sequence=None,
            rest_confirmed=True,
            rest_observation_version=1,
            observed_at=moment.isoformat(),
            venue_timestamp=moment.isoformat(),
        )


def trading_reconciliation_context_factory(
    store: object,
    trading: object,
    *,
    units_per_dollar: int = NLEG_UNITS_PER_DOLLAR,
) -> Callable[[str], ReconciliationContext]:
    """Review round 2 (P1): the production reconciliation context factory the
    runtime wires into ``NLegOrderQueueDriver``.

    Per batch it takes ONE fresh venue read — the trading client's account
    snapshot (collateral balance/allowance plus venue positions) — and binds
    it to the batch's booked receipts, in exactly the ``ReconciliationContext``
    shape ``complete_reconciliation`` verifies: balances must cover every
    settlement key, venue positions must show at least the booked fills, and
    the per-order cash flows carry the booked economics re-observed fresh
    (``_bound_cash_flows`` re-proves the binding economics at fold time).
    Every failure is a ``ValueError`` so the driver falls into its visible
    AWAITING_RECONCILIATION path instead of crashing the loop.
    """
    from datetime import UTC, datetime
    from decimal import Decimal, InvalidOperation

    def _position_quantity(
        positions: object, asset_id: str
    ) -> int | None:
        if not isinstance(positions, (list, tuple)):
            return None
        for position in positions:
            payload = position
            if not isinstance(payload, Mapping):
                continue
            token = None
            for key in ("token_id", "asset_id", "tokenId", "token"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    token = value
                    break
            if token != asset_id:
                continue
            for key in ("size", "quantity", "position_size", "amount"):
                raw = payload.get(key)
                if isinstance(raw, str) and raw:
                    try:
                        quantity = int(Decimal(raw))
                    except (InvalidOperation, ValueError):
                        continue
                    return quantity if quantity > 0 else None
        return None

    def factory(batch_id: str) -> ReconciliationContext:
        batch = store.n_leg_batch(str(batch_id))
        if batch is None:
            raise ValueError("N_LEG_BATCH_NOT_FOUND")
        legs = [
            dict(leg)
            for leg in batch.get("legs", [])
            if isinstance(leg, dict)
        ]
        if not legs:
            raise ValueError("N_LEG_RECONCILIATION_SOURCE_UNAVAILABLE")
        try:
            venue = trading.account_snapshot()
        except Exception as exc:
            raise ValueError(
                "N_LEG_RECONCILIATION_SOURCE_UNAVAILABLE"
            ) from exc
        # The observation instant is taken AFTER the venue read so the read's
        # own checked_at can never land in this instant's future.
        moment = datetime.now(UTC)
        checked_at = getattr(venue, "checked_at", None)
        if not isinstance(checked_at, datetime) or checked_at.tzinfo is None:
            raise ValueError("N_LEG_RECONCILIATION_SOURCE_UNAVAILABLE")
        if not 0 <= (moment - checked_at).total_seconds() <= 10:
            raise ValueError("N_LEG_RECONCILIATION_SOURCE_UNAVAILABLE")
        try:
            available = int(
                Decimal(str(getattr(venue, "p_usd_balance"))) * units_per_dollar
            )
            allowance = int(
                Decimal(str(getattr(venue, "p_usd_allowance")))
                * units_per_dollar
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                "N_LEG_RECONCILIATION_SOURCE_UNAVAILABLE"
            ) from exc
        positions = getattr(venue, "positions", None)
        settlement_keys = sorted(
            {
                (
                    str(leg["venue_id"]),
                    str(leg["account_id"]),
                    str(leg["settlement_asset_id"]),
                )
                for leg in legs
            }
        )
        balances = tuple(
            AccountBalance(venue_id, account_id, asset_id, available, allowance)
            for venue_id, account_id, asset_id in settlement_keys
        )
        try:
            safety = store.n_leg_safety_config_latest()
            config = (
                safety.get("config") if isinstance(safety, Mapping) else {}
            ) or {}
            control = store.n_leg_control()
        except (TypeError, ValueError, RuntimeError):
            config, control = {}, {}
        snapshot = AccountSnapshot(
            captured_at=moment,
            balances=balances,
            max_per_trade_cost_units=int(
                config.get("max_per_trade_cost_units", 0) or 0
            ),
            unsettled_capital_units=int(
                control.get("total_unsettled_capital_units", 0) or 0
            ),
            max_total_unsettled_capital_units=int(
                config.get("max_total_unsettled_capital_units", 0) or 0
            ),
        )
        holdings: list[ConfirmedHolding] = []
        flows: list[SettlementCashFlow] = []
        for leg in legs:
            receipt = leg.get("receipt")
            if not isinstance(receipt, dict):
                raise ValueError("N_LEG_RECONCILIATION_NOT_READY")
            filled = int(receipt["cumulative_filled_quantity"])
            if filled > 0:
                quantity = _position_quantity(
                    positions, str(leg["asset_id"])
                )
                if quantity is None or quantity < filled:
                    # The venue must independently show at least the booked
                    # fill; a missing/short position is fail-closed evidence.
                    raise ValueError(
                        "N_LEG_RECONCILIATION_SOURCE_UNAVAILABLE"
                    )
                holdings.append(
                    ConfirmedHolding(
                        str(leg["venue_id"]),
                        str(leg["account_id"]),
                        str(leg["asset_id"]),
                        filled,
                        moment,
                        moment,
                    )
                )
            flows.append(
                SettlementCashFlow(
                    str(leg["client_order_id"]),
                    receipt.get("venue_order_id"),
                    str(leg["venue_id"]),
                    str(leg["account_id"]),
                    str(leg["settlement_asset_id"]),
                    int(receipt["cumulative_cost_units"]),
                    int(receipt["cumulative_fee_units"]),
                    moment,
                    moment,
                    (
                        receipt.get("rest_observation_version")
                        if receipt.get("rest_confirmed")
                        else receipt.get("sequence")
                    ),
                    bool(receipt.get("rest_confirmed")),
                )
            )
        return ReconciliationContext(
            str(batch["reservation_version"]),
            snapshot,
            tuple(holdings),
            tuple(flows),
            moment,
            moment,
            moment,
        )

    return factory


def _leg_by_id(
    batch: Mapping[str, object], client_order_id: str
) -> dict[str, object] | None:
    for leg in batch.get("legs", []) if isinstance(batch, Mapping) else []:
        if isinstance(leg, dict) and leg.get("client_order_id") == client_order_id:
            return leg
    return None


def _admission_reason(message: str) -> str:
    """Translate store admission literals to the operator-facing set."""
    mapping = {
        "N_LEG_ADMISSION_VERSION_STALE": VERSION_STALE,
        "N_LEG_ADMISSION_UNSETTLED_CAP": UNSETTLED_CAP,
        "N_LEG_ACTIVE_BATCH_EXISTS": "EXECUTION_BATCH_ACTIVE",
        "N_LEG_LINEAGE_ALREADY_CLAIMED": "N_LEG_LINEAGE_ALREADY_CLAIMED",
        "N_LEG_BREAKER_OPEN": "GLOBAL_BREAKER_OPEN",
    }
    return mapping.get(message, message)
