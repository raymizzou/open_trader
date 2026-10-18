"""Reader contracts at the external source boundary; no live SDK calls."""
from datetime import UTC, datetime
from threading import Event, Lock
import time


def test_current_sources_start_in_parallel_without_trade_history(tmp_path):
    from open_trader.polymarket_lp_market_reader import MarketReader, ReadScope
    entered = {name: Event() for name in ('balance', 'open_orders')}
    release = Event()
    lock = Lock()
    counts = {'active': 0, 'max': 0, 'history': 0}

    class Sources:
        def read(self, component, scope):
            with lock:
                counts['active'] += 1
                counts['max'] = max(counts['max'], counts['active'])
            try:
                if component in entered:
                    entered[component].set()
                    assert all(e.wait(2) for e in entered.values())
                    release.set()
                assert release.wait(2)
                return {'balance': ('100', '100'), 'open_orders': (), 'positions': ()}[component]
            finally:
                with lock: counts['active'] -= 1
        def history(self):
            counts['history'] += 1
            raise AssertionError('history must not be read')

    reader = MarketReader(Sources())
    try:
        frame = reader.read_current(ReadScope(account='account-A'), time.monotonic() + 3)
        assert all(e.is_set() for e in entered.values())
        assert counts['max'] == 2 and counts['history'] == 0
        assert frame.account == 'account-A'
        assert set(frame.sources) == {'balance', 'open_orders', 'positions'}
        assert all(s.complete and s.source_id and s.checked_at.tzinfo for s in frame.sources.values())
    finally:
        release.set()
        assert reader.close(timeout=2)

    # Real adapter port: market identity follows the authenticated order source.
    from tests.test_lp_order_registration_contract import _adapter, _open_order, WALLET, TOKEN_ID, CONDITION_ID
    adapter, sdk = _adapter(tmp_path, orders=(_open_order('native', 'BUY', price='.40', original='20'),))
    sdk.list_account_trades = lambda: pytest.fail('current Reader read history')
    native = MarketReader(adapter.lp_reader_source_port())
    try:
        frame = native.read_current(ReadScope(WALLET, related_markets=True), time.monotonic() + 3)
        assert frame.orders[0]['order_id'] == 'native'
        market = frame.sources['market:' + CONDITION_ID + ':' + TOKEN_ID]
        assert market.complete and market.checked_at and market.value['book']['token_id'] == TOKEN_ID
        assert market.value['metadata'] and market.value['rewards']
    finally:
        assert native.close(timeout=2)
        adapter.close()

from decimal import Decimal
import pytest


def captured(*, orders=(), positions=(), orders_complete=True):
    from open_trader.polymarket_lp_market_reader import CapturedSources, SourceResult
    now = datetime(2026, 10, 15, tzinfo=UTC)
    return CapturedSources('account-A', {
        'balance': SourceResult('balance-1', ('100', '100'), checked_at=now),
        'open_orders': SourceResult('orders-1', orders, orders_complete, now),
        'positions': SourceResult('positions-1', positions, checked_at=now),
    })


def order(oid='o1', **changes):
    return dict(order_id=oid, account_id='account-A', condition_id='c1', token_id='t1',
        side='BUY', status='LIVE', original_size='20', size_matched='0', price='.40', **changes)


@pytest.mark.parametrize('case', ['empty', 'none', 'page_failed', 'foreign', 'overmatched', 'invalid_fill', 'missing_cost'])
def test_partial_facts_preserve_identity_completeness_and_unknown(case):
    from open_trader.polymarket_lp_market_reader import MarketReader
    rows, positions, complete = (), (), True
    if case == 'none': rows = None
    if case == 'page_failed': rows, complete = (order(),), False
    if case == 'foreign':
        bad = order('foreign'); bad['account_id'] = 'account-B'
        rows = (order(), bad)
    if case == 'overmatched':
        bad = order('bad'); bad['size_matched'] = '21'
        rows = (order(), bad)
    if case == 'invalid_fill':
        bad = order('bad'); bad.update(status='FILLED', size_matched='10')
        rows = (order(), bad)
    if case == 'missing_cost':
        positions = ({'account_id': 'account-A', 'condition_id': 'c1', 'token_id': 't1', 'quantity': '40'},)
    frame = MarketReader.from_captured_sources(captured(orders=rows, positions=positions, orders_complete=complete))
    assert frame.history_fees is None
    assert frame.orders_complete is (case not in {'none', 'page_failed', 'foreign', 'overmatched', 'invalid_fill'})
    assert [o['order_id'] for o in frame.orders] == (['o1'] if case in {'page_failed', 'foreign', 'overmatched', 'invalid_fill'} else [])
    assert frame.buy_reserved == (Decimal('0') if case in {'empty', 'missing_cost'} else None)
    assert frame.inventory_cost == (None if case == 'missing_cost' else Decimal('0'))
    if case in {'foreign', 'overmatched', 'invalid_fill'}:
        assert frame.errors and frame.orders[0]['remaining'] == Decimal('20')
    if case == 'empty':
        for status in ('OPEN', 'ACCEPTED', 'PARTIALLY_FILLED', 'CANCELING', 'CANCELLING', 'PENDING_CANCEL'):
            live = MarketReader.from_captured_sources(captured(orders=(dict(order(), status=status),)))
            assert live.orders_complete and live.buy_reserved == Decimal('8')
        unknown = MarketReader.from_captured_sources(captured(orders=(dict(order(), status='UNRECOGNIZED'),)))
        assert unknown.buy_reserved is None and not unknown.orders_complete
        for status in ('CANCELLED', 'REJECTED', 'EXPIRED', 'FAILED', 'MATCHED'):
            terminal = dict(order(), status=status, size_matched='20' if status == 'MATCHED' else '0')
            ended = MarketReader.from_captured_sources(captured(orders=(terminal,)))
            assert ended.orders_complete and ended.buy_reserved == 0 and ended.current_buys == ()
        average = {'token_id':'t1','condition_id':'c1','size':'40','average_price':'.40'}
        by_average = MarketReader.from_captured_sources(captured(positions=(average,)))
        assert by_average.inventory_cost == Decimal('16')
        resolved = dict(average, redeemable=True, current_price='0', current_value='0', average_price=None)
        assert MarketReader.from_captured_sources(captured(positions=(resolved,))).inventory_cost == 0



def test_same_id_uses_current_quantity_and_confirmed_terminal_exits():
    from open_trader.polymarket_lp_market_reader import MarketReader
    first = MarketReader.from_captured_sources(captured(orders=(order(),)))
    changed = order(); changed['size_matched'] = '10'
    second = MarketReader.from_captured_sources(captured(orders=(changed,)))
    assert first.buy_reserved == Decimal('8') and second.buy_reserved == Decimal('4')
    assert first.orders[0]['remaining'] == Decimal('20')
    for status in ('CANCELED', 'FILLED'):
        ended = dict(changed, status=status, size_matched='20' if status == 'FILLED' else '10')
        frame = MarketReader.from_captured_sources(captured(orders=(ended,)))
        assert frame.buy_reserved == 0 and frame.current_buys == ()
    absent = MarketReader.from_captured_sources(captured())
    assert absent.current_buys == () and absent.orders == ()
    assert second.current_buys[0]['remaining'] == Decimal('10')


@pytest.mark.parametrize('upstream', [False, True])
def test_timeout_retains_native_capacity_and_actual_retry_deadline(tmp_path, upstream):
    from open_trader.polymarket_lp_market_reader import MarketReader, ReadScope
    expired_calls = []
    class ExpiredSources:
        def read(self, component, scope):
            expired_calls.append(component)
            return ('100', '100')
    expired_reader = MarketReader(ExpiredSources())
    try:
        expired = expired_reader.read_current(ReadScope('account-A', ('balance',), related_markets=True), time.monotonic() - 1)
        assert expired_reader.drain(timeout=0), 'expired admission created a native task'
        assert expired_calls == []
        assert not expired.sources['balance'].complete and expired.sources['balance'].error == 'timeout'
        assert expired.sources['balance'].value is None
    finally:
        assert expired_reader.close(timeout=2)
    entered, release, finished = Event(), Event(), Event()
    clock = [0.0]
    calls = []

    class RetryFailure(Exception):
        retry_after_seconds = 120

    class Sources:
        def read(self, component, scope):
            calls.append((component, clock[0]))
            entered.set()
            try:
                assert release.wait(3)
                clock[0] = 10
                if upstream:
                    from urllib.error import HTTPError
                    raise HTTPError('https://offline.invalid', 503, 'sensitive original text', {'Retry-After': '120'}, None)
                raise RetryFailure('sensitive original text')
            finally:
                finished.set()

    adapter = None
    port = Sources()
    account_id = 'account-A'
    if upstream:
        from tests.test_lp_order_registration_contract import _adapter, WALLET
        adapter, sdk = _adapter(tmp_path)
        sdk.get_balance_allowance = lambda **kw: port.read('balance', None)
        account_id = WALLET
    reader = MarketReader(adapter.lp_reader_source_port() if adapter else port, clock=lambda: clock[0])
    scope = ReadScope(account_id, ('balance',))
    try:
        frame = reader.read_current(scope, time.monotonic() + .05)
        assert entered.is_set() and not frame.sources['balance'].complete
        again = reader.read_current(scope, time.monotonic() + .05)
        assert len(calls) == 1 and again.sources['balance'].error == 'in_progress'
        assert reader.drain(timeout=0) is False
        release.set()
        assert finished.wait(2)
        assert reader.drain(timeout=2)
        clock[0] = 129
        cooled = reader.read_current(scope, time.monotonic() + 1)
        assert len(calls) == 1 and cooled.sources['balance'].retry_at == 130
        assert cooled.sources['balance'].failed_at is not None
        assert cooled.sources['balance'].checked_at is None
        if upstream: assert cooled.sources['balance'].error_facts['status'] == 503
        clock[0] = 130
        reader.read_current(scope, time.monotonic() + 1)
        assert len(calls) == 2
        assert frame.sources['balance'].value is None  # historical timeout never mutates
    finally:
        release.set()
        assert reader.close(timeout=2)
        if adapter: adapter.close()


def test_pure_current_finances_use_independent_literals(monkeypatch):
    import socket, sqlite3
    def prohibited(*args, **kwargs):
        pytest.fail("pure calculation attempted external I/O")
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(sqlite3, "connect", prohibited)
    from open_trader.polymarket_lp_market_reader import MarketReader
    from open_trader.polymarket_lp_market_calculation import calculate_current, CalculationPolicy
    rows = (order(), order('o2'), dict(order('sell'), side='SELL'))
    positions = ({'condition_id': 'c1', 'token_id': 't1', 'quantity': '40', 'cost_usd': '16'},)
    frame = MarketReader.from_captured_sources(captured(orders=rows, positions=positions))
    result = calculate_current(frame, CalculationPolicy(budget=Decimal('40')), datetime(2026,10,15,tzinfo=UTC))
    assert result.buy_reserved == Decimal('16')
    assert result.inventory_cost == Decimal('16') and result.new_capacity == Decimal('8')
    unknown = MarketReader.from_captured_sources(captured(orders=None, positions=positions))
    result = calculate_current(unknown, CalculationPolicy(budget=Decimal('40')), datetime(2026,10,15,tzinfo=UTC))
    assert result.buy_reserved is None and result.new_capacity is None
    assert frame.history_fees is None  # current facts must not invent historical fees


def direction(pool='24'):
    return {'market': {'reward_min_size': Decimal('1'), 'reward_max_spread': Decimal('.10')},
        'book': {'bids': ({'price': '.40', 'size': '100'},), 'asks': ({'price': '.42', 'size': '100'},)},
        'daily_pool_usd': None if pool is None else Decimal(pool)}


def test_pure_current_yield_uses_remaining_size_literal_oracle():
    from open_trader.polymarket_lp_market_reader import MarketReader, CapturedSources, SourceResult
    from open_trader.polymarket_lp_market_calculation import calculate_current, CalculationPolicy
    now = datetime(2026,10,15,tzinfo=UTC)
    for matched, expected in [('0', '0.892857142857'), ('10', '0.862068965517')]:
        row = dict(order(), size_matched=matched)
        for pool in ('24', '0', None):
            inputs = captured(orders=(row,))
            inputs = CapturedSources(inputs.account, {**inputs.sources,
                'directions': SourceResult('direction-1', {'o1': direction(pool)}, checked_at=now)})
            frame = MarketReader.from_captured_sources(inputs)
            result = calculate_current(frame, CalculationPolicy(Decimal('40')), now)
            actual = result.yields['o1']
            if pool is None: assert actual is None
            elif pool == '0': assert actual == Decimal('0')
            else: assert actual.quantize(Decimal('.000000000001')) == Decimal(expected)


def test_pure_targets_are_fixed_and_deterministically_ordered():
    from open_trader.polymarket_lp_market_reader import MarketReader, CapturedSources, SourceResult
    from open_trader.polymarket_lp_market_calculation import calculate_current, CalculationPolicy
    now = datetime(2026,10,15,tzinfo=UTC)
    candidates = [dict(condition_id=label, token_id=label, order_id='', existing=False,
        direction=direction(pool), price='.40', quantity='20')
        for label, pool in [('F', '12'), ('E', '24'), ('D', '36'), ('C', '48')]]
    inputs = captured()
    source = dict(inputs.sources, candidates=SourceResult('candidates-1', candidates, checked_at=now))
    frame = MarketReader.from_captured_sources(CapturedSources(inputs.account, source))
    result = calculate_current(frame, CalculationPolicy(Decimal('24'), 3), now)
    assert result.targets == ('C', 'D', 'E')
    candidates[0]['direction']['daily_pool_usd'] = Decimal('9999')
    assert result.targets == ('C', 'D', 'E')
    equal = [dict(candidates[1], condition_id=k, token_id=k, existing=k == 'Z') for k in ['B', 'Z', 'A']]
    source['candidates'] = SourceResult('equal-1', equal, checked_at=now)
    source['open_orders'] = SourceResult('current-Z', (
        dict(order('Z'), condition_id='Z', token_id='Z'),), checked_at=now)
    source['directions'] = SourceResult('current-Z-market', {'Z': dict(direction(), book=dict(
        bids=[dict(price='.40', size='120')], asks=[dict(price='.42', size='100')]))}, checked_at=now)
    tied = calculate_current(MarketReader.from_captured_sources(CapturedSources(inputs.account, source)),
        CalculationPolicy(Decimal('24'), 3), now)
    assert tied.targets == ('Z', 'A', 'B')
    assert not hasattr(result, 'send') and not hasattr(result, 'actions')
