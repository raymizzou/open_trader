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


@dataclass(frozen=True)
class ReadScope:
    account: str
    components: tuple[str, ...] = ('balance', 'open_orders', 'positions')
    related_markets: bool = False


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

    @property
    def current_buys(self):
        return tuple(o for o in self.orders if o['side'] == 'BUY' and o['status'] in CURRENT_ORDER_STATES and o['remaining'] > 0)


class MarketReader:
    def __init__(self, source_port, *, clock=time.monotonic):
        self._port = source_port
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs = {}
        self._retry = {}
        self._closed = False

    @staticmethod
    def from_captured_sources(captured: CapturedSources) -> MarketFrame:
        sources = {name: replace(s, value=freeze(s.value), error_facts=freeze(s.error_facts)) for name, s in captured.sources.items()}
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
        def run():
            source_id = uuid.uuid4().hex
            try:
                value = self._port.read(key[1], scope)
                result = SourceResult(source_id, freeze(value), checked_at=datetime.now(UTC))
            except BaseException as exc:
                from .polymarket_trading import _safe_history_response_facts
                facts = _safe_history_response_facts(exc)
                facts.update(getattr(exc, 'response_facts', {}) or {})
                seconds = facts.get('retry_after_seconds', getattr(exc, 'retry_after_seconds', None))
                if seconds is None and isinstance(facts.get('retry_after_at'), datetime):
                    seconds = max(0, (facts['retry_after_at'] - datetime.now(UTC)).total_seconds())
                if seconds is None: seconds = 60
                if not isinstance(seconds, (int, float)) or not 0 <= seconds < float('inf'):
                    seconds = 60
                result = SourceResult(source_id, complete=False, error=type(exc).__name__,
                    retry_at=self._clock() + seconds, failed_at=datetime.now(UTC), error_facts=freeze(facts))
                with self._lock:
                    self._retry[key] = result
            future.set_result(result)
        threading.Thread(target=run, daemon=True, name='lp-reader-source').start()
        return future

    def read_current(self, scope: ReadScope, deadline: float) -> MarketFrame:
        sources, own, waiting = {}, {}, list(scope.components)
        # No queued native work: at most two live tasks, including timed-out calls.
        while waiting or own:
            with self._lock:
                for name in tuple(waiting):
                    key = (scope.account, name)
                    existing = self._jobs.get(key)
                    if existing is not None and existing.done():
                        self._jobs.pop(key)
                        existing = None
                    retry = self._retry.get(key)
                    if self._closed:
                        sources[name] = SourceResult('', complete=False, error='closed')
                    elif existing is not None:
                        sources[name] = SourceResult('', complete=False, error='in_progress')
                    elif retry and self._clock() < retry.retry_at:
                        sources[name] = retry
                    elif time.monotonic() >= deadline:
                        sources[name] = SourceResult('', complete=False, error='timeout')
                    elif sum(not f.done() for f in self._jobs.values()) < 2:
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
        frame = self.from_captured_sources(CapturedSources(scope.account, sources))
        if scope.related_markets:
            identities = sorted({(r['condition_id'], r['token_id']) for r in (*frame.orders, *frame.positions)})
            if identities:
                market_scope = ReadScope(scope.account, tuple('market:' + c + ':' + t for c, t in identities))
                markets = self.read_current(market_scope, deadline)
                frame = self.from_captured_sources(CapturedSources(scope.account, {**sources, **markets.sources}))
        return frame

    def drain(self, *, timeout=0) -> bool:
        with self._lock:
            jobs = tuple(self._jobs.values())
        return not jobs or not wait(jobs, timeout=timeout).not_done

    def close(self, *, timeout=0) -> bool:
        with self._lock:
            self._closed = True
        return self.drain(timeout=timeout)
