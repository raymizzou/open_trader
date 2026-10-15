"""Same-source observational sampling. The legacy round remains authoritative."""
from contextlib import contextmanager
from collections import Counter, deque
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from threading import Condition, Thread, local
from time import monotonic
import logging
import uuid
import hashlib
from types import MappingProxyType
import math

from .polymarket_lp_market_reader import CapturedSources, SourceResult, MarketReader, freeze, decimal
from .polymarket_lp_market_calculation import calculate_current, CalculationPolicy

logger = logging.getLogger(__name__)
_local = local()


class CaptureLimit(ValueError):
    pass


def bounded_freeze(value, budget=None):
    """Bound traversal before copying. A rejected frame is never truncated."""
    budget = budget if budget is not None else [0, 0]
    budget[0] += 1
    if budget[0] > 50000:
        raise CaptureLimit('value_nodes')
    if isinstance(value, Mapping):
        return MappingProxyType({bounded_freeze(k, budget): bounded_freeze(v, budget) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(bounded_freeze(v, budget) for v in value)
    if isinstance(value, datetime):
        value = datetime.fromisoformat(value.isoformat())
    if value is not None and type(value) not in (str, int, bool, float, Decimal, datetime):
        raise TypeError('capture_requires_plain_values')
    if type(value) is float and not math.isfinite(value):
        raise TypeError('capture_requires_finite_values')
    size = len(value) if isinstance(value, str) else 32
    if budget[1] + size > 4 * 1024 * 1024:
        raise CaptureLimit('value_bytes')
    budget[1] += len(value.encode('utf-8')) if isinstance(value, str) else size
    if budget[1] > 4 * 1024 * 1024:
        raise CaptureLimit('value_bytes')
    return value


@contextmanager
def comparison_capture(observer):
    previous = getattr(_local, 'batch', None)
    batch = {'entries': [], 'budget': [0, 0], 'failure': None} if observer is not None else None
    _local.batch = batch
    try:
        yield
    finally:
        _local.batch = previous
        if batch:
            try:
                if batch['failure']:
                    observer.note(batch['failure'])
                elif batch['entries']:
                    entries = batch['entries']
                    observer.offer({'round_id': next((e.get('round_id') for e in entries if e.get('round_id')), None), 'entries': entries})
            except Exception:
                pass  # Optional diagnostics cannot change a round's result.


def capture(kind, **values):
    batch = getattr(_local, 'batch', None)
    if batch is not None and not batch['failure']:
        try:
            batch['entries'].append(bounded_freeze({'kind': kind, 'source_id': uuid.uuid4().hex, **values}, batch['budget']))
        except Exception as exc:
            batch['entries'].clear()
            batch['failure'] = 'skipped' if isinstance(exc, CaptureLimit) else 'error'


def _account(entry):
    account, old = entry['account'], entry['old']
    stamp = account.get('checked_at')
    sources = {'balance': SourceResult(entry['source_id'], (account.get('balance'), account.get('allowance')),
            account.get('balance_complete') is True, stamp),
        'open_orders': SourceResult(entry['source_id'], account.get('open_orders'), account.get('open_orders_complete') is True, stamp),
        'positions': SourceResult(entry['source_id'], account.get('positions'), account.get('positions_complete') is True, stamp)}
    frame = MarketReader.from_captured_sources(CapturedSources(account['account_id'], sources))
    result = calculate_current(frame, CalculationPolicy(Decimal(0)), entry['as_of'])
    rows = [('buy_reserved', sum((decimal(r['reserved_usd']) for r in old.get('buys', ())), Decimal(0)), result.buy_reserved),
        ('inventory_cost', decimal(old.get('inventory_cost_usd')), result.inventory_cost),
        ('balance', decimal(old.get('balance_usd')), decimal(account.get('balance'))),
        ('allowance', decimal(old.get('allowance_usd')), decimal(account.get('allowance')))]
    by_id = {r['order_id']: r for r in old.get('buys', ())}
    for row in frame.current_buys:
        primary = by_id.get(row['order_id'], {})
        for name, new in [('quantity', row['remaining']), ('price', row['price']),
                ('original_quantity', decimal(row.get('original_size'))), ('filled_quantity', decimal(row.get('size_matched')))]:
            rows.append(('order.' + row['order_id'] + '.' + name, decimal(primary.get(name)), new))
        for name in ('status', 'side'):
            rows.append(('order.' + row['order_id'] + '.' + name, primary.get(name), row.get(name)))
        rows.append(('order.' + row['order_id'] + '.identity',
            (primary.get('condition_id'), primary.get('token_id')), (row['condition_id'], row['token_id'])))
    return rows


def compare_snapshot(snapshot):
    fields, candidate_inputs = [], {}
    for entry in snapshot['entries']:
        if entry['kind'] == 'account':
            fields.extend((*f, entry) for f in _account(entry))
        elif entry['kind'] == 'field':
            fields.append((entry['field'], entry.get('old'), entry.get('new'), entry))
        elif entry['kind'] in ('yield', 'candidate'):
            if entry['kind'] == 'yield':
                sources = {
                    'open_orders': SourceResult(entry['source_id'], (entry['order_terms'],)),
                    'directions': SourceResult(entry['source_id'], {entry['order_id']: entry.get('direction')}),
                }
                frame = MarketReader.from_captured_sources(CapturedSources(entry['account_id'], sources))
                calculated = calculate_current(frame, CalculationPolicy(Decimal(0)), entry['as_of'])
                new = calculated.yields.get(entry['order_id'])
            else:
                row = {**entry['guidance'], 'direction': entry['direction'], 'existing': False}
                frame = MarketReader.from_captured_sources(CapturedSources('', {
                    'candidates': SourceResult(entry['source_id'], (row,))}))
                calculated = calculate_current(frame, CalculationPolicy(Decimal(0)), entry['as_of'])
                new = calculated.candidate_yields.get(entry['token_id'])
                candidate_inputs[entry['token_id']] = entry
            fields.append(('yield.' + entry.get('order_id', entry.get('token_id', '')),
                decimal(entry.get('old')), new, entry))
        elif entry['kind'] == 'ranking':
            candidates, times = [], set()
            for row in entry['rows']:
                pair = candidate_inputs.get(row['token_id'])
                if (not pair or row.get('order_id') or row.get('intent_id')
                        or pair['as_of'] != row['minimum_order_estimate'].get('checked_at')
                        or decimal(pair['guidance'].get('price')) != decimal(row.get('price'))
                        or decimal(pair['guidance'].get('quantity')) != decimal(row.get('quantity'))):
                    break
                candidates.append({**pair['guidance'], 'direction': pair['direction'], 'existing': False, 'evaluation_at': pair['as_of']})
                times.add(pair['as_of'])
            else:
                if not entry.get('retained'):
                    # Per-candidate business times stay in the frame, including
                    # candidates acquired at different points in a real round.
                    as_of = max(times) if times else None
                    entry = {**entry, 'as_of': as_of, 'source_times': {
                        'evaluation_first_at': min(times) if times else None, 'evaluation_last_at': as_of}}
                    controls = entry['controls']
                    frame = MarketReader.from_captured_sources(CapturedSources('', {
                        'candidates': SourceResult(entry['source_id'], candidates)}))
                    calculated = calculate_current(frame, CalculationPolicy(Decimal(0), controls['slots'],
                        selection_wallet=controls['available'], selection_capacity=controls['capacity']), as_of)
                    fields.append(('targets', tuple(entry['old']), calculated.target_tokens, entry))
            if not any(f[0] == 'targets' for f in fields):
                fields.append(('targets', entry['old'], None, entry))
    result = []
    if not fields:
        fields.append(('coverage', None, None, {}))
    for field, old, new, entry in fields:
        oid = entry.get('order_id', '')
        if field.startswith('order.'):
            _, oid, field = field.split('.', 2)
        elif field.startswith('yield.'):
            _, oid = field.split('.', 1)
            field = 'yield'
        reason = None
        if not snapshot.get('round_id'):
            reason = 'round_missing'
        elif not entry.get('source_id'):
            reason = 'source_missing'
        elif entry.get('kind') == 'field':
            if not entry.get('order_id') or not entry.get('account_id'):
                reason = 'identity_missing'
            elif entry.get('source_id') != entry.get('old_source_id'):
                reason = 'source_different'
            elif not entry.get('as_of') or entry.get('as_of') != entry.get('old_as_of'):
                reason = 'evaluation_time_missing_or_different'
        if reason is None and (old is None or new is None):
            reason = 'control_or_value_unknown' if field == 'targets' else 'value_unknown'
        result.append(dict(field=field, old=old, new=new,
            delta=new-old if isinstance(old, Decimal) and isinstance(new, Decimal) else None,
            source_id=entry.get('source_id'), as_of=entry.get('as_of'),
            source_times=_source_times(entry), order_hash=hashlib.sha256(str(oid).encode()).hexdigest(),
            status='not_comparable' if reason else 'match' if old == new else 'mismatch', reason=reason, basis='raw_decimal'))
    return result


def _safe_identity(value):
    if value is None:
        return None
    text = str(value)
    if len(text) <= 64 and text.replace('-', '').replace('_', '').isalnum() and not text.startswith('0x'):
        return text
    return hashlib.sha256(text.encode()).hexdigest()


def _safe_value(value):
    if value is None or type(value) in (Decimal, int, float, bool):
        return value
    if isinstance(value, (tuple, list)):
        return tuple(hashlib.sha256(str(v).encode()).hexdigest() for v in value[:12])
    if isinstance(value, str) and value in {'BUY', 'SELL', 'LIVE', 'OPEN', 'CANCELED', 'FILLED', 'MATCHED', 'ACCEPTED', 'PARTIALLY_FILLED'}:
        return value
    return 'redacted'


def _safe_time(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.isoformat() if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _source_times(entry):
    if entry.get('source_times'):
        return entry['source_times']
    account = entry.get('account') or {}
    direction = entry.get('direction') or {}
    return {**{k: account.get(k) for k in ('read_started_at', 'checked_at', 'read_ended_at')},
        'book_received_at': (direction.get('book') or {}).get('received_at'),
        'reward_checked_at': direction.get('reward_checked_at')}


def _safe_field(field):
    allowed = {'buy_reserved', 'inventory_cost', 'quantity', 'price', 'yield', 'targets', 'coverage',
        'comparison', 'identity', 'status', 'side', 'remaining', 'original_quantity', 'filled_quantity', 'balance', 'allowance'}
    return {**{k: field.get(k) for k in ('status', 'reason', 'order_hash', 'basis')},
        'field': field['field'] if field['field'] in allowed else 'value',
        'old': _safe_value(field.get('old')), 'new': _safe_value(field.get('new')),
        'delta': _safe_value(field.get('delta')), 'source_id': _safe_identity(field.get('source_id')),
        'as_of': _safe_time(field.get('as_of')),
        'source_times': {k: _safe_time(v) for k,v in (field.get('source_times') or {}).items()
            if k in {'read_started_at', 'checked_at', 'read_ended_at', 'book_received_at', 'reward_checked_at', 'evaluation_first_at', 'evaluation_last_at'}}}


class ReaderComparisonObserver:
    def __init__(self, *, emit=None, compare=compare_snapshot, clock=monotonic):
        self._emit = emit or self._log
        self._compare = compare
        self._clock = clock
        self._last_summary = None
        self._condition = Condition()
        self._queue = deque()
        self._counts = Counter()
        self._coverage = Counter()
        self._running = False
        self._thread = None
        self._closed = False
        self._last_mismatch = None

    @staticmethod
    def _log(record):
        from .polymarket_trading import _lp_capture_read_log
        _lp_capture_read_log(lambda: logger.info('lp_reader_comparison facts=%s', record))

    def offer(self, snapshot):
        with self._condition:
            if self._closed:
                self._counts['skipped'] += 1
                return False
        try:
            value = bounded_freeze(snapshot)
        except Exception as exc:
            self.note('skipped' if isinstance(exc, CaptureLimit) else 'error')
            return False
        with self._condition:
            if self._closed:
                self._counts['skipped'] += 1
                return False
            if self._queue:
                self._queue.clear()
                self._counts['skipped'] += 1
            self._queue.append(value)
            if self._thread is None:
                self._thread = Thread(target=self._run, daemon=True, name='lp-reader-comparison')
                self._thread.start()
            self._condition.notify_all()
        return True

    def _run(self):
        while True:
            with self._condition:
                if not self._queue:
                    self._thread = None
                    self._condition.notify_all()
                    return
                value = self._queue.popleft()
                self._running = True
            try:
                fields = self._compare(value)
            except Exception:
                fields = [{'field': 'comparison', 'status': 'error', 'reason': 'comparison_failed',
                    'order_hash': '', 'old': None, 'new': None}]

            with self._condition:
                self._counts.update(f['status'] for f in fields)
                self._coverage.update(f['field'] for f in fields if f['status'] in {'match', 'mismatch'})
            now = self._clock()
            fingerprint = tuple((f['field'], f['order_hash'], str(f['old']), str(f['new']))
                for f in fields if f['status'] == 'mismatch')
            difference = bool(fingerprint and fingerprint != self._last_mismatch)
            summary = self._last_summary is None or now - self._last_summary >= 60
            visible = [_safe_field(f) for f in sorted(fields, key=lambda f: f['field'] not in {'targets', 'yield'})[:12]]
            records = []
            common = {'round_id': _safe_identity(value.get('round_id')),
                'compared': sum(f['status'] in {'match', 'mismatch'} for f in fields)}
            if difference:
                mismatches = [f for f in fields if f['status'] == 'mismatch']
                records.append({**common, 'event': 'difference',
                    'fields': [_safe_field(f) for f in mismatches[:12]],
                    'omitted_differences': max(0, len(mismatches) - 12)})
            if summary:
                records.append({**common, 'event': 'summary', 'counts': self._totals(), 'coverage': dict(self._coverage),
                    'fields': [] if difference else visible})
                self._last_summary = now
            for record in records:
                try:
                    self._emit(record)
                except Exception:
                    self.note('error')
            self._last_mismatch = fingerprint
            with self._condition:
                self._running = False
                self._condition.notify_all()

    def wait_idle(self, *, timeout):
        with self._condition:
            return self._condition.wait_for(lambda: not self._running and not self._queue and self._thread is None, timeout)

    def _totals(self):
        with self._condition:
            return {k: self._counts[k] for k in ('match', 'mismatch', 'not_comparable', 'skipped', 'error')}

    def note(self, status):
        with self._condition:
            self._counts[status] += 1

    def snapshot(self):
        with self._condition:
            return {**{k: self._counts[k] for k in ('match', 'mismatch', 'not_comparable', 'skipped', 'error')},
                'running': int(self._running), 'pending': len(self._queue), 'coverage': dict(self._coverage)}

    def close(self):
        with self._condition:
            self._closed = True
            self._counts['skipped'] += len(self._queue)
            self._queue.clear()
            self._condition.notify_all()
