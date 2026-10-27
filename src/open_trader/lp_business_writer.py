"""Disposable business records. Admission and persistence grant no authority."""
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
import json
import logging
import threading


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HoldingInventoryRecord:
    """Best-effort restart guard; never a mutation admission or send intent."""
    payload: str

    def __post_init__(self):
        row = json.loads(self.payload)
        if row.get('kind') != 'inventory' or row.get('state') != 'terminal':
            raise ValueError('holding_inventory_record_required')


@dataclass(frozen=True)
class RequestRecord:
    account_id: str
    process_epoch: str
    request_id: str
    session_id: str
    condition_id: str
    token_id: str
    kind: str
    price: Decimal
    quantity: Decimal
    requested_at: datetime
    order_id: str | None = None
    venue: str | None = None
    cycle_generation: str | None = None
    batch_id: str | None = None
    round_number: int | None = None
    observation_id: str | None = None
    order_type: str | None = None
    expiration: int | None = None
    post_only: bool | None = None
    send_options: tuple = ()

    def __post_init__(self):
        for value in (self.account_id, self.process_epoch, self.request_id, self.session_id,
                      self.condition_id, self.token_id):
            if type(value) is not str or not value.strip():
                raise ValueError('record_identity_required')
        sealed = (self.venue, self.cycle_generation, self.batch_id, self.observation_id)
        if any(v is not None for v in sealed) and (
                any(type(v) is not str or not v for v in sealed)
                or type(self.round_number) is not int or self.round_number < 0):
            raise ValueError('sealed_record_identity_invalid')
        if (not isinstance(self.send_options, tuple)
                or any(not isinstance(item, tuple) or len(item) != 2
                    or type(item[0]) is not str
                    or not isinstance(item[1], (str, bool, int, Decimal, type(None)))
                    for item in self.send_options)):
            raise ValueError('sealed_record_options_invalid')
        if (self.order_type is not None and (type(self.order_type) is not str or not self.order_type)
                or self.expiration is not None and (type(self.expiration) is not int or self.expiration < 0)
                or self.post_only is not None and type(self.post_only) is not bool):
            raise ValueError('sealed_record_terms_invalid')
        if self.kind not in ('BUY', 'SELL', 'CANCEL'):
            raise ValueError('record_kind_invalid')
        if self.order_id is not None and (type(self.order_id) is not str or not self.order_id.strip()):
            raise ValueError('record_order_identity_invalid')
        if self.kind == 'CANCEL' and self.order_id is None:
            raise ValueError('cancel_order_identity_required')
        if (not isinstance(self.price, Decimal) or not self.price.is_finite() or not 0 < self.price < 1
                or not isinstance(self.quantity, Decimal) or not self.quantity.is_finite() or self.quantity <= 0):
            raise ValueError('record_terms_invalid')
        if not isinstance(self.requested_at, datetime) or self.requested_at.utcoffset() is None:
            raise ValueError('record_time_required')


@dataclass(frozen=True)
class ReceiptRecord:
    account_id: str
    process_epoch: str
    request_id: str
    session_id: str
    condition_id: str
    token_id: str
    order_id: str | None
    outcome: str
    received_at: datetime
    reason_code: str | None = None

    def __post_init__(self):
        _validate_evidence(self, self.received_at)
        if self.reason_code is not None and (type(self.reason_code) is not str or self.reason_code not in {
                'unsupported_order_shape', 'ambiguous_send_options', 'unsupported_send_option',
                'invalid_builder_code', 'control_or_native_not_current',
                'mutation_not_admitted', 'mutation_lease_not_current',
                'independent_cancel_identity', 'independent_cancel_not_current',
                'independent_cancel_inflight', 'rotation_not_current',
                'protection_cancel_owned', 'protection_cancel_not_current',
                'protection_evidence_not_current', 'native_capacity', 'cancel_rejected'}):
            raise ValueError('record_reason_code_invalid')
        if self.outcome not in ('accepted', 'rejected', 'unknown', 'not_sent'):
            raise ValueError('record_outcome_invalid')


@dataclass(frozen=True)
class ProgressRecord:
    account_id: str
    process_epoch: str
    request_id: str
    session_id: str
    condition_id: str
    token_id: str
    order_id: str
    original_size: Decimal
    size_matched: Decimal
    price: Decimal
    status: str
    checked_at: datetime

    def __post_init__(self):
        _validate_evidence(self, self.checked_at)
        if (not isinstance(self.price, Decimal) or not self.price.is_finite() or not 0 < self.price < 1
                or not isinstance(self.original_size, Decimal) or not self.original_size.is_finite()
                or not isinstance(self.size_matched, Decimal) or not self.size_matched.is_finite()
                or not 0 <= self.size_matched <= self.original_size or self.original_size <= 0
                or type(self.status) is not str):
            raise ValueError('record_progress_invalid')


@dataclass(frozen=True)
class CancelTerminalRecord:
    """One terminal fact for exact matching durable cancel request parents."""
    progress: ProgressRecord
    venue: str
    order_type: str
    expiration: int | None

    def __post_init__(self):
        if (type(self.progress) is not ProgressRecord
                or type(self.venue) is not str or not self.venue
                or type(self.order_type) is not str or not self.order_type
                or self.expiration is not None and (type(self.expiration) is not int or self.expiration < 0)):
            raise ValueError('cancel_terminal_record_invalid')


def _validate_evidence(record, timestamp):
    for value in (record.account_id, record.process_epoch, record.request_id, record.session_id,
                  record.condition_id, record.token_id):
        if type(value) is not str or not value.strip():
            raise ValueError('record_identity_required')
    if record.order_id is not None and (type(record.order_id) is not str or not record.order_id.strip()):
        raise ValueError('record_order_identity_invalid')
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
        raise ValueError('record_time_required')


def _encode_scalar(value):
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, datetime): return value.isoformat()
    raise TypeError('record_requires_values')


class LPBusinessWriter:
    def __init__(self, store, *, policy=None):
        self._policy = dict(max_entries=3000, max_encoded_bytes=16 * 1024 * 1024)
        self._policy.update(policy or {})
        if (set(self._policy) != {'max_entries', 'max_encoded_bytes'}
                or any(type(v) is not int or v < 1 for v in self._policy.values())):
            raise ValueError('business_writer_policy_invalid')
        self._store = store
        self._condition = threading.Condition()
        self._pending = deque()
        self._pending_bytes = self._inflight_bytes = self._inflight = 0
        self._closed = False
        self._counts = dict(saved_entries=0, dropped_overflow=0, dropped_oversize=0,
                            dropped_inflight_capacity=0, failed_entries=0, unknown_outcome_entries=0,
                            dropped_shutdown=0, dropped_closed=0, dropped_parent=0)
        self._thread = threading.Thread(target=self._run, name='lp-business-writer', daemon=True)
        self._thread.start()

    def offer(self, record):
        if type(record) not in (RequestRecord, ReceiptRecord, ProgressRecord, CancelTerminalRecord, HoldingInventoryRecord):
            raise TypeError('closed_business_record_required')
        encoded = json.dumps(asdict(record), ensure_ascii=False, separators=(',', ':'), default=_encode_scalar)
        size = len(encoded.encode('utf-8'))
        with self._condition:
            if self._closed:
                self._counts['dropped_closed'] += 1
                return
            if size > self._policy['max_encoded_bytes']:
                self._counts['dropped_oversize'] += 1
                return
            def full():
                return (len(self._pending) + self._inflight + 1 > self._policy['max_entries']
                        or self._pending_bytes + self._inflight_bytes + size > self._policy['max_encoded_bytes'])
            while self._pending and full():
                _, removed_size = self._pending.popleft()
                self._pending_bytes -= removed_size
                self._counts['dropped_overflow'] += 1
            if full():
                self._counts['dropped_inflight_capacity'] += 1
                return
            self._pending.append((record, size))
            self._pending_bytes += size
            self._condition.notify()

    def status(self):
        with self._condition:
            alive = self._thread.is_alive()
            return dict(state=('STOPPING' if alive else 'STOPPED') if self._closed else 'RUNNING',
                thread_alive=alive, pending_entries=len(self._pending), inflight_entries=self._inflight,
                pending_bytes=self._pending_bytes, inflight_bytes=self._inflight_bytes, **self._counts)

    def close(self, timeout=5):
        with self._condition:
            self._closed = True
            self._counts['dropped_shutdown'] += len(self._pending)
            self._pending.clear()
            self._pending_bytes = 0
            self._condition.notify_all()
        self._thread.join(timeout)
        if self._thread.is_alive(): raise TimeoutError('LP business writer is still stopping')

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending)
                if self._closed: return
                record, size = self._pending.popleft()
                self._pending_bytes -= size
                self._inflight, self._inflight_bytes = 1, size
            failed = False
            saved = False
            try:
                if type(record) is HoldingInventoryRecord:
                    self._store.lp_save_holding_sell(json.loads(record.payload))
                    saved = True
                else:
                    saved = self._store.lp_log_business_record(record)
            except Exception as exc:
                failed = True
                # A raised transaction can have committed. Never replay it.
                try:
                    logger.warning('lp_business_save_outcome_unknown error_type=%s', type(exc).__name__)
                except Exception:
                    pass
            finally:
                record = None
                with self._condition:
                    if failed:
                        self._counts['failed_entries'] += 1
                        self._counts['unknown_outcome_entries'] += 1
                    elif saved:
                        self._counts['saved_entries'] += 1
                    else:
                        self._counts['dropped_parent'] += 1
                    self._inflight = self._inflight_bytes = 0
