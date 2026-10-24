"""Immutable current facts. Independent acquisition is opt-in, never a live loop."""
from concurrent.futures import Future, wait, FIRST_COMPLETED
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from collections.abc import Mapping
import time
import uuid
from decimal import Decimal, InvalidOperation
from .polymarket_lp_risk import TERMINAL_ORDER_STATES

FINANCIAL_COMPONENTS = frozenset({'balance', 'open_orders', 'positions'})

CURRENT_ORDER_STATES = frozenset({'LIVE', 'OPEN', 'ACCEPTED', 'PARTIALLY_FILLED', 'CANCELING', 'CANCELLING', 'PENDING_CANCEL'})


def freeze(value):
    """Copy values only; never retain an SDK object or an owning service."""
    if value is None or isinstance(value, (str, int, float, bool, datetime)):
        return value
    from decimal import Decimal
    if isinstance(value, Decimal):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    raise TypeError('reader_requires_values')


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def observed_source_error(source):
    """Only native error codes and captured exception types are publishable."""
    if source is None or not source.error:
        return None
    if source.error in ('capacity', 'timeout', 'in_progress', 'closed', 'provider_error'):
        return source.error
    exception_type = source.diagnostics.get('exception_type') if isinstance(source.diagnostics, Mapping) else None
    return exception_type[:64] if isinstance(exception_type, str) else 'provider_error'


def _freeze_source_diagnostics(value):
    """Diagnostic metadata must not invalidate captured trading facts."""
    try:
        return freeze(value) if isinstance(value, Mapping) else MappingProxyType({})
    except Exception:
        return MappingProxyType({})


@dataclass(frozen=True)
class ReadScope:
    account: str
    components: tuple[str, ...] = ('balance', 'open_orders', 'positions')
    related_markets: bool = False
    position_only_markets: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceResult:
    source_id: str
    value: object = None
    complete: bool = True
    checked_at: datetime | None = None
    error: str | None = None
    retry_at: float | None = None
    failed_at: datetime | None = None
    error_facts: Mapping = field(default_factory=dict)
    diagnostics: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class CapturedSources:
    account: str
    sources: Mapping[str, SourceResult]


@dataclass(frozen=True)
class MarketFrame:
    account: str
    sources: Mapping[str, SourceResult]
    orders: tuple = ()
    positions: tuple = ()
    orders_complete: bool = False
    positions_complete: bool = False
    buy_reserved: Decimal | None = None
    inventory_cost: Decimal | None = None
    history_fees: Decimal | None = None
    errors: tuple[str, ...] = ()
    reader_diagnostics: Mapping = field(default_factory=dict)
    managed_order_ids: frozenset[str] | None = None

    @property
    def current_buys(self):
        return tuple(o for o in self.orders if o['side'] == 'BUY' and o['status'] in CURRENT_ORDER_STATES and o['remaining'] > 0
            and (self.managed_order_ids is None or o['order_id'] in self.managed_order_ids))


class MarketReader:
    def __init__(self, source_port, *, clock=time.monotonic):
        self._port = source_port
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs = {}
        self._job_started_at = {}
        self._retry = {}
        self._wait_epoch = 0
        self._closed = False
        self._closed_positions = {}  # bounded, current position identity only; never financial authority

    @staticmethod
    def from_captured_sources(captured: CapturedSources) -> MarketFrame:
        sources = {name: replace(s, value=freeze(s.value), error_facts=freeze(s.error_facts),
            diagnostics=_freeze_source_diagnostics(s.diagnostics)) for name, s in captured.sources.items()}
        errors, orders, positions = [], [], []
        order_source = sources.get('open_orders')
        position_source = sources.get('positions')
        orders_complete = bool(order_source and order_source.complete and order_source.value is not None)
        positions_complete = bool(position_source and position_source.complete and position_source.value is not None)
        seen = set()
        for row in (order_source.value if order_source and order_source.value else ()):
            original = decimal(row.get('original_size', row.get('quantity')))
            matched = decimal(row.get('size_matched'))
            price = decimal(row.get('price'))
            identity = row.get('order_id')
            if (not identity or identity in seen or not row.get('condition_id') or not row.get('token_id')
                    or str(row.get('account_id', captured.account)).casefold() != captured.account.casefold()
                    or row.get('side') not in {'BUY', 'SELL'} or row.get('status') not in CURRENT_ORDER_STATES | TERMINAL_ORDER_STATES
                    or original is None or matched is None or price is None
                    or not 0 <= matched <= original or not 0 < price < 1
                    or row.get('status') in {'FILLED', 'MATCHED'} and matched != original):
                orders_complete = False
                errors.append('invalid_order_identity_or_terms')
                continue
            seen.add(identity)
            orders.append(freeze({**row, 'remaining': original - matched, 'price': price}))
        costs = []
        for row in (position_source.value if position_source and position_source.value else ()):
            quantity = decimal(row.get('quantity', row.get('size')))
            if (not row.get('condition_id') or not row.get('token_id') or quantity is None or quantity < 0
                    or str(row.get('account_id', captured.account)).casefold() != captured.account.casefold()):
                positions_complete = False
                errors.append('invalid_position_identity_or_terms')
                continue
            cost = decimal(row.get('cost_usd', row.get('initial_value')))
            if quantity == 0 or (row.get('redeemable') is True and decimal(row.get('current_price')) == 0
                    and decimal(row.get('current_value')) == 0):
                cost = Decimal(0)
            elif cost is None:
                average = decimal(row.get('average_price'))
                if average is not None and 0 <= average <= 1:
                    cost = average * quantity
            if cost is not None and cost < 0:
                cost = None
            positions.append(freeze({**row, 'quantity': quantity, 'cost_usd': cost}))
            costs.append(Decimal(0) if quantity == 0 else cost)
        reserved = (sum((o['remaining'] * o['price'] for o in orders if o['side'] == 'BUY' and o['status'] in CURRENT_ORDER_STATES), Decimal(0))
            if orders_complete else None)
        inventory = (sum(costs, Decimal(0)) if positions_complete and all(c is not None for c in costs) else None)
        return MarketFrame(captured.account, MappingProxyType(sources), tuple(orders), tuple(positions),
            orders_complete, positions_complete, reserved, inventory, errors=tuple(errors))

    def _start(self, key, scope):
        future = Future()
        self._jobs[key] = future
        try:
            for previous in tuple(self._job_started_at):
                job = self._jobs.get(previous)
                if job is None or job.done():
                    self._job_started_at.pop(previous, None)
            self._job_started_at[key] = self._clock()
        except Exception:
            pass
        def run():
            source_id = uuid.uuid4().hex
            try:
                value = self._port.read(key[1], scope)
                result = SourceResult(source_id, freeze(value), checked_at=datetime.now(UTC))
            except BaseException as exc:
                # A response belongs to the reset epoch in which it arrives,
                # even when its native request started before that reset.
                # Capture under the same lock as reset; formatting a response
                # observed before reset must not reinstall its old deadline.
                with self._lock:
                    response_epoch = self._wait_epoch
                    response_at = self._clock()
                    failed_at = datetime.now(UTC)
                from .polymarket_trading import _safe_history_response_facts
                facts = _safe_history_response_facts(exc)
                facts.update(getattr(exc, 'response_facts', {}) or {})
                seconds = facts.get('retry_after_seconds', getattr(exc, 'retry_after_seconds', None))
                if seconds is None and isinstance(facts.get('retry_after_at'), datetime):
                    seconds = max(0, (facts['retry_after_at'] - failed_at).total_seconds())
                if seconds is None: seconds = 60
                if not isinstance(seconds, (int, float)) or not 0 <= seconds < float('inf'):
                    seconds = 60
                result = SourceResult(source_id, complete=False, error=type(exc).__name__,
                    retry_at=response_at + seconds, failed_at=failed_at, error_facts=freeze(facts),
                    diagnostics=freeze(dict(exception_type=type(exc).__name__[:64])))
                with self._lock:
                    if response_epoch == self._wait_epoch:
                        self._retry[key] = result
            future.set_result(result)
        threading.Thread(target=run, daemon=True, name='lp-reader-source').start()
        return future

    def read_current(self, scope: ReadScope, deadline: float) -> MarketFrame:
        sources, own, waiting = {}, {}, list(scope.components)
        # No queued native work: two live tasks, at most one nonfinancial.
        # Timed-out tasks retain both total and class ownership until actual completion.
        while waiting or own:
            with self._lock:
                for name in tuple(waiting):
                    key = (scope.account, name)
                    existing = self._jobs.get(key)
                    if existing is not None and existing.done():
                        self._jobs.pop(key)
                        self._job_started_at.pop(key, None)
                        existing = None
                    retry = self._retry.get(key)
                    if self._closed:
                        sources[name] = SourceResult('', complete=False, error='closed')
                    elif existing is not None:
                        sources[name] = SourceResult('', complete=False, error='in_progress')
                    elif retry and self._clock() < retry.retry_at:
                        sources[name] = retry
                        try:
                            sources[name] = replace(retry, diagnostics=freeze(dict(retry.diagnostics,
                                retry_cache_hit=True,
                                remaining_retry_seconds=max(0, retry.retry_at-self._clock()))))
                        except Exception:
                            pass
                    elif time.monotonic() >= deadline:
                        sources[name] = SourceResult('', complete=False, error='timeout')
                    elif (sum(not f.done() for f in self._jobs.values()) < 2
                            and (name in FINANCIAL_COMPONENTS or not any(
                                not job.done() and other[1] not in FINANCIAL_COMPONENTS
                                for other, job in self._jobs.items()))):
                        own[name] = self._start(key, scope)
                    else:
                        continue
                    waiting.remove(name)
            for name, future in tuple(own.items()):
                if future.done():
                    sources[name] = future.result()
                    own.pop(name)
            if not waiting and not own:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not own:
                for name in waiting + list(own):
                    sources[name] = SourceResult('', complete=False, error='timeout' if name in own else 'capacity')
                break
            wait(tuple(own.values()), timeout=remaining, return_when=FIRST_COMPLETED)
        # Observation cannot change an acquired result or native ownership.
        try:
            sources = {name: replace(s, diagnostics=freeze(dict(
                {'retry_cache_hit': False, 'remaining_retry_seconds': None}, **s.diagnostics,
                source_error=s.error, error_class=(s.error if s.error in
                    ('capacity', 'timeout', 'in_progress', 'closed') else 'exception' if s.error else None),
                http_status=s.error_facts.get('status') if type(s.error_facts.get('status')) is int else None)))
                for name, s in sources.items()}
        except Exception:
            pass
        frame = self.from_captured_sources(CapturedSources(scope.account, sources))
        if scope.related_markets:
            identities = sorted({(r['condition_id'], r['token_id']) for r in (*frame.orders, *frame.positions)})
            if identities:
                components = tuple('market:' + c + ':' + t for c, t in identities)
                order_identities = {(r['condition_id'], r['token_id']) for r in frame.orders}
                eligible = set('market:' + c + ':' + t for c, t in identities
                    if (c, t) not in order_identities) if frame.orders_complete and frame.positions_complete else set()
                with self._lock:
                    # An order, missing coverage or removed position invalidates applicability.
                    self._closed_positions = {key: value for key, value in self._closed_positions.items()
                        if key[0] != scope.account or key[1] in eligible}
                    retained = {name: self._closed_positions[(scope.account, name)] for name in components
                        if (scope.account, name) in self._closed_positions}
                market_scope = ReadScope(scope.account, tuple(n for n in components if n not in retained),
                    position_only_markets=tuple(sorted(eligible)))
                markets = self.read_current(market_scope, deadline)
                with self._lock:
                    for name, source in markets.sources.items():
                        if (name in eligible and source.complete and not source.error
                                and isinstance(source.value, Mapping)
                                and source.value.get('state') == 'non_tradable'):
                            self._closed_positions[(scope.account, name)] = source
                    # A finite resource proof cache, with no extension of source time.
                    while len(self._closed_positions) > 128:
                        self._closed_positions.pop(next(iter(self._closed_positions)))
                frame = self.from_captured_sources(CapturedSources(scope.account, {**sources, **retained, **markets.sources}))
            else:
                with self._lock:
                    self._closed_positions = {key: value for key, value in self._closed_positions.items()
                        if key[0] != scope.account}
        try:
            with self._lock:
                observed = self._clock()
                jobs = tuple(dict(key=key, scope=dict(account=key[0], component=key[1],
                    condition_id=key[1].split(':',2)[1] if key[1].startswith('market:') and key[1].count(':')>=2 else None,
                    token_id=key[1].split(':',2)[2] if key[1].startswith('market:') and key[1].count(':')>=2 else None),
                    age_seconds=max(0, observed-self._job_started_at[key])
                        if key in self._job_started_at else None)
                    for key, future in self._jobs.items() if not future.done())
            ages = {tuple(job['key']): job['age_seconds'] for job in jobs}
            frame = replace(frame, reader_diagnostics=freeze(dict(inflight_count=len(jobs),
                jobs=jobs[:2], observed_monotonic=observed)),
                sources=MappingProxyType({name: replace(s, diagnostics=freeze(dict(s.diagnostics,
                    inflight_age_seconds=ages.get((scope.account, name)))))
                    for name, s in frame.sources.items()}))
        except Exception:
            pass
        return frame

    def reset_provider_wait(self):
        """Discard old provider waits, retaining every actual native job.

        Responses observed before reset cannot reinstall their old deadlines.
        Responses arriving after reset keep new limits, including older jobs.
        """
        with self._lock:
            self._wait_epoch += 1
            self._retry.clear()

    def drain(self, *, timeout=0) -> bool:
        with self._lock:
            jobs = tuple(self._jobs.values())
        return not jobs or not wait(jobs, timeout=timeout).not_done

    def close(self, *, timeout=0) -> bool:
        with self._lock:
            self._closed = True
        return self.drain(timeout=timeout)
