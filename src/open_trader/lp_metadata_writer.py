"""Bounded FIFO persistence for disposable LP metadata cache snapshots.

Only this thread calls the Store. Queue locks never cover persistence or joins,
and completion never changes the client's current metadata facts.
"""

from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
import logging
import threading


logger = logging.getLogger(__name__)
DEFAULT_POLICY = dict(max_entries=3000, max_encoded_bytes=16 * 1024 * 1024, batch_rows=400)


def writer_policy(policy: Mapping[str, int] | None = None) -> dict[str, int]:
    values = DEFAULT_POLICY | dict(policy or {})
    if set(values) != set(DEFAULT_POLICY):
        raise ValueError("unknown metadata writer policy")
    if any(type(value) is not int or value < 1 for value in values.values()):
        raise ValueError("metadata writer limits must be positive integers")
    if values["batch_rows"] > 400:
        raise ValueError("metadata writer batch_rows must not exceed 400")
    return values


def empty_status(*, closed=False):
    return dict(state="STOPPED" if closed else "NOT_STARTED", thread_alive=False,
                pending_entries=0, inflight_entries=0, pending_bytes=0, inflight_bytes=0,
                saved_entries=0, failed_entries=0, unknown_outcome_entries=0,
                dropped_overflow=0, dropped_oversize=0, dropped_inflight_capacity=0,
                dropped_shutdown=0, dropped_closed=0, prune_pending=0, prune_inflight=0,
                prune_retained_bytes=0, prune_completed_transactions=0, prune_deleted_rows=0,
                prune_failed=0, prune_unknown_outcome=0, prune_dropped_shutdown=0, prune_dropped_closed=0)


def _encode_scalar(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("metadata snapshot contains an unsupported value")


@dataclass(frozen=True)
class _Prune:
    cutoff: datetime


class LPMetadataWriter:
    def __init__(self, store, *, policy=None):
        self._store = store
        self._policy = writer_policy(policy)
        self._condition = threading.Condition()
        self._pending = deque()
        self._pending_bytes = 0
        self._pending_entries = 0
        self._prune_active = False
        self._prune_inflight = False
        self._prune_counts = dict(prune_completed_transactions=0, prune_deleted_rows=0,
            prune_failed=0, prune_unknown_outcome=0, prune_dropped_shutdown=0, prune_dropped_closed=0)
        self._inflight = 0
        self._inflight_bytes = 0
        self._counts = {key: value for key, value in empty_status().items()
                        if key.startswith(("saved_", "failed_", "unknown_", "dropped_"))}
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="lp-metadata-writer", daemon=True)
        self._thread.start()

    def enqueue(self, entries):
        # Keep normalized values, including source timestamps, independently of
        # callers. The byte budget measures encoded snapshots, not Python RSS.
        snapshots = []
        for cid, entry in entries.items():
            frozen = deepcopy(entry)
            size = len(json.dumps((cid, frozen), ensure_ascii=False,
                                  separators=(",", ":"), default=_encode_scalar).encode("utf-8"))
            snapshots.append((cid, frozen, size))
        with self._condition:
            for cid, entry, size in snapshots:
                if self._closed:
                    self._counts["dropped_closed"] += 1
                    continue
                if size > self._policy["max_encoded_bytes"]:
                    self._counts["dropped_oversize"] += 1
                    continue

                def full():
                    return (self._pending_entries + self._inflight + 1 > self._policy["max_entries"]
                            or self._pending_bytes + self._inflight_bytes + size
                            > self._policy["max_encoded_bytes"])

                while self._pending_entries and full():
                    index = next(i for i, row in enumerate(self._pending) if not isinstance(row, _Prune))
                    _, _, removed_size = self._pending[index]
                    del self._pending[index]
                    self._pending_entries -= 1
                    self._pending_bytes -= removed_size
                    self._counts["dropped_overflow"] += 1
                if full():
                    self._counts["dropped_inflight_capacity"] += 1
                    continue
                self._pending.append((cid, entry, size))
                self._pending_entries += 1
                self._pending_bytes += size
            self._condition.notify()

    def enqueue_prune(self, *, now):
        """Reserve at most one fixed-size maintenance intent, including inflight."""
        with self._condition:
            if self._closed:
                self._prune_counts['prune_dropped_closed'] += 1
                return False
            if self._prune_active:
                return False
            self._prune_active = True
            self._pending.append(_Prune(now))
            self._condition.notify()
            return True

    def stop(self, *, timeout=5):
        with self._condition:
            self._closed = True
            self._counts["dropped_shutdown"] += self._pending_entries
            self._pending_entries = 0
            if self._prune_active and not self._prune_inflight:
                self._prune_counts["prune_dropped_shutdown"] += 1
                self._prune_active = False
            self._pending.clear()
            self._pending_bytes = 0
            self._condition.notify_all()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            raise TimeoutError("LP metadata writer is still stopping")

    def status(self):
        with self._condition:
            alive = self._thread.is_alive()
            return dict(
                state=("STOPPING" if alive else "STOPPED") if self._closed else "RUNNING",
                thread_alive=alive, pending_entries=self._pending_entries,
                inflight_entries=self._inflight, pending_bytes=self._pending_bytes,
                inflight_bytes=self._inflight_bytes, **self._counts,
                prune_pending=int(self._prune_active and not self._prune_inflight),
                prune_inflight=int(self._prune_inflight),
                prune_retained_bytes=1024 if self._prune_active else 0, **self._prune_counts,
            )

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending)
                if self._closed:
                    return
                prune = self._pending[0] if isinstance(self._pending[0], _Prune) else None
                if prune is not None:
                    self._pending.popleft()
                    self._prune_inflight = True
                entries = {}
                batch_bytes = 0
                while prune is None and self._pending and len(entries) < self._policy["batch_rows"]:
                    if isinstance(self._pending[0], _Prune):
                        break
                    cid, entry, size = self._pending[0]
                    # A Mapping cannot represent repeated IDs. End this batch
                    # before the duplicate, preserving every FIFO entry/write.
                    if cid in entries:
                        break
                    self._pending.popleft()
                    self._pending_entries -= 1
                    entries[cid] = entry
                    batch_bytes += size
                self._pending_bytes -= batch_bytes
                self._inflight = len(entries)
                self._inflight_bytes = batch_bytes
            if prune is not None:
                self._run_prune(prune)
                prune = None
                continue
            failed = False
            try:
                self._store.lp_metadata_cache_store_entries(entries)
            except Exception as exc:
                failed = True
                # A raised save can have an unknown commit outcome. Never replay.
                try:
                    logger.warning("lp_metadata_save_outcome_unknown error_type=%s", type(exc).__name__)
                except Exception:
                    pass
            finally:
                count = len(entries)
                # Do not retain a completed batch in the sleeping worker's
                # frame after removing it from the in-flight capacity count.
                entries.clear()
                entry = None
                with self._condition:
                    if failed:
                        self._counts["failed_entries"] += count
                        self._counts["unknown_outcome_entries"] += count
                    else:
                        self._counts["saved_entries"] += count
                    self._inflight = self._inflight_bytes = 0

    def _run_prune(self, intent):
        failed = False
        deleted, remaining = 0, False
        try:
            result = self._store.lp_metadata_cache_prune(now=intent.cutoff)
            if isinstance(result, tuple):
                deleted, remaining = result
        except Exception as exc:
            failed = True
            try:
                logger.warning('lp_metadata_prune_outcome_unknown error_type=%s', type(exc).__name__)
            except Exception:
                pass
        finally:
            with self._condition:
                if failed:
                    self._prune_counts['prune_failed'] += 1
                    self._prune_counts['prune_unknown_outcome'] += 1
                else:
                    self._prune_counts['prune_completed_transactions'] += 1
                    self._prune_counts['prune_deleted_rows'] += deleted or 0
                self._prune_inflight = False
                if not failed and remaining and not self._closed:
                    self._pending.append(intent)
                    self._condition.notify()
                else:
                    self._prune_active = False
