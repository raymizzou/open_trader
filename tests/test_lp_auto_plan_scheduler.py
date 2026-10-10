"""Issue 322 durable plans and controlled business deadlines."""
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import fcntl
import pytest

from open_trader.polymarket_lp_scheduler import LPAutoScheduler
from tests import test_lp_auto_pool as pool
from tests.test_lp_auto_target_convergence import plan_setup, live_orders, publish_candidates


def at_deadline(monkeypatch, engine, *, before=0):
    deadline = datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline'])
    monkeypatch.setattr(pool, 'NOW', deadline - timedelta(seconds=before))
    return deadline


def restart_engine(engine, exchange):
    store = pool.PredictionArbitrageStore(engine._store.path.parent.parent)
    lp = pool.PolymarketLPService(store, exchange, clock=lambda: pool.NOW)
    restored = pool.PredictionExecutionService(store=store, monitor=SimpleNamespace(), trading=exchange,
        notifier=SimpleNamespace(), lock_path=engine._lock_path, lp=lp)
    restored._breaker_open = False  # The simulated venue has no actual runtime owner.
    exchange.lp = lp
    return restored


@pytest.mark.parametrize('initial', [(), ('A', 'B', 'C', 'D', 'I')], ids=['zero', 'five'])
def test_ranking_generation_change_uses_api_wait(tmp_path, monkeypatch, initial):
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, initial)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    engine.lp_auto_configure(dict(round_interval_seconds=1,
        api_retry_interval_seconds=60, order_check_interval_seconds=10))
    planning_as_of = []
    exchange.before_sign = lambda: planning_as_of.append(engine.lp_auto_state()['funds']['as_of'])
    original_orders = live_orders(exchange)
    original_generation = store.lp_trade_generation()
    reads = dict(account=0, shared=0, metadata=0, reward=0, books=0)
    trace, invalidated_at = [], []
    race_enabled = [True]
    for name, counter in [('lp_account_snapshot', 'account'),
                          ('lp_account_snapshot_shared', 'shared'),
                          ('lp_market_metadata_fresh', 'metadata'),
                          ('lp_reward_catalog', 'reward'), ('lp_order_books', 'books')]:
        external = getattr(exchange, name)
        def observed(*args, _read=external, _counter=counter, **kwargs):
            reads[_counter] += 1
            trace.append((_counter, args, kwargs))
            result = _read(*args, **kwargs)
            if _counter == 'reward' and race_enabled[0] and not invalidated_at:
                assert reads['shared'] == 1, 'the complete account read precedes ranking'
                assert store.lp_advance_trade_generation(original_generation)
                invalidated_at.append(pool.NOW)
            return result
        monkeypatch.setattr(exchange, name, observed)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    first_reward = next(row for row in trace if row[0] == 'reward')
    assert first_reward[2]['condition_ids'] == (('A',) if initial else ('B',))
    assert len(invalidated_at) == 1
    assert store.lp_trade_generation() == original_generation + 1
    assert exchange.cancels == exchange.posts == []
    assert live_orders(exchange) == original_orders
    assert state['slots']['occupied'] == (5 if initial else 0)
    assert Decimal(state['funds']['buy_reserved_usd']) == (Decimal('39.00') if initial else Decimal(0))
    assert state['plan_wait']['kind'] == 'api', state['last_round']
    assert state['last_round']['reason'] == 'account_financial_facts_changed'
    assert not state['last_round'].get('completed_at')
    assert state['active_plan'] is None and state['last_round']['actions'] == []
    assert 'account_financial_facts_changed' in state['admission_block_reasons']
    started = datetime.fromisoformat(state['plan_wait']['started_at'])
    deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert started == invalidated_at[0] and deadline == started + timedelta(seconds=60)
    stopped_reads = dict(reads)
    for elapsed in (1, 59):
        monkeypatch.setattr(pool, 'NOW', started + timedelta(seconds=elapsed))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert reads == stopped_reads
        assert exchange.cancels == exchange.posts == []
        assert live_orders(exchange) == original_orders
    race_enabled[0] = False
    monkeypatch.setattr(pool, 'NOW', deadline)
    publish_candidates(exchange, lp, store, tuple('ABCDEFGHI'))
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert {row['condition_id'] for row in state['last_round']['targets']} == {'B', 'C', 'E', 'F', 'G'}
    if state['active_plan']:
        assert initial and state['plan_wait']['kind'] == 'order'
        at_deadline(monkeypatch, engine)
        assert scheduler.run_due()
        state = engine.lp_auto_state()
    assert [post['token_id'] for post in exchange.posts] == (['E', 'F', 'G'] if initial else ['B', 'C', 'E', 'F', 'G'])
    assert exchange.cancels == (['original-A', 'original-D', 'original-I'] if initial else [])
    assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
    if initial:
        assert live_orders(exchange)['B'] == 'original-B'
        assert live_orders(exchange)['C'] == 'original-C'
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('39.00')
    assert state['funds']['status'] == 'unknown' and state['last_round']['completed_at']
    assert planning_as_of and planning_as_of[0]
    assert datetime.fromisoformat(planning_as_of[0]) <= pool.NOW
    assert state['funds']['as_of'] == planning_as_of[0]
    assert state['funds']['spendable_usd'] is None and state['funds']['available_usd'] is None
    assert 'account_financial_facts_changed' in state['admission_block_reasons']








@pytest.mark.parametrize('window', ['first_cancel_result', 'unknown_POST_result'])
def test_api_failure_pauses_and_recovers_the_same_plan(tmp_path, monkeypatch, window):
    from copy import deepcopy
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    initial = ('A', 'B', 'C', 'D', 'I') if window == 'first_cancel_result' else ('B', 'C')
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, initial)
    result_reader = install_order_result_reader(exchange, monkeypatch)
    failed, queries, failure_ends, financial_reads = [False], [], [], []
    for name in ('lp_account_snapshot', 'lp_account_snapshot_shared'):
        original_read = getattr(exchange, name)
        def financial(*args, _read=original_read, **kwargs):
            if engine.lp_auto_state()['active_plan']:
                financial_reads.append(name)
            return _read(*args, **kwargs)
        monkeypatch.setattr(exchange, name, financial)
    def result(request):
        queries.append(pool.NOW)
        if failed[0]:
            monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=3))
            failure_ends.append(pool.NOW)
            raise TimeoutError('original plan result query unavailable')
        return result_reader(request)
    exchange.lp_order_result_snapshot = result
    if window == 'unknown_POST_result':
        post = exchange.lp_post_order
        def unknown(signed):
            if signed['token_id'] == 'E':
                exchange.posts.append(signed)
                raise TimeoutError('original E POST receipt unknown')
            return post(signed)
        exchange.lp_post_order = unknown
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    original = deepcopy(state['active_plan'])
    original_as_of = state['funds']['as_of']
    expected_posts = [] if window == 'first_cancel_result' else ['E', 'F', 'G']
    assert [p['token_id'] for p in exchange.posts] == expected_posts
    original_ids = [a['action_id'] for a in original['actions']]
    original_terms = [(t['token_id'], t['price'], t['quantity']) for t in original['targets']]
    unknown_action = next((a for a in original['actions'] if a['kind'] == 'buy' and a['token_id'] == 'E'), None) if window == 'unknown_POST_result' else None
    unknown_audit = deepcopy(store.lp_actions(unknown_action['session_id'])) if unknown_action else None
    failed[0] = True
    for _ in range(2):
        at_deadline(monkeypatch, engine)
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        assert state['active_plan']['round_id'] == original['round_id']
        assert state['active_plan']['targets'] == original['targets']
        assert [a['action_id'] for a in state['active_plan']['actions']] == original_ids
        assert state['plan_wait']['kind'] == 'api'
        assert state['last_round']['reason'] == 'order_result_read_failed'
        started = datetime.fromisoformat(state['plan_wait']['started_at'])
        assert started == failure_ends[-1]
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == started + timedelta(seconds=60)
        assert state['funds']['as_of'] == original_as_of
        query_count = len(queries)
        for elapsed in (1, 59):
            monkeypatch.setattr(pool, 'NOW', started + timedelta(seconds=elapsed))
            scheduler.request_check()
            assert not scheduler.run_due()
            assert len(queries) == query_count
            assert [p['token_id'] for p in exchange.posts] == expected_posts
    failed[0] = False
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['last_round']['round_id'] == original['round_id']
    assert [(t['token_id'], t['price'], t['quantity']) for t in state['last_round']['targets']] == original_terms
    assert [p['token_id'] for p in exchange.posts] == ['E', 'F', 'G']
    assert financial_reads == []
    if window == 'first_cancel_result':
        assert exchange.cancels == ['original-A', 'original-D', 'original-I']
        assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
        assert state['last_round']['completed_at'] and state['plan_wait']['kind'] == 'round'
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(state['last_round']['completed_at']) + timedelta(seconds=60)
    else:
        assert live_orders(exchange) == {'B': 'original-B', 'C': 'original-C', 'F': 'o2', 'G': 'o3'}
        assert not state['last_round']['completed_at'] and state['active_plan']
        assert state['plan_wait']['kind'] == 'order' and exchange.cancels == []
        recovered = next(a for a in state['last_round']['actions'] if a['action_id'] == unknown_action['action_id'])
        assert recovered['state'] == 'unknown' and recovered['order_id'] is None
        assert store.lp_actions(unknown_action['session_id']) == unknown_audit


@pytest.mark.parametrize('receipt', ['timeout', 'rejected', 'ACK', 'ACK-generation-race', 'ACK-retry-api-failure'])
def test_live_after_cancel_automatically_reconciles_and_retries(tmp_path, monkeypatch, receipt):
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    exchange.cancel_terminal = False
    cancel = exchange.cancel_order
    def response(order_id):
        if receipt.startswith('ACK'):
            return cancel(order_id)
        exchange.cancels.append(order_id)
        if receipt == 'timeout':
            raise TimeoutError('simulated cancel timeout')
        return dict(canceled=[], not_canceled={order_id: 'temporarily unavailable'})
    exchange.cancel_order = response
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    original = engine.lp_auto_state()['last_round']['round_id']
    assert exchange.cancels == ['original-A', 'original-D', 'original-I']
    at_deadline(monkeypatch, engine, before=1)
    scheduler.request_check()
    assert not scheduler.run_due()
    assert len(exchange.cancels) == 3
    if receipt == 'rejected':
        from copy import deepcopy
        at_deadline(monkeypatch, engine)
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        assert exchange.cancels == ['original-A', 'original-D', 'original-I'] and exchange.posts == []
        assert state['active_plan'] is None and state['last_round']['completed_at']
        assert [a['state'] for a in state['last_round']['actions'] if a['kind'] == 'cancel'] == ['rejected'] * 3
        original_audits = {session['session_id']: deepcopy(store.lp_actions(session['session_id']))
            for session in store.lp_sessions() if any(a.get('role') == 'reconciliation_cancel'
                for a in store.lp_actions(session['session_id']))}
        engine.lp_tick()
        engine = restart_engine(engine, exchange)
        scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
        completed = datetime.fromisoformat(state['last_round']['completed_at'])
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=60)
        for elapsed in (1, 59):
            monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=elapsed))
            scheduler.request_check()
            assert not scheduler.run_due()
            assert exchange.cancels == ['original-A', 'original-D', 'original-I'] and exchange.posts == []
        exchange.cancel_order = cancel
        at_deadline(monkeypatch, engine)
        publish_candidates(exchange, engine._lp, engine._store, tuple('ABCDEFGHI'))
        assert scheduler.run_due()
        assert engine.lp_auto_state()['last_round']['round_id'] != original
        assert exchange.cancels == ['original-A', 'original-D', 'original-I'] * 2
        for sid, audit in original_audits.items():
            assert engine._store.lp_actions(sid)[:len(audit)] == audit
        assert exchange.posts == []
        return
    if receipt in {'ACK-generation-race', 'ACK-retry-api-failure'}:
        original_plan = engine.lp_auto_state()['active_plan']
        reads, failed_read_end = [], []
        def generation_race(request):
            snapshot = result_reader(request)
            reads.append(store.lp_trade_generation())
            if receipt == 'ACK-retry-api-failure':
                monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=3))
                failed_read_end.append(pool.NOW)
                raise TimeoutError('temporary retry result API failure')
            assert store.lp_advance_trade_generation(reads[-1])
            return snapshot
        exchange.lp_order_result_snapshot = generation_race
        at_deadline(monkeypatch, engine)
        assert scheduler.run_due()
        raced = engine.lp_auto_state()
        assert exchange.cancels == ['original-A', 'original-D', 'original-I']
        assert exchange.posts == []
        assert raced['active_plan']['round_id'] == original
        assert raced['active_plan']['targets'] == original_plan['targets']
        assert raced['slots']['occupied'] == 5
        assert Decimal(raced['funds']['buy_reserved_usd']) == Decimal('39.00')
        assert raced['plan_wait']['kind'] == 'api'
        assert datetime.fromisoformat(raced['plan_wait']['deadline']) - datetime.fromisoformat(raced['plan_wait']['started_at']) == timedelta(seconds=60)
        if failed_read_end:
            assert datetime.fromisoformat(raced['plan_wait']['started_at']) == failed_read_end[0]
            assert datetime.fromisoformat(raced['plan_wait']['deadline']) == failed_read_end[0] + timedelta(seconds=60)
        assert len(reads) == 1
        at_deadline(monkeypatch, engine, before=1)
        scheduler.request_check()
        assert not scheduler.run_due()
        assert len(reads) == 1
        exchange.lp_order_result_snapshot = result_reader
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    assert exchange.cancels == ['original-A', 'original-D', 'original-I'] * 2
    assert engine.lp_auto_state()['last_round']['round_id'] == original
    assert not scheduler.run_due()
    assert len(exchange.cancels) == 6
    assert exchange.posts == []
    # Both attempts remain audited; no ACK was interpreted as released capital.
    attempts = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])
                if a.get('role') == 'reconciliation_cancel']
    assert len(attempts) == 6
    for order in exchange.orders:
        if order['order_id'] in exchange.cancels:
            order['status'] = 'CANCELED'
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    assert [p['token_id'] for p in exchange.posts] == ['E', 'F', 'G']
    assert engine.lp_auto_state()['last_round']['completed_at']


@pytest.mark.parametrize('fill,window', [('0', 'registered'), ('8', 'registered'),
    ('8', 'before_registration'), ('8', 'live_partial_before_registration')])
def test_api_absence_advances_plan_without_phantom_hold(tmp_path, monkeypatch, fill, window):
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    from copy import deepcopy
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    exchange.cancel_terminal = False
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    handle = None
    if window != 'registered':
        handle = engine._lock_path.open('a+')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert scheduler.run_due()
    finally:
        if handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
    before = deepcopy(engine.lp_auto_state())
    plan_id = before['last_round']['round_id']
    original_actions = [a['action_id'] for a in before['last_round']['actions']]
    original_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    for order in exchange.orders:
        if order['token_id'] in {'A', 'D', 'I'}:
            order.update(status='LIVE' if window == 'live_partial_before_registration' and order['token_id'] == 'A' else 'CANCELED',
                size_matched=fill if order['token_id'] == 'A' else '0')
    if fill == '8':
        exchange.positions = [dict(token_id='A', condition_id='A', size='8', average_price='.39')]
        exchange.trades = [dict(trade_id='actual-original-A', status='CONFIRMED', trader_side='MAKER',
            taker_order_id='external-taker', token_id='A', condition_id='A', side='BUY', size='8', price='.39',
            fee='0', timestamp=pool.NOW, maker_orders=[dict(order_id='original-A', token_id='A', side='BUY',
                price='.39', matched_amount='8', fee='0', maker_address='test-wallet')])]
    # Invalid account facts cannot establish absence or release any reservation.
    exchange.lp_order_result_snapshot = lambda request: {**result_reader(request), 'open_orders_complete': False}
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    assert exchange.posts == []
    failed = engine.lp_auto_state()
    assert failed['plan_wait']['kind'] == 'api' and failed['last_round']['reason'] == 'order_result_incomplete'
    assert failed['funds']['status'] == before['funds']['status'] and failed['funds']['as_of'] == before['funds']['as_of']
    assert Decimal(failed['funds']['buy_reserved_usd']) == 39 and failed['slots']['occupied'] == 5
    assert {sid: store.lp_actions(sid) for sid in original_audits} == original_audits
    exchange.lp_order_result_snapshot = result_reader
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['last_round']['round_id'] == plan_id
    if window == 'live_partial_before_registration':
        assert exchange.cancels == []
        assert [p['token_id'] for p in exchange.posts] == ['E', 'F']
        assert live_orders(exchange)['A'] == 'original-A'
        assert set(live_orders(exchange)) == {'A', 'B', 'C', 'E', 'F'}
        assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.12')
        assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('35.88')
        assert state['slots']['occupied'] == 5
        assert state['last_round']['completed_at'] and state['active_plan'] is None and state['plan_wait']['kind'] == 'round'
        assert all(p['price'] == Decimal('.39') and p['quantity'] == 20 and p['side'] == 'BUY' for p in exchange.posts)
        assert [a['action_id'] for a in state['last_round']['actions']] == original_actions
        assert {sid: store.lp_actions(sid) for sid in original_audits} == original_audits
        return
    assert [p['token_id'] for p in exchange.posts] == ['E', 'F', 'G']
    assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('39.00')
    assert Decimal(state['funds']['inventory_cost_usd']) == (Decimal('3.12') if fill == '8' else Decimal(0))
    assert state['last_round']['completed_at']
    assert [a['action_id'] for a in state['last_round']['actions']] == original_actions
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == before['funds']['as_of']
    assert all(p['price'] == Decimal('.39') and p['quantity'] == 20 and p['side'] == 'BUY' for p in exchange.posts)
    assert {sid: store.lp_actions(sid) for sid in original_audits} == original_audits
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(state['last_round']['completed_at']) + timedelta(seconds=60)
    # Historical pending/ACK cancel audit is retained; it cannot recreate holds.
    assert len([a for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])
                if a.get('role') == 'reconciliation_cancel']) == (3 if window == 'registered' else 0)
    if window == 'before_registration':
        assert exchange.cancels == []


@pytest.mark.parametrize('intervals', [(60, 60, 10), (90, 30, 5)], ids=['defaults', 'custom'])
def test_three_waits_use_independent_fixed_intervals(tmp_path, monkeypatch, intervals):
    engine, exchange, _, _ = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    unavailable = [False]
    def result(request):
        if unavailable[0]:
            raise TimeoutError('necessary original order-result read unavailable')
        return result_reader(request)
    exchange.lp_order_result_snapshot = result
    fields = ('round_interval_seconds', 'api_retry_interval_seconds', 'order_check_interval_seconds')
    if intervals != (60, 60, 10):
        engine.lp_auto_configure(dict(zip(fields, intervals)))
    assert tuple(engine.lp_auto_state()[key] for key in fields) == intervals
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()

    def assert_wait(kind, seconds):
        state = engine.lp_auto_state()
        assert state['plan_wait']['kind'] == kind
        assert datetime.fromisoformat(state['plan_wait']['deadline']) - datetime.fromisoformat(state['plan_wait']['started_at']) == timedelta(seconds=seconds)
        at_deadline(monkeypatch, engine, before=1)
        scheduler.request_check()
        assert not scheduler.run_due()
    assert_wait('order', intervals[2])
    unavailable[0] = True
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    assert_wait('api', intervals[1])
    unavailable[0] = False
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    completed = datetime.fromisoformat(engine.lp_auto_state()['last_round']['completed_at'])
    assert completed > datetime.fromisoformat(engine.lp_auto_state()['last_round']['started_at'])
    assert datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']) == completed + timedelta(seconds=intervals[0])
    assert_wait('round', intervals[0])
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()


def test_running_config_changes_only_future_waits(tmp_path, monkeypatch):
    engine, exchange, _, _ = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    unavailable = [False]
    def result(request):
        if unavailable[0]:
            raise TimeoutError('necessary original order-result read unavailable')
        return result_reader(request)
    exchange.lp_order_result_snapshot = result
    exchange.cancel_terminal = False
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    before = engine.lp_auto_state()
    original_round = before['active_plan']['round_id']
    state = engine.lp_auto_configure(dict(order_check_interval_seconds=15, api_retry_interval_seconds=30,
        round_interval_seconds=90, expected_config_version=before['config_version']))
    assert state['plan_wait'] == before['plan_wait']
    assert state['active_plan'] == before['active_plan']
    assert state['desired_running'] is True
    assert (state['budget_usd'], state['buy_price_level'], state['target_buy_count']) == ('100', 2, 5)
    with pytest.raises(ValueError, match='pause_and_finish'):
        engine.lp_auto_configure(dict(budget_usd='101', target_buy_count=5))
    with pytest.raises(ValueError, match='config_version_changed'):
        engine.lp_auto_configure(dict(order_check_interval_seconds=99, expected_config_version=before['config_version']))
    engine = restart_engine(engine, exchange)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    at_deadline(monkeypatch, engine, before=1)
    scheduler.request_check()
    assert not scheduler.run_due()
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert datetime.fromisoformat(state['plan_wait']['deadline']) - datetime.fromisoformat(state['plan_wait']['started_at']) == timedelta(seconds=15)
    unavailable[0] = True
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    before = engine.lp_auto_state()['plan_wait']
    engine.lp_auto_configure(dict(api_retry_interval_seconds=45))
    assert engine.lp_auto_state()['plan_wait'] == before
    assert datetime.fromisoformat(before['deadline']) - datetime.fromisoformat(before['started_at']) == timedelta(seconds=30)
    unavailable[0] = False
    for order in exchange.orders:
        if order['token_id'] in {'A', 'D', 'I'}:
            order['status'] = 'CANCELED'
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert [p['token_id'] for p in exchange.posts] == ['E', 'F', 'G'], state['last_round']
    assert state['last_round']['round_id'] == original_round
    assert state['last_round']['completed_at']
    assert state['api_retry_interval_seconds'] == 45
    assert datetime.fromisoformat(state['plan_wait']['deadline']) - datetime.fromisoformat(state['plan_wait']['started_at']) == timedelta(seconds=90)


@pytest.mark.parametrize('stage', ['cancel', 'api_failure', 'unknown_POST'])
def test_restart_and_duplicate_wakes_preserve_unresolved_request_identity(tmp_path, monkeypatch, stage):
    initial = ('B', 'C') if stage == 'unknown_POST' else ('A', 'B', 'C', 'D', 'I')
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, initial)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    query_failure = [False]
    def query(request):
        if query_failure[0]:
            raise TimeoutError('original restart query unavailable')
        return result_reader(request)
    exchange.lp_order_result_snapshot = query
    exchange.cancel_terminal = False
    if stage == 'unknown_POST':
        post = exchange.lp_post_order
        def unknown_post(signed):
            if signed['token_id'] == 'E':
                exchange.posts.append(signed)
                raise TimeoutError('original E POST receipt unknown')
            return post(signed)
        exchange.lp_post_order = unknown_post
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    if stage == 'api_failure':
        query_failure[0] = True
        at_deadline(monkeypatch, engine)
        assert scheduler.run_due()
    original = engine.lp_auto_state()['active_plan']
    requests = [(s['session_id'], a['action_key']) for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])]
    counts = len(exchange.posts), len(exchange.cancels)
    engine = restart_engine(engine, exchange)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert engine.lp_auto_state()['active_plan'] == original
    at_deadline(monkeypatch, engine, before=1)
    for _ in range(3):
        scheduler.request_check()
        assert not scheduler.run_due()
    assert (len(exchange.posts), len(exchange.cancels)) == counts
    for order in exchange.orders:
        if order['token_id'] in {'A', 'D', 'I'}:
            order['status'] = 'CANCELED'
    query_failure[0] = False
    if stage == 'unknown_POST':
        exchange.fail = False
        exchange.lp_post_order = post
    # An actual order-result read blocks the real check; a simultaneous wake/check
    # cannot send another request or acquire this cycle.
    reader = exchange.lp_order_result_snapshot
    entered, release = Event(), Event()
    def blocked_read(request):
        entered.set()
        assert release.wait(5), 'Independent result-read watchdog'
        return reader(request)
    exchange.lp_order_result_snapshot = blocked_read
    at_deadline(monkeypatch, engine)
    with ThreadPoolExecutor(1) as workers:
        running = workers.submit(scheduler.run_due)
        try:
            assert entered.wait(2)
            scheduler.request_check()
            assert not scheduler.run_due()
            assert (len(exchange.posts), len(exchange.cancels)) == counts
        finally:
            release.set()
        assert running.result(timeout=5)
    state = engine.lp_auto_state()
    assert state['last_round']['round_id'] == original['round_id']
    if stage == 'unknown_POST':
        assert state['active_plan'] and not state['last_round']['completed_at']
        assert state['plan_wait']['kind'] == 'order'
        unknown = next(a for a in state['last_round']['actions'] if a['kind'] == 'buy' and a['token_id'] == 'E')
        assert unknown['state'] == 'unknown' and unknown['order_id'] is None
        assert engine._store.lp_session(unknown['session_id'])['submit_status'] == 'unknown'
        assert len(exchange.posts) == counts[0]
    else:
        assert state['last_round']['completed_at'], state['last_round']
    assert all(request in [(s['session_id'], a['action_key']) for s in engine._store.lp_sessions()
        for a in engine._store.lp_actions(s['session_id'])] for request in requests)
    assert [p['token_id'] for p in exchange.posts] == ['E', 'F', 'G']
    assert len(exchange.cancels) == counts[1]
    assert set(live_orders(exchange)) == ({'B', 'C', 'F', 'G'} if stage == 'unknown_POST' else {'B', 'C', 'E', 'F', 'G'})


@pytest.mark.parametrize('resource', ['execution_lock', 'execution_lock_before_cancel', 'read_capacity', 'same_market'])
def test_transient_resource_contention_resumes_without_replacing_plan(tmp_path, monkeypatch, resource):
    engine, exchange, _, _ = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    held = []
    if resource == 'execution_lock_before_cancel':
        handle = engine._lock_path.open('a+')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(handle)
    assert scheduler.run_due()
    original = engine.lp_auto_state()['active_plan']
    entered = {token: Event() for token in ('Y', 'Z', 'E')}
    release = Event()
    metadata = exchange.lp_market_metadata_fresh
    if resource in {'execution_lock', 'execution_lock_before_cancel'}:
        def lock_after_preparation():
            if not held:
                handle = engine._lock_path.open('a+')
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(handle)
        if resource == 'execution_lock':
            exchange.before_sign = lock_after_preparation
        blockers = ()
    else:
        blockers = ('Y', 'Z') if resource == 'read_capacity' else ('E',)
        def blocking_metadata(ids, **kwargs):
            for token in blockers:
                if token in ids:
                    entered[token].set()
                    assert release.wait(5), 'Independent market-read watchdog'
            return metadata(ids, **kwargs)
        exchange.lp_market_metadata_fresh = blocking_metadata
    with ThreadPoolExecutor(max_workers=2) as workers:
        reads = [workers.submit(engine.lp_candidate_preview,
            dict(market_id=token, condition_id=token, token_id=token, outcome='YES')) for token in blockers]
        try:
            for token in blockers:
                assert entered[token].wait(2)
            at_deadline(monkeypatch, engine)
            assert scheduler.run_due()
            state = engine.lp_auto_state()
            assert state['last_round']['round_id'] == original['round_id']
            assert state['active_plan']['targets'] == original['targets']
            assert exchange.posts == [] if resource != 'same_market' else all(p['token_id'] != 'E' for p in exchange.posts)
            assert state['plan_wait']['kind'] == 'order'
            scheduler.request_check()
            assert not scheduler.run_due()
            if resource == 'execution_lock_before_cancel':
                # Repeated bounded calls preserve the original plan while its
                # planning timestamps age; they cannot weaken freshness.
                for _ in range(7):
                    at_deadline(monkeypatch, engine)
                    assert scheduler.run_due()
                assert exchange.cancels == []
        finally:
            release.set()
            for handle in held:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            exchange.before_sign = None
            for read in reads:
                read.result(timeout=5)
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    if resource == 'execution_lock_before_cancel':
        assert exchange.cancels == ['original-A', 'original-D', 'original-I']
        at_deadline(monkeypatch, engine)
        assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['last_round']['round_id'] == original['round_id']
    assert {p['token_id'] for p in exchange.posts} == {'E', 'F', 'G'}, state['last_round']
    assert len(exchange.posts) == 3
    assert state['last_round']['completed_at']
    assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}


@pytest.mark.parametrize('window', ['precancel_generation', 'precancel_ttl', 'final_buy_generation'])
def test_approved_plan_continues_after_financial_change(tmp_path, monkeypatch, window):
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    initial = ('A', 'B', 'C', 'D', 'I') if window.startswith('precancel') else ()
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, initial)
    install_order_result_reader(exchange, monkeypatch)
    interval = 300 if initial else 1
    engine.lp_auto_configure(dict(round_interval_seconds=interval,
        api_retry_interval_seconds=60, order_check_interval_seconds=1 if initial else 10))
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    captured, forbidden = [], []
    account, shared = exchange.lp_account_snapshot, exchange.lp_account_snapshot_shared
    def financial(read):
        def observed(*args, **kwargs):
            if engine.lp_auto_state()['active_plan']:
                forbidden.append(pool.NOW)
                raise TimeoutError('approved execution must not reread balance/allowance')
            return read(*args, **kwargs)
        return observed
    exchange.lp_account_snapshot = financial(account)
    exchange.lp_account_snapshot_shared = financial(shared)
    def change():
        state = engine.lp_auto_state()
        plan = state['active_plan']
        assert plan and all(a['state'] == 'pending' for a in plan['actions'])
        captured.append(state)
        if window == 'precancel_ttl':
            pool.NOW += timedelta(seconds=61)
        else:
            assert store.lp_advance_trade_generation(store.lp_trade_generation())
    held = None
    if initial:
        held = engine._lock_path.open('a+')
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        sign = exchange.lp_create_limit_order
        def final_buy(**kwargs):
            signed = sign(**kwargs)
            if kwargs['token_id'] == 'B':
                change()
            return signed
        exchange.lp_create_limit_order = final_buy
    try:
        assert scheduler.run_due()
        if initial:
            assert exchange.posts == exchange.cancels == []
            assert engine.lp_auto_state()['plan_wait']['kind'] == 'order'
            change()
            at_deadline(monkeypatch, engine)
            if window == 'precancel_ttl':
                monkeypatch.setattr(pool, 'NOW', max(pool.NOW, datetime.fromisoformat(captured[0]['plan_wait']['deadline'])) + timedelta(seconds=61))
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
            held = None
            assert scheduler.run_due()
        state = engine.lp_auto_state()
        if state['active_plan']:
            assert state['plan_wait']['kind'] == 'order'
            at_deadline(monkeypatch, engine)
            assert scheduler.run_due()
            state = engine.lp_auto_state()
        assert len(captured) == 1 and forbidden == []
        plan = captured[0]['active_plan']
        assert [r['condition_id'] for r in plan['targets']] == ['B', 'C', 'E', 'F', 'G']
        assert all(Decimal(r['price']) == Decimal('.39') and Decimal(r['quantity']) == 20 for r in plan['targets'])
        assert state['last_round']['round_id'] == plan['round_id']
        assert state['last_round']['targets'] == captured[0]['last_round']['targets']
        assert [a['action_id'] for a in state['last_round']['actions']] == [a['action_id'] for a in plan['actions']]
        assert [p['token_id'] for p in exchange.posts] == (['E', 'F', 'G'] if initial else ['B', 'C', 'E', 'F', 'G'])
        assert exchange.cancels == (['original-A', 'original-D', 'original-I'] if initial else [])
        assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20 for p in exchange.posts)
        assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
        if initial:
            assert live_orders(exchange)['B'] == 'original-B' and live_orders(exchange)['C'] == 'original-C'
        assert all(a['state'] == 'success' for a in state['last_round']['actions'])
        assert state['slots']['occupied'] == 5
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        assert state['plan_wait']['kind'] == 'round'
        completed = datetime.fromisoformat(state['last_round']['completed_at'])
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=interval)
        audits = {row['session_id']: store.lp_actions(row['session_id']) for row in store.lp_sessions()}
        for action in state['last_round']['actions']:
            if action['kind'] == 'buy':
                audit = store.lp_actions(action['session_id'])
                assert len(audit) == 1 and audit[0]['state'] == 'accepted' and audit[0]['post_started'] is True
        at_deadline(monkeypatch, engine, before=1)
        scheduler.request_check()
        assert not scheduler.run_due()
        assert {row['session_id']: store.lp_actions(row['session_id']) for row in store.lp_sessions()} == audits
        assert forbidden == []
    finally:
        if held is not None:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
