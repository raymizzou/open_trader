"""Per-database LP write admission; persistence never grants trading authority.

Existing writers keep their threads and disposable queues. Only their native
transactions share this owner. Non-LP admission belongs to the later X-lite.
"""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
import threading
import time
import weakref
import math
import sqlite3
import logging


logger = logging.getLogger(__name__)
SLOW_WRITE_SECONDS = 1.0


_memory = threading.local()


class MemoryLock:
    """A real mutex with per-thread lock-order observation, without SQL."""
    def __init__(self, name, *, reentrant=False):
        self.name = name
        self._lock = threading.RLock() if reentrant else threading.Lock()

    def acquire(self, blocking=True, timeout=-1):
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            stack = getattr(_memory, 'held', ())
            _memory.held = (*stack, self)
        return acquired

    def release(self):
        held = list(getattr(_memory, 'held', ()))
        index = max(i for i, lock in enumerate(held) if lock is self)
        self._lock.release()
        del held[index]
        _memory.held = tuple(held)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()

    def locked(self):
        native = getattr(self._lock, 'locked', None)
        if callable(native):
            return native()
        if self._is_owned():
            return True
        acquired = self._lock.acquire(False)
        if acquired:
            self._lock.release()
        return not acquired

    def _is_owned(self):
        return self in getattr(_memory, 'held', ())


class SerializedCalls:
    """Serialize complete invocations without holding a mutex during I/O.

    This preserves existing one-call-at-a-time ports. The condition is held
    only while registering, waiting, or publishing completion. No native
    transaction holder may wait here for another invocation.
    """
    def __init__(self):
        self._condition = threading.Condition()
        self._owner = None
        self._tickets = deque()

    def acquire(self, blocking=True, timeout=-1):
        deadline = None if timeout < 0 else time.monotonic() + timeout
        with self._condition:
            if not blocking and (self._owner is not None or self._tickets):
                return False
            ticket = object()
            self._tickets.append(ticket)
            try:
                while self._owner is not None or self._tickets[0] is not ticket:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return False
                    self._condition.wait(remaining)
                self._owner = threading.get_ident()
                return True
            finally:
                self._tickets.remove(ticket)

    def release(self):
        with self._condition:
            if self._owner != threading.get_ident():
                raise RuntimeError('serialized_call_not_owned')
            self._owner = None
            self._condition.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


class AdmissionCancelled(RuntimeError):
    known_not_written = True


class AdmissionTimeout(TimeoutError):
    known_not_written = True


class WriteAdmission:
    """Atomic queued/BEGIN boundary for one control-save attempt.

    Business deadlines and native real-time waits have separate clocks. The
    state lock never protects SQL, a coordinator wait, or a control callback.
    """
    def __init__(self, deadline, clock=time.monotonic):
        self.deadline, self.clock = deadline, clock
        self.real_deadline = time.monotonic() + max(0, deadline - clock())
        self._lock = threading.Lock()
        self._state = 'queued'

    def is_set(self):
        with self._lock:
            return self._state == 'cancelled' or self.clock() >= self.deadline

    def cancel_before_begin(self):
        with self._lock:
            if self._state in ('queued', 'cancelled'):
                self._state = 'cancelled'
                return True
            return False

    def start_begin(self):
        with self._lock:
            if self._state == 'cancelled' or self.clock() >= self.deadline:
                self._state = 'cancelled'
                raise AdmissionCancelled('lp_control_cancelled_before_begin')
            self._state = 'begin'

    def begin_failed(self):
        with self._lock:
            if self._state == 'begin':
                self._state = 'queued'


def write_priority(operation):
    if operation == 'lp_save_current_control':
        return 'control'
    if ('claim' in operation or 'recover' in operation or operation in {'lp_save_current_settings', 'lp_bind_instance', 'lp_register_order_provenance'}
            or operation.startswith(('lp_finish_preparation', 'lp_finish_attention', 'lp_finalize_attention'))):
        return 'reliable'
    return 'background'


LP_WRITE_OPERATIONS = frozenset({
    'lp_bind_instance',
    'lp_register_order_provenance', 'lp_save_holding_sell',
    'consume_lp_preview', 'lp_claim_manual_command', 'lp_finish_manual_command',
    'lp_record_book_samples', 'lp_save_price_history_batch', 'lp_record_market_exclusion',
    'lp_prune_market_exclusions', 'lp_clear_ended_market_exclusions', '_ensure_lp_metadata_cache_schema',
    'lp_metadata_cache_store_entries', 'lp_metadata_cache_prune', 'lp_save_screening_snapshot',
    'lp_normalize_interrupted_preparation_items', 'lp_migrate_legacy_preparation',
    'lp_claim_preparation_item_alerts', 'lp_finish_preparation_item_alerts', 'lp_wake_preparation_retries',
    'lp_record_preparation_failure', 'lp_claim_preparation_retries', 'lp_clear_preparation_items',
    'lp_recover_preparation_items', 'lp_save_preparation', 'lp_claim_preparation_alert',
    'lp_finish_preparation_alert', 'save_lp_observation', 'update_lp_observation',
    'lp_competitiveness_upsert', 'lp_save_auto_daily_report', 'lp_save_daily_report',
    'lp_create_session', 'lp_update_session', 'lp_register_exchange_orders',
    'lp_merge_queue_protection', 'lp_merge_queue_protection_bucket', 'lp_mark_queue_protection_notified',
    'lp_arm_queue_protection_notification', 'lp_create_first_seen_episode', 'lp_update_first_seen_episode',
    'lp_register_trade_change', 'lp_advance_trade_generation', 'lp_recover_interrupted_submissions',
    'lp_publish_account_financial_facts', 'lp_publish_facts', 'lp_register_fenced_actions',
    'lp_claim_attention_notification', 'lp_finalize_attention_notification_batch',
    'lp_finish_attention_notification', 'lp_save_current_settings', 'lp_save_current_control',
    'lp_save_observed_pool_roles',
    'lp_log_business_record', 'lp_upsert_action', 'lp_finish_cancel', 'lp_reject_pending_cancel',
})


class LPWriteCoordinator:
    """One owner for each resolved database path, retained by live clients."""

    _registry = weakref.WeakValueDictionary()
    _registry_lock = threading.Lock()

    def __new__(cls, path):
        identity = str(Path(path).resolve())
        with cls._registry_lock:
            owner = cls._registry.get(identity)
            if owner is None:
                owner = super().__new__(cls)
                owner.path = Path(identity)
                owner._condition = threading.Condition()
                owner._pending = deque()
                owner._holder = None
                owner._holder_thread = None
                owner._next_ticket = 0
                owner._completed = 0
                owner._wait_seconds = 0.0
                owner._hold_seconds = 0.0
                owner._cancelled = owner._timed_out = 0
                owner._lock_order_violations = 0
                owner._local = threading.local()
                owner._transactions = deque(maxlen=64)
                owner._checkpoint = None
                owner._begin_retries = 0
                owner._next_chain = 0
                owner._admissions = deque(maxlen=64)
                owner._strict_lock_order = False
                cls._registry[identity] = owner
            return owner

    @contextmanager
    def acquire(self, priority='background', *, deadline=None, cancellation=None, retry_chain=None):
        ranks = {'control': 0, 'safety': 0, 'reliable': 1, 'background': 2}
        if priority not in ranks:
            raise ValueError('lp_write_priority_invalid')
        started = time.monotonic()
        deadline = started + 15 if deadline is None else deadline
        if not math.isfinite(deadline):
            raise ValueError('lp_write_deadline_invalid')
        wal_bytes = self.wal_bytes()
        try:
            with self._condition:
                if getattr(_memory, 'held', ()):
                    self._lock_order_violations += 1
                    if self._strict_lock_order:
                        raise RuntimeError('lp_write_lock_order_violation')
                if self._holder_thread == threading.get_ident():
                    raise RuntimeError('nested_lp_write_connection')
                enqueued_at = time.monotonic()
                ticket = (ranks[priority], self._next_ticket, enqueued_at)
                self._next_ticket += 1
                self._pending.append(ticket)
                try:
                    while True:
                        if cancellation is not None and cancellation.is_set():
                            raise AdmissionCancelled('lp_write_cancelled_before_begin')
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AdmissionTimeout('lp_write_admission_deadline')
                        if self._holder is None and min(self._pending) == ticket:
                            break
                        self._condition.wait(min(remaining, .025) if cancellation is not None else remaining)
                finally:
                    self._pending.remove(ticket)
                    self._condition.notify_all()
                self._holder = ticket
                self._holder_thread = threading.get_ident()
                acquired = time.monotonic()
                self._wait_seconds += acquired - started
                self._local.wait_seconds = acquired - started
                self._local.ticket_enqueued_at = enqueued_at
                self._local.wal_bytes = wal_bytes
                self._local.priority = priority
                if retry_chain is not None and retry_chain['attempt']:
                    retry_chain['retry_wait_seconds'] += acquired - started
        except (AdmissionTimeout, AdmissionCancelled) as error:
            if retry_chain is not None and retry_chain['attempt']:
                retry_chain['retry_wait_seconds'] += time.monotonic() - started
            self.admission_failed(error, priority=priority, started=started,
                enqueued_at=enqueued_at, retry_chain=retry_chain)
            raise
        try:
            if acquired - started >= SLOW_WRITE_SECONDS:
                self._record_admission('slow_wait', priority, enqueued_at, acquired-started, 0.0, retry_chain=retry_chain)
            yield self
        finally:
            held = time.monotonic() - acquired
            with self._condition:
                self._hold_seconds += held
                self._completed += 1
                self._holder = self._holder_thread = None
                self._condition.notify_all()
            self._record_admission('slow_hold' if held >= SLOW_WRITE_SECONDS else 'complete',
                priority, enqueued_at, acquired-started, held, retry_chain=retry_chain)

    def wal_bytes(self):
        # File sampling is always outside admission and its queue mutex.
        try:
            return self.path.with_name(self.path.name + '-wal').stat().st_size
        except FileNotFoundError:
            return 0
        except OSError:
            return None

    def _record_admission(self, event, priority, started, wait_seconds, hold_seconds, *, retry_chain=None):
        row = dict(event=event, priority=priority, ticket_enqueued_at=started,
            wait_seconds=wait_seconds, hold_seconds=hold_seconds, thread=threading.get_ident(),
            chain_id=retry_chain['chain_id'] if retry_chain is not None else None,
            begin_attempts=retry_chain['begin_attempts'] if retry_chain is not None else 0,
            retry_wait_seconds=retry_chain['retry_wait_seconds'] if retry_chain is not None else 0.0)
        with self._condition:
            self._admissions.append(row)
        if event != 'complete':
            try:
                logger.warning('lp_write_admission event=%s priority=%s ticket_enqueued_at=%.6f wait_seconds=%.3f hold_seconds=%.3f thread=%s chain_id=%s begin_attempts=%d retry_wait_seconds=%.3f',
                    event, priority, started, wait_seconds, hold_seconds, row['thread'],
                    row['chain_id'], row['begin_attempts'], row['retry_wait_seconds'])
            except Exception:
                pass  # Observability cannot change a storage outcome.

    def admission_failed(self, error, *, priority, started, enqueued_at=None, retry_chain=None):
        if getattr(error, '_lp_admission_logged', False):
            return
        error._lp_admission_logged = True
        cancelled = isinstance(error, AdmissionCancelled)
        with self._condition:
            if cancelled:
                self._cancelled += 1
            else:
                self._timed_out += 1
        self._record_admission('cancelled' if cancelled else 'timeout', priority,
            enqueued_at if enqueued_at is not None else getattr(self._local, 'ticket_enqueued_at', started),
            max(0.0, time.monotonic()-started), 0.0, retry_chain=retry_chain)

    def new_retry_chain(self, deadline):
        with self._condition:
            identity = self._next_chain
            self._next_chain += 1
        return dict(chain_id=identity, deadline=deadline, attempt=0, body_entered=False,
            begin_attempts=0, retry_wait_seconds=0.0)

    @contextmanager
    def enforce_lock_order(self):
        """Test opt-in: production counts violations without changing outcomes."""
        with self._condition:
            previous = self._strict_lock_order
            self._strict_lock_order = True
        try:
            yield
        finally:
            with self._condition:
                self._strict_lock_order = previous

    @contextmanager
    def scope(self, priority='background', *, deadline=None):
        """Reserve admission before a caller's short memory publication lock.

        Each actual Store action retains its own commit/rollback boundary.
        Borrowing this reservation never opens or nests a second transaction.
        """
        if self.in_scope:
            yield self
            return
        with self.acquire(priority, deadline=deadline, retry_chain=self.write_attempt):
            self._local.scope = True
            try:
                yield self
            finally:
                self._local.scope = False

    def run_write(self, operation, priority='background'):
        """Re-enter a short publication closure only before any native body.

        The closure owns its guards. Unwinding it closes a failed BEGIN's
        connection and exits every guard before releasing admission and taking
        a new ticket. An entered body, including a committed prefix, forbids
        replay of the closure.
        """
        if self.in_scope:
            return operation()
        deadline = time.monotonic() + 15
        previous = getattr(self._local, 'write_attempt', None)
        state = self.new_retry_chain(deadline)
        self._local.write_attempt = state
        try:
            for attempt in range(3):
                state['attempt'] = attempt
                try:
                    with self.scope(priority, deadline=deadline):
                        if attempt and deadline - time.monotonic() < 5:
                            raise AdmissionTimeout('lp_write_retry_budget_exhausted')
                        return operation()
                except (AdmissionTimeout, AdmissionCancelled) as error:
                    self.admission_failed(error, priority=priority, started=deadline-15, retry_chain=state)
                    raise
                except sqlite3.Error as error:
                    if (state['body_entered'] or attempt >= 2
                            or not getattr(error, '_lp_begin_not_started', False)
                            or getattr(error, 'sqlite_errorcode', None) != sqlite3.SQLITE_BUSY
                            or deadline - time.monotonic() < 5):
                        raise
                    self.record_begin_retry()
        finally:
            self._local.write_attempt = previous

    @property
    def write_attempt(self):
        return getattr(self._local, 'write_attempt', None)

    @property
    def in_scope(self):
        return getattr(self._local, 'scope', False)

    @contextmanager
    def native_transaction(self):
        if getattr(self._local, 'native', False):
            raise RuntimeError('nested_lp_write_connection')
        self._local.native = True
        try:
            yield
        finally:
            self._local.native = False

    def record_transaction(self, operation, *, retry_chain=None, **metrics):
        metrics.update(operation=operation, permit_wait_seconds=float(getattr(self._local, 'wait_seconds', 0)),
            wal_bytes_before=getattr(self._local, 'wal_bytes', None),
            priority=getattr(self._local, 'priority', 'unknown'), thread=threading.get_ident(),
            ticket_enqueued_at=getattr(self._local, 'ticket_enqueued_at', None),
            chain_id=retry_chain['chain_id'] if retry_chain is not None else None,
            begin_attempts=retry_chain['begin_attempts'] if retry_chain is not None else 0,
            retry_wait_seconds=retry_chain['retry_wait_seconds'] if retry_chain is not None else 0.0)
        with self._condition:
            self._transactions.append(metrics)

    def record_begin_retry(self):
        with self._condition:
            self._begin_retries += 1

    def record_read_stage(self, operation, *, phase, seconds, cpu_seconds, stages):
        """Scans/hydration outside admission must not disappear from telemetry."""
        row = dict(operation=operation, phase=phase, failed=False, wall_seconds=seconds, thread=threading.get_ident(),
            thread_cpu_seconds=cpu_seconds, begin_seconds=0.0, body_seconds=0.0,
            guard_wait_seconds=0.0, commit_seconds=0.0, after_commit_seconds=0.0,
            permit_wait_seconds=0.0, wal_bytes_before=None, priority='read_only', stages=dict(stages))
        with self._condition:
            self._transactions.append(row)

    def record_checkpoint(self, result, *, seconds, page_size, autocheckpoint, wal_bytes_before=None, wal_bytes_after=None):
        with self._condition:
            self._checkpoint = dict(busy=result[0], log_frames=result[1], checkpointed_frames=result[2],
                seconds=seconds, page_size=page_size, wal_autocheckpoint_pages=autocheckpoint,
                source='existing_startup_truncate', wal_bytes_before=wal_bytes_before, wal_bytes_after=wal_bytes_after,
                wal_bytes_delta=wal_bytes_after-wal_bytes_before if wal_bytes_before is not None and wal_bytes_after is not None else None)

    def status(self):
        with self._condition:
            oldest = min((ticket[2] for ticket in self._pending), default=None)
            return dict(oldest_ticket_enqueued_at=oldest,
                        oldest_ticket_age_seconds=max(0.0, time.monotonic()-oldest) if oldest is not None else 0.0,
                        admissions=[dict(row) for row in self._admissions], waiting=len(self._pending), holder=self._holder is not None,
                        completed=self._completed, permit_wait_seconds=self._wait_seconds,
                        permit_hold_seconds=self._hold_seconds,
                        cancelled_before_begin=self._cancelled, admission_timeouts=self._timed_out,
                        lock_order_violations=self._lock_order_violations, begin_retries=self._begin_retries,
                        transactions=[{**row, 'stages': dict(row['stages'])} for row in self._transactions],
                        checkpoint=dict(self._checkpoint) if self._checkpoint is not None else None)
