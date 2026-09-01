"""Issue #106: minimal opportunity episodes ("病历卡") for the N_LEG list.

An episode is one component's qualification "medical record": the first
qualified snapshot opens it, five consecutive minutes of fresh negative
proof close it (``episode_rearm_gap_seconds``), restarts resume it, and a
retired component closes it immediately. Every non-positive event clears
the negative close timer; only an accepted (binding-matched, quote-fresh)
negative proof starts or advances it. OBSERVE_ONLY: this module only
accumulates simulated-side statistics and never submits orders.

``EpisodeTracker`` is a pure, clock-injected state machine keyed by
``component_id``; ``EpisodeStore`` persists state changes to the shared
prediction SQLite with its own expand-only DDL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


STATUS_ONGOING = "ONGOING"
STATUS_CLOSED = "CLOSED"
CLOSE_NO_QUALIFIED_OPPORTUNITY = "NO_QUALIFIED_OPPORTUNITY"
CLOSE_COMPONENT_RETIRED = "COMPONENT_RETIRED"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: object) -> datetime:
    text = str(value)
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


@dataclass
class EpisodeRecord:
    """One component's mutable episode state (persisted on state changes)."""

    opportunity_episode_id: str
    episode_lineage_id: str
    component_id: str
    component_generation: int
    model_fingerprint: str | None
    quote_fingerprint: str | None
    qualification_fingerprint: str | None
    qualification_policy_version: str | None
    opened_at: datetime
    last_seen_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    close_reason: str | None = None
    best_guaranteed_profit: Decimal | None = None
    worst_guaranteed_profit: Decimal | None = None
    would_submit_ready_seconds: float = 0.0
    would_submit_ready_since: datetime | None = None
    would_submit_plan_snapshot: str | None = None
    negative_close_started_at: datetime | None = None

    @property
    def status(self) -> str:
        return STATUS_CLOSED if self.closed_at is not None else STATUS_ONGOING


_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunity_episodes (
  opportunity_episode_id TEXT PRIMARY KEY,
  episode_lineage_id TEXT NOT NULL,
  component_id TEXT NOT NULL,
  component_generation INTEGER NOT NULL,
  model_fingerprint TEXT, quote_fingerprint TEXT,
  qualification_fingerprint TEXT, qualification_policy_version TEXT,
  opened_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  closed_at TEXT, close_reason TEXT,
  best_guaranteed_profit TEXT, worst_guaranteed_profit TEXT,
  would_submit_ready_seconds REAL NOT NULL DEFAULT 0,
  would_submit_ready_since TEXT,
  would_submit_plan_snapshot TEXT,
  negative_close_started_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_episode_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opportunity_episode_id TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  proof_fingerprint TEXT NOT NULL,
  generation INTEGER, model_fingerprint TEXT, quote_fingerprint TEXT,
  qualification_fingerprint TEXT
);
"""


class EpisodeStore:
    """Persist episodes in the shared prediction SQLite (expand-only DDL)."""

    def __init__(self, data_dir: str | Path) -> None:
        self.path = (
            Path(data_dir)
            / "prediction_arbitrage"
            / "prediction_arbitrage.sqlite3"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    def save_episode(self, record: EpisodeRecord) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO opportunity_episodes(
                    opportunity_episode_id, episode_lineage_id, component_id,
                    component_generation, model_fingerprint, quote_fingerprint,
                    qualification_fingerprint, qualification_policy_version,
                    opened_at, last_seen_at, closed_at, close_reason,
                    best_guaranteed_profit, worst_guaranteed_profit,
                    would_submit_ready_seconds, would_submit_ready_since,
                    would_submit_plan_snapshot, negative_close_started_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_episode_id) DO UPDATE SET
                    episode_lineage_id=excluded.episode_lineage_id,
                    component_id=excluded.component_id,
                    component_generation=excluded.component_generation,
                    model_fingerprint=excluded.model_fingerprint,
                    quote_fingerprint=excluded.quote_fingerprint,
                    qualification_fingerprint=excluded.qualification_fingerprint,
                    qualification_policy_version=excluded.qualification_policy_version,
                    opened_at=excluded.opened_at,
                    last_seen_at=excluded.last_seen_at,
                    closed_at=excluded.closed_at,
                    close_reason=excluded.close_reason,
                    best_guaranteed_profit=excluded.best_guaranteed_profit,
                    worst_guaranteed_profit=excluded.worst_guaranteed_profit,
                    would_submit_ready_seconds=excluded.would_submit_ready_seconds,
                    would_submit_ready_since=excluded.would_submit_ready_since,
                    would_submit_plan_snapshot=excluded.would_submit_plan_snapshot,
                    negative_close_started_at=excluded.negative_close_started_at,
                    updated_at=excluded.updated_at
                """,
                (
                    record.opportunity_episode_id,
                    record.episode_lineage_id,
                    record.component_id,
                    int(record.component_generation),
                    record.model_fingerprint,
                    record.quote_fingerprint,
                    record.qualification_fingerprint,
                    record.qualification_policy_version,
                    _iso(record.opened_at),
                    _iso(record.last_seen_at),
                    None if record.closed_at is None else _iso(record.closed_at),
                    record.close_reason,
                    (
                        None
                        if record.best_guaranteed_profit is None
                        else format(record.best_guaranteed_profit, "f")
                    ),
                    (
                        None
                        if record.worst_guaranteed_profit is None
                        else format(record.worst_guaranteed_profit, "f")
                    ),
                    float(record.would_submit_ready_seconds),
                    (
                        None
                        if record.would_submit_ready_since is None
                        else _iso(record.would_submit_ready_since)
                    ),
                    record.would_submit_plan_snapshot,
                    (
                        None
                        if record.negative_close_started_at is None
                        else _iso(record.negative_close_started_at)
                    ),
                    _iso(record.updated_at),
                ),
            )

    def record_proof(
        self,
        *,
        opportunity_episode_id: str,
        recorded_at: datetime,
        proof_fingerprint: str,
        generation: int | None,
        model_fingerprint: str | None,
        quote_fingerprint: str | None,
        qualification_fingerprint: str | None,
    ) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO opportunity_episode_proofs(
                    opportunity_episode_id, recorded_at, proof_fingerprint,
                    generation, model_fingerprint, quote_fingerprint,
                    qualification_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    opportunity_episode_id,
                    _iso(recorded_at),
                    proof_fingerprint,
                    None if generation is None else int(generation),
                    model_fingerprint,
                    quote_fingerprint,
                    qualification_fingerprint,
                ),
            )

    def load_open(self) -> dict[str, dict[str, Any]]:
        """Open (never closed) episode rows keyed by component id."""
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM opportunity_episodes WHERE closed_at IS NULL"
            ).fetchall()
        return {str(row["component_id"]): dict(row) for row in rows}

    def proofs_for_episode(self, opportunity_episode_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM opportunity_episode_proofs"
                " WHERE opportunity_episode_id=? ORDER BY id",
                (opportunity_episode_id,),
            ).fetchall()
        return [dict(row) for row in rows]


class EpisodeTracker:
    """Pure episode state machine keyed by component_id (injected clock)."""

    def __init__(
        self,
        *,
        store: EpisodeStore | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._now_fn = now_fn or _utc_now
        self._lock = threading.RLock()
        self._episodes: dict[str, EpisodeRecord] = {}

    # -- read seams ---------------------------------------------------------

    def project(
        self, now: datetime | None = None
    ) -> dict[str, dict[str, object]]:
        """Read-side episode projection keyed by component id."""
        at = now or self._now_fn()
        with self._lock:
            projections: dict[str, dict[str, object]] = {}
            for component_id, record in self._episodes.items():
                closed = record.closed_at is not None
                if closed:
                    duration = (
                        record.closed_at - record.opened_at
                    ).total_seconds()
                    ready = record.would_submit_ready_seconds
                else:
                    duration = (at - record.opened_at).total_seconds()
                    ready = record.would_submit_ready_seconds + (
                        (
                            at - record.would_submit_ready_since
                        ).total_seconds()
                        if record.would_submit_ready_since is not None
                        else 0.0
                    )
                projections[component_id] = {
                    "opportunity_episode_id": record.opportunity_episode_id,
                    "episode_lineage_id": record.episode_lineage_id,
                    "status": record.status,
                    "opened_at": _iso(record.opened_at),
                    "duration_seconds": duration,
                    "would_submit_ready_seconds": ready,
                    "best_guaranteed_profit": (
                        None
                        if record.best_guaranteed_profit is None
                        else format(record.best_guaranteed_profit, "f")
                    ),
                    "close_reason": record.close_reason,
                }
            return projections

    def episode(self, component_id: str) -> EpisodeRecord | None:
        with self._lock:
            record = self._episodes.get(component_id)
            return None if record is None else replace(record)

    # -- state transitions --------------------------------------------------

    def observe_qualified(
        self,
        component_id: str,
        lineage_id: str,
        profit: Decimal,
        would_submit: bool,
        plan_snapshot: Mapping[str, object] | None,
        fingerprints: Mapping[str, object],
        now: datetime,
    ) -> None:
        """Open or update the episode; always resets the negative timer."""
        with self._lock:
            current = self._episodes.get(component_id)
            if current is None or current.closed_at is not None:
                record = EpisodeRecord(
                    opportunity_episode_id=uuid.uuid4().hex,
                    episode_lineage_id=str(lineage_id or component_id),
                    component_id=component_id,
                    component_generation=int(fingerprints.get("component_generation") or 0),
                    model_fingerprint=(
                        None if fingerprints.get("model_fingerprint") is None
                        else str(fingerprints["model_fingerprint"])
                    ),
                    quote_fingerprint=(
                        None if fingerprints.get("quote_fingerprint") is None
                        else str(fingerprints["quote_fingerprint"])
                    ),
                    qualification_fingerprint=(
                        None if fingerprints.get("qualification_fingerprint") is None
                        else str(fingerprints["qualification_fingerprint"])
                    ),
                    qualification_policy_version=(
                        None if fingerprints.get("qualification_policy_version") is None
                        else str(fingerprints["qualification_policy_version"])
                    ),
                    opened_at=now,
                    last_seen_at=now,
                    updated_at=now,
                    best_guaranteed_profit=Decimal(profit),
                    worst_guaranteed_profit=Decimal(profit),
                    would_submit_plan_snapshot=(
                        None
                        if plan_snapshot is None
                        else json.dumps(plan_snapshot, sort_keys=True, default=str)
                    ),
                )
            else:
                record = current
                record.last_seen_at = now
                record.updated_at = now
                record.component_generation = int(
                    fingerprints.get("component_generation") or 0
                )
                record.model_fingerprint = (
                    None if fingerprints.get("model_fingerprint") is None
                    else str(fingerprints["model_fingerprint"])
                )
                record.quote_fingerprint = (
                    None if fingerprints.get("quote_fingerprint") is None
                    else str(fingerprints["quote_fingerprint"])
                )
                record.qualification_fingerprint = (
                    None if fingerprints.get("qualification_fingerprint") is None
                    else str(fingerprints["qualification_fingerprint"])
                )
                record.qualification_policy_version = (
                    None if fingerprints.get("qualification_policy_version") is None
                    else str(fingerprints["qualification_policy_version"])
                )
                if record.best_guaranteed_profit is None or Decimal(
                    profit
                ) > record.best_guaranteed_profit:
                    record.best_guaranteed_profit = Decimal(profit)
                if record.worst_guaranteed_profit is None or Decimal(
                    profit
                ) < record.worst_guaranteed_profit:
                    record.worst_guaranteed_profit = Decimal(profit)
            # A fresh qualified observation always resets the negative timer.
            record.negative_close_started_at = None
            self._apply_would_submit(record, bool(would_submit), now)
            self._episodes[component_id] = record
            self._persist(record)

    def observe_negative(
        self,
        component_id: str,
        *,
        proof_fingerprint: str,
        generation: int | None,
        model_fingerprint: str | None,
        quote_fingerprint: str | None,
        qualification_fingerprint: str | None,
        binding_matches: bool,
        quote_fresh: bool,
        gap_seconds: float,
        now: datetime,
        qualification_policy_version: str | None = None,
    ) -> None:
        """Consume one component negative proof against the open episode.

        Only a binding-matched proof on a fresh quote is accepted: the close
        timer starts at the FIRST accepted negative and an accepted negative
        arriving ``gap_seconds`` later closes the episode. A proof proven
        under a different component generation or qualification policy
        version rebinds the record to the new identity and restarts the
        window at its own timestamp — an old-state window never closes a
        new-state record. Mismatched or stale-arrival proofs only clear the
        timer (never a no-arbitrage signal) and leave no side-table row.
        """
        with self._lock:
            record = self._episodes.get(component_id)
            if record is None or record.closed_at is not None:
                return
            if not binding_matches or not quote_fresh:
                if record.negative_close_started_at is not None:
                    self._reset_negative_timer(record)
                    record.updated_at = now
                    self._persist(record)
                return
            self._rebind_generation_or_policy(
                record,
                generation=generation,
                qualification_policy_version=qualification_policy_version,
                model_fingerprint=model_fingerprint,
                quote_fingerprint=quote_fingerprint,
                qualification_fingerprint=qualification_fingerprint,
            )
            if self._store is not None:
                self._store.record_proof(
                    opportunity_episode_id=record.opportunity_episode_id,
                    recorded_at=now,
                    proof_fingerprint=str(proof_fingerprint),
                    generation=generation,
                    model_fingerprint=model_fingerprint,
                    quote_fingerprint=quote_fingerprint,
                    qualification_fingerprint=qualification_fingerprint,
                )
            if record.negative_close_started_at is None:
                record.negative_close_started_at = now
                record.updated_at = now
                self._persist(record)
                return
            elapsed = (now - record.negative_close_started_at).total_seconds()
            if elapsed >= float(gap_seconds):
                self._close(record, CLOSE_NO_QUALIFIED_OPPORTUNITY, now)

    def load_open(self, now: datetime | None = None) -> dict[str, EpisodeRecord]:
        """Restore open episodes from the store after a restart.

        Downtime never accumulates: the running would-submit ``since`` is
        cleared (kept seconds stay) and the negative close window is
        cleared, so the next accepted negative proof restarts it.
        """
        load_at = now or self._now_fn()
        with self._lock:
            loaded: dict[str, EpisodeRecord] = {}
            for component_id, row in (self._store.load_open() if self._store else {}).items():
                record = EpisodeRecord(
                    opportunity_episode_id=str(row["opportunity_episode_id"]),
                    episode_lineage_id=str(row["episode_lineage_id"]),
                    component_id=str(component_id),
                    component_generation=int(row["component_generation"]),
                    model_fingerprint=row["model_fingerprint"],
                    quote_fingerprint=row["quote_fingerprint"],
                    qualification_fingerprint=row["qualification_fingerprint"],
                    qualification_policy_version=row["qualification_policy_version"],
                    opened_at=_parse(row["opened_at"]),
                    last_seen_at=_parse(row["last_seen_at"]),
                    updated_at=load_at,
                    closed_at=None,
                    close_reason=None,
                    best_guaranteed_profit=_decimal_or_none(row["best_guaranteed_profit"]),
                    worst_guaranteed_profit=_decimal_or_none(row["worst_guaranteed_profit"]),
                    would_submit_ready_seconds=float(row["would_submit_ready_seconds"]),
                    would_submit_ready_since=None,
                    would_submit_plan_snapshot=row["would_submit_plan_snapshot"],
                    negative_close_started_at=None,
                )
                self._episodes[str(component_id)] = record
                loaded[str(component_id)] = replace(record)
            return loaded

    def observe_would_submit(
        self, component_id: str, *, would_submit: bool, now: datetime
    ) -> None:
        """Tick-driven would-submit transition on the open episode."""
        with self._lock:
            record = self._episodes.get(component_id)
            if record is None or record.closed_at is not None:
                return
            before_since = record.would_submit_ready_since
            self._apply_would_submit(record, bool(would_submit), now)
            if record.would_submit_ready_since is not before_since:
                record.updated_at = now
                self._persist(record)

    def open_component_ids(self) -> list[str]:
        """Component ids with an open (ONGOING) episode, sorted for ticks."""
        with self._lock:
            return sorted(
                component_id
                for component_id, record in self._episodes.items()
                if record.closed_at is None
            )

    def mark_quote_stale(self, component_id: str, *, now: datetime) -> None:
        """Stale quotes clear the negative timer (never a close signal).

        Persistence only happens on the actual non-None→None window
        transition: a permanently stale open episode is ticked every cycle
        and must not upsert SQLite on each one.
        """
        with self._lock:
            record = self._episodes.get(component_id)
            if record is None or record.closed_at is not None:
                return
            if record.negative_close_started_at is None:
                return
            self._reset_negative_timer(record)
            record.updated_at = now
            self._persist(record)

    def component_retired(self, component_id: str, *, now: datetime) -> None:
        """Close the open episode immediately with its own retirement cause."""
        with self._lock:
            record = self._episodes.get(component_id)
            if record is None or record.closed_at is not None:
                return
            self._close(record, CLOSE_COMPONENT_RETIRED, now)

    def observe_unknown(self, component_id: str, *, now: datetime) -> None:
        """UNKNOWN evidence only clears the negative timer; never a close.

        Any non-positive reset clears the running close window — unknown
        periods never count toward the five-minute close — and the next
        accepted negative proof restarts the window at its own timestamp.
        Persistence only happens on the actual non-None→None window
        transition.
        """
        with self._lock:
            record = self._episodes.get(component_id)
            if record is None or record.closed_at is not None:
                return
            if record.negative_close_started_at is None:
                return
            self._reset_negative_timer(record)
            record.updated_at = now
            self._persist(record)

    # -- internals ----------------------------------------------------------

    def _rebind_generation_or_policy(
        self,
        record: EpisodeRecord,
        *,
        generation: int | None,
        qualification_policy_version: str | None,
        model_fingerprint: str | None,
        quote_fingerprint: str | None,
        qualification_fingerprint: str | None,
    ) -> None:
        """Rebind the record when the proof's state changed under it.

        A generation or qualification policy version change clears the
        running close window outright; the caller then treats this proof as
        the new state's window start. ``None`` inputs mean "unknown, do not
        compare" and never trigger a rebind. The rebound fingerprints are
        persisted together with the window restart by the caller.
        """
        changed = False
        if generation is not None and int(generation) != record.component_generation:
            record.component_generation = int(generation)
            changed = True
        policy = (
            None if qualification_policy_version is None
            else str(qualification_policy_version)
        )
        if policy is not None and policy != record.qualification_policy_version:
            record.qualification_policy_version = policy
            changed = True
        if changed:
            record.model_fingerprint = model_fingerprint
            record.quote_fingerprint = quote_fingerprint
            record.qualification_fingerprint = qualification_fingerprint
            record.negative_close_started_at = None

    def _reset_negative_timer(self, record: EpisodeRecord) -> None:
        """Non-positive reset: clear the running close window outright.

        UNKNOWN, binding mismatch, stale-quote, generation and policy resets
        all clear the window — UNKNOWN and interrupted periods never count
        toward the five-minute close, and the next accepted negative proof
        restarts the window at its own timestamp.
        """
        record.negative_close_started_at = None

    def _close(
        self, record: EpisodeRecord, reason: str, now: datetime
    ) -> None:
        record.closed_at = now
        record.close_reason = reason
        record.updated_at = now
        self._apply_would_submit(record, False, now)
        record.negative_close_started_at = None
        self._persist(record)

    def _apply_would_submit(
        self, record: EpisodeRecord, would_submit: bool, now: datetime
    ) -> None:
        """Transition-driven would-submit accumulation."""
        if would_submit and record.would_submit_ready_since is None:
            record.would_submit_ready_since = now
        elif not would_submit and record.would_submit_ready_since is not None:
            record.would_submit_ready_seconds += (
                now - record.would_submit_ready_since
            ).total_seconds()
            record.would_submit_ready_since = None

    def _persist(self, record: EpisodeRecord) -> None:
        if self._store is not None:
            self._store.save_episode(record)


__all__ = [
    "CLOSE_COMPONENT_RETIRED",
    "CLOSE_NO_QUALIFIED_OPPORTUNITY",
    "EpisodeRecord",
    "EpisodeStore",
    "EpisodeTracker",
    "STATUS_CLOSED",
    "STATUS_ONGOING",
]
