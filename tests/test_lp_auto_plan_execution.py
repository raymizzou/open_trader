"""Plan-time account approval and durable execution, through public entrypoints."""
from datetime import datetime, timedelta
from decimal import Decimal
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import fcntl

import pytest

from open_trader.polymarket_lp_scheduler import LPAutoScheduler
from tests import test_lp_auto_pool as pool
from tests.test_lp_auto_target_convergence import plan_setup, live_orders
from tests.test_lp_account_reservation_reconciliation import runtime


def install_order_result_reader(exchange, monkeypatch):
    """The external venue returns order facts without calling its balance API."""
    def result_reader(request):
        started = pool.NOW
        pool.NOW += timedelta(microseconds=1)
        return dict(authenticated=True, wallet_address='test-wallet',
            read_started_at=started, read_ended_at=pool.NOW, checked_at=pool.NOW,
            open_orders=deepcopy([o for o in exchange.orders if o['status'] == 'LIVE']),
            orders=deepcopy(exchange.orders), positions=deepcopy(exchange.positions),
            trades=deepcopy(exchange.trades), open_orders_complete=True,
            pagination_complete=True, trades_complete=True, positions_complete=True,
            order_read_errors={})
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', result_reader, raising=False)
    return result_reader


def test_entry_audit_registration_failure_prevents_post(tmp_path, monkeypatch):
    import sqlite3
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch)
    signed = []
    sign = exchange.lp_create_limit_order
    def signer(**kwargs):
        signed.append(kwargs)
        return sign(**kwargs)
    monkeypatch.setattr(exchange, 'lp_create_limit_order', signer)
    with sqlite3.connect(store.path) as connection:
        connection.execute('''CREATE TRIGGER fail_original_entry_audit BEFORE INSERT ON lp_actions
            WHEN json_extract(NEW.payload,'$.role')='entry'
            AND json_extract(NEW.payload,'$.side')='BUY'
            AND json_extract(NEW.payload,'$.token_id')='B'
            AND EXISTS (SELECT 1 FROM lp_sessions s JOIN lp_auto_pool p ON p.singleton=1
                WHERE s.session_id=NEW.session_id
                AND json_extract(s.payload,'$.token_id')='B'
                AND s.idempotency_key='lp-auto:' || json_extract(p.payload,'$.active_plan.actions[0].action_id'))
            BEGIN SELECT RAISE(ABORT, 'original entry audit registration failed'); END''')
    try:
        with pytest.raises(sqlite3.IntegrityError, match='original entry audit registration failed'):
            engine.lp_auto_run_once()
        state = engine.lp_auto_state()
        plan = state['active_plan']
        assert plan and not state['last_round'].get('completed_at')
        assert [r['condition_id'] for r in plan['targets']] == ['B', 'C', 'E', 'F', 'G']
        assert all(Decimal(r['price']) == Decimal('.39') and Decimal(r['quantity']) == 20 for r in plan['targets'])
        assert all(a['state'] == 'pending' for a in plan['actions'])
        first = plan['actions'][0]
        session, = store.lp_sessions()
        assert session['token_id'] == session['condition_id'] == 'B'
        assert session['idempotency_key'] == 'lp-auto:' + first.get('request_id', first['action_id'])
        assert session['submit_stage'] == 'preparing' and session['post_started'] is False
        assert session['state'] == 'entry_submit_pending' and not session.get('entry_order_id')
        assert store.lp_actions(session['session_id']) == []
        assert signed == exchange.posts == exchange.cancels == []
    finally:
        with sqlite3.connect(store.path) as connection:
            connection.execute('DROP TRIGGER IF EXISTS fail_original_entry_audit')


@pytest.mark.parametrize('invalid', [
    'balance_transport', 'incomplete_account', 'wrong_identity',
    'generation_during_read', 'generation_during_planning',
])
def test_planning_requires_valid_account_facts(tmp_path, monkeypatch, invalid):
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch)
    read = exchange.lp_account_snapshot_shared
    reward = exchange.lp_reward_catalog
    armed = [True]
    reads, failed_at = [], []

    def account(**kwargs):
        reads.append(pool.NOW)
        result = read(**kwargs)
        if armed[0] and invalid != 'generation_during_planning':
            # Failure ends after the read starts. The wait uses its end time.
            pool.NOW += timedelta(seconds=3)
            failed_at.append(pool.NOW)
            if invalid == 'balance_transport':
                raise TimeoutError('simulated balance transport failure')
            if invalid == 'incomplete_account':
                result['open_orders_complete'] = False
            elif invalid == 'wrong_identity':
                result.update(account_id='another-wallet', wallet_address='another-wallet')
            else:
                assert store.lp_advance_trade_generation(result['trade_generation'])
        return result

    def catalog(**kwargs):
        result = reward(**kwargs)
        if armed[0] and invalid == 'generation_during_planning' and not failed_at:
            assert store.lp_advance_trade_generation(store.lp_trade_generation())
            failed_at.append(pool.NOW)
        return result

    monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', account)
    monkeypatch.setattr(exchange, 'lp_reward_catalog', catalog)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert len(failed_at) == 1
    assert exchange.posts == exchange.cancels == []
    assert state['active_plan'] is None
    assert not state['last_round'].get('completed_at')
    assert state['last_round']['actions'] == []
    assert state['plan_wait']['kind'] == 'api'
    started = datetime.fromisoformat(state['plan_wait']['started_at'])
    deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert started == failed_at[0]
    assert deadline == started + timedelta(seconds=60)
    count = len(reads)
    for elapsed in (1, 59):
        monkeypatch.setattr(pool, 'NOW', started + timedelta(seconds=elapsed))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert len(reads) == count
        assert exchange.posts == exchange.cancels == []
        assert engine.lp_auto_state()['active_plan'] is None
    armed[0] = False
    monkeypatch.setattr(pool, 'NOW', deadline)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert len(reads) > count
    assert [p['token_id'] for p in exchange.posts] == ['B', 'C', 'E', 'F', 'G']
    assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
    assert exchange.cancels == []
    assert state['last_round']['completed_at']
    assert state['plan_wait']['kind'] == 'round'


@pytest.mark.parametrize('window', ['before_prepare', 'after_sign', 'long_lock', 'before_cancel'])
@pytest.mark.parametrize('financial_change', ['ttl', 'generation'])
def test_approved_plan_does_not_reapprove_account_finances(
    tmp_path, monkeypatch, window, financial_change,
):
    initial = ('A', 'B', 'C', 'D', 'I') if window == 'before_cancel' else ()
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, initial)
    install_order_result_reader(exchange, monkeypatch)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    saved, saved_target_summaries, forbidden_reads = [], [], []
    account = exchange.lp_account_snapshot
    shared = exchange.lp_account_snapshot_shared

    def invalidate():
        if saved:
            return
        plan = engine.lp_auto_state()['active_plan']
        assert plan and plan['actions']
        saved.append(plan)
        saved_target_summaries.append(engine.lp_auto_state()['last_round']['targets'])
        if financial_change == 'ttl':
            pool.NOW += timedelta(seconds=61)
        else:
            assert store.lp_advance_trade_generation(store.lp_trade_generation())

    def unavailable(name, read):
        def observe(*args, **kwargs):
            if saved:
                forbidden_reads.append(name)
                raise TimeoutError('balance/allowance transport unavailable after approval')
            return read(*args, **kwargs)
        return observe

    monkeypatch.setattr(exchange, 'lp_account_snapshot', unavailable('account', account))
    monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', unavailable('shared', shared))
    held = None
    if window == 'after_sign':
        exchange.before_sign = invalidate
    else:
        held = engine._lock_path.open('a+')
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert scheduler.run_due()
        if window != 'after_sign':
            state = engine.lp_auto_state()
            assert state['active_plan'] and state['plan_wait']['kind'] == 'order'
            assert exchange.posts == exchange.cancels == []
            invalidate()
            deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
            monkeypatch.setattr(pool, 'NOW', max(pool.NOW, deadline))
            if window == 'long_lock':
                monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=61))
                assert scheduler.run_due()
                waiting = engine.lp_auto_state()
                assert waiting['active_plan']['targets'] == saved[0]['targets']
                assert waiting['plan_wait']['kind'] == 'order'
                assert exchange.posts == exchange.cancels == []
                monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(waiting['plan_wait']['deadline']))
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
            held = None
            assert scheduler.run_due()
        state = engine.lp_auto_state()
        if initial and state['active_plan']:
            assert state['plan_wait']['kind'] == 'order'
            monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
            assert scheduler.run_due()
            state = engine.lp_auto_state()
        assert len(saved) == 1
        assert forbidden_reads == [], state['last_round']
        assert state['last_round']['round_id'] == saved[0]['round_id']
        assert state['last_round']['targets'] == saved_target_summaries[0]
        assert [a['action_id'] for a in state['last_round']['actions']] == [
            a['action_id'] for a in saved[0]['actions']]
        assert [p['token_id'] for p in exchange.posts] == (
            ['E', 'F', 'G'] if initial else ['B', 'C', 'E', 'F', 'G'])
        assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20
                   for p in exchange.posts)
        assert exchange.cancels == (['original-A', 'original-D', 'original-I'] if initial else [])
        assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
        assert state['last_round']['completed_at']
        assert state['active_plan'] is None and state['plan_wait']['kind'] == 'round'
    finally:
        exchange.before_sign = None
        if held is not None:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()


@pytest.mark.parametrize('rejection', [
    'balance_insufficient', 'allowance_insufficient', 'venue_denied', 'sdk_not_enough_balance',
])
def test_definite_buy_rejection_finishes_only_that_action(tmp_path, monkeypatch, rejection):
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch)
    install_order_result_reader(exchange, monkeypatch)
    post = exchange.lp_post_order
    reject_once = [True]
    expected_reason = 'not_enough_balance' if rejection == 'sdk_not_enough_balance' else rejection

    def venue(signed):
        if signed['token_id'] == 'B' and reject_once[0]:
            reject_once[0] = False
            exchange.posts.append(signed)
            if rejection == 'sdk_not_enough_balance':
                from polymarket.models.clob import RejectedOrder
                return RejectedOrder(code='not_enough_balance', message='not enough balance / allowance')
            return dict(accepted=False, status='REJECTED', reason=rejection)
        return post(signed)

    monkeypatch.setattr(exchange, 'lp_post_order', venue)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert [p['token_id'] for p in exchange.posts] == ['B', 'C', 'E', 'F', 'G']
    assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
    assert exchange.cancels == []
    first, *others = state['last_round']['actions']
    assert first['state'] == 'rejected' and first['reason'] == expected_reason
    assert all(a['state'] == 'success' for a in others)
    session = store.lp_session(first['session_id'])
    audit = store.lp_actions(first['session_id'])
    assert session['state'] == 'entry_rejected' and session['reason'] == expected_reason
    assert len(audit) == 1 and audit[0]['state'] == 'rejected'
    assert audit[0]['post_started'] is True and audit[0]['submit_stage'] == 'exchange_rejected'
    assert audit[0]['reason'] == expected_reason
    if rejection == 'sdk_not_enough_balance':
        assert session['exchange_rejection_message'] == 'not enough balance / allowance'
        assert audit[0]['exchange_rejection_message'] == 'not enough balance / allowance'
    assert state['last_round']['completed_at']
    assert state['active_plan'] is None and state['plan_wait']['kind'] == 'round'
    completed = datetime.fromisoformat(state['last_round']['completed_at'])
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=60)
    for elapsed in (0, 59):
        monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=elapsed))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert len(exchange.posts) == 5
        assert store.lp_actions(first['session_id']) == audit
    monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=60))
    assert scheduler.run_due()
    assert [p['token_id'] for p in exchange.posts] == ['B', 'C', 'E', 'F', 'G', 'B']
    assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
    assert store.lp_actions(first['session_id']) == audit


@pytest.mark.parametrize('unavailable', ['metadata', 'book'])
@pytest.mark.parametrize('planned_action', ['new_B', 'old_A'])
def test_unavailable_planned_market_keeps_existing_order_and_continues(
    tmp_path, monkeypatch, unavailable, planned_action,
):
    initial = ('A', 'B', 'C', 'D', 'I') if planned_action == 'old_A' else ()
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, initial)
    install_order_result_reader(exchange, monkeypatch)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    with engine._lock_path.open('a+') as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert scheduler.run_due()
            original = engine.lp_auto_state()['active_plan']
            assert original and exchange.posts == exchange.cancels == []
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    token = 'A' if initial else 'B'
    method = 'lp_market_metadata_fresh' if unavailable == 'metadata' else 'lp_order_books'
    read = getattr(exchange, method)
    failures = []

    def absent(ids, **kwargs):
        result = read(ids, **kwargs)
        if token in ids:
            failures.append(token)
            result.pop(token, None)
        return result

    monkeypatch.setattr(exchange, method, absent)
    old_session = next((r['session_id'] for r in original['victims'] if r['condition_id'] == token), None)
    protection = deepcopy(store.lp_session(old_session)['queue_protection']) if old_session else None
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']))
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    if initial:
        assert exchange.cancels == ['original-D', 'original-I']
        if any(a['state'] == 'canceling' for a in state['last_round']['actions']):
            assert state['plan_wait']['kind'] == 'order'
            monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
            assert scheduler.run_due()
            state = engine.lp_auto_state()
        assert live_orders(exchange)['A'] == 'original-A'
        assert store.lp_session(old_session)['queue_protection'] == protection
        assert [p['token_id'] for p in exchange.posts] == ['E', 'F']
        assert set(live_orders(exchange)) == {'A', 'B', 'C', 'E', 'F'}
    else:
        assert [p['token_id'] for p in exchange.posts] == ['C', 'E', 'F', 'G']
        assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
        assert state['last_round']['completed_at']
    assert failures
    assert state['last_round']['round_id'] == original['round_id']
    action = next(a for a in state['last_round']['actions'] if a['condition_id'] == token)
    assert action['state'] == 'rejected'
    assert action['reason'] == ('candidate_market_unknown' if unavailable == 'metadata' else 'book_unknown')
    assert state['plan_wait']['kind'] != 'api'
    assert not any(p['token_id'] == 'H' for p in exchange.posts)


@pytest.mark.parametrize('first_buy_result', ['accepted', 'unknown'])
def test_retained_order_skips_only_the_buy_without_a_slot(tmp_path, monkeypatch, first_buy_result):
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    install_order_result_reader(exchange, monkeypatch)
    if first_buy_result == 'unknown':
        post = exchange.lp_post_order
        def venue(signed):
            if signed['token_id'] == 'E':
                exchange.posts.append(signed)
                raise TimeoutError('E original POST result unknown')
            return post(signed)
        monkeypatch.setattr(exchange, 'lp_post_order', venue)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    with engine._lock_path.open('a+') as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert scheduler.run_due()
            original = engine.lp_auto_state()['active_plan']
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    metadata = exchange.lp_market_metadata_fresh
    unavailable = [True]
    def local_market(ids, **kwargs):
        result = metadata(ids, **kwargs)
        if unavailable[0]:
            result.pop('A', None)
        return result
    monkeypatch.setattr(exchange, 'lp_market_metadata_fresh', local_market)
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']))
    assert scheduler.run_due()
    assert exchange.cancels == ['original-D', 'original-I']
    state = engine.lp_auto_state()
    assert state['active_plan'] and state['plan_wait']['kind'] == 'order'
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['last_round']['round_id'] == original['round_id']
    assert [p['token_id'] for p in exchange.posts] == ['E', 'F']
    assert live_orders(exchange) == (dict(A='original-A', B='original-B', C='original-C', E='o1', F='o2')
        if first_buy_result == 'accepted' else dict(A='original-A', B='original-B', C='original-C', F='o2'))
    assert exchange.cancels == ['original-D', 'original-I']
    actions = state['last_round']['actions']
    assert [(a['condition_id'], a['state']) for a in actions] == [
        ('A', 'rejected'), ('D', 'success'), ('I', 'success'),
        ('E', 'success' if first_buy_result == 'accepted' else 'unknown'), ('F', 'success'), ('G', 'rejected')]
    assert actions[-1]['reason'] == 'target_filled'
    assert state['slots']['occupied'] == 5
    if first_buy_result == 'unknown':
        from tests.test_lp_auto_plan_scheduler import restart_engine
        assert state['active_plan'] and not state['last_round'].get('completed_at')
        assert state['plan_wait']['kind'] == 'order'
        original_e = deepcopy(actions[3])
        audit = store.lp_actions(original_e['session_id'])
        assert len(audit) == 1 and audit[0]['state'] == 'unknown' and audit[0]['post_started'] is True
        engine = restart_engine(engine, exchange)
        scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
        scheduler.request_check()
        assert not scheduler.run_due()
        monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
        assert scheduler.run_due()
        restored = engine.lp_auto_state()
        assert restored['active_plan']['round_id'] == original['round_id']
        assert restored['active_plan']['actions'][3] == original_e
        assert restored['active_plan']['actions'][-1]['reason'] == 'target_filled'
        assert restored['slots']['occupied'] == 5
        assert [p['token_id'] for p in exchange.posts] == ['E', 'F']
        assert store.lp_actions(original_e['session_id']) == audit
        return
    assert state['active_plan'] is None and state['last_round']['completed_at']
    assert state['plan_wait']['kind'] == 'round'
    unavailable[0] = False
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']) - timedelta(seconds=1))
    scheduler.request_check()
    assert not scheduler.run_due()
    assert exchange.cancels == ['original-D', 'original-I']
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
    assert scheduler.run_due()
    assert exchange.cancels == ['original-D', 'original-I', 'original-A']


@pytest.mark.parametrize('carrier', ['mapping', 'sdk'])
def test_definite_cancel_rejection_is_not_retried_in_this_round(tmp_path, monkeypatch, request, carrier):
    if carrier == 'sdk':
        from polymarket.models.clob.cancel import CancelOrdersResponse
        from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic, NOW
        from tests.test_lp_order_registration_contract import _open_order
        sdk_runtime = request.getfixturevalue('runtime')
        orders = (_open_order('rank-a', 'BUY', price='.39', original='20'),
                  _open_order('rank-b', 'BUY', price='.39', original='20'))
        store, _, account, lp, engine = sdk_runtime(orders=orders, public_client=_CandidateSDKPublic(NOW))
        engine.lp_auto_configure(dict(budget_usd='39', target_buy_count=1, buy_price_level=2))
        engine.lp_auto_set_desired_running(True)
        def venue(*, order_ids):
            account.cancels.append(order_ids)
            assert order_ids == ('rank-b',)
            return CancelOrdersResponse(canceled=(), not_canceled={'rank-b': 'venue_denied'})
        monkeypatch.setattr(account, 'cancel_orders', venue)
        scheduler = LPAutoScheduler(engine, clock=lambda: sdk_runtime.clock[0])
        assert scheduler.run_due()
        saved = engine.lp_auto_state()
        original = saved['active_plan']['actions'][0]
        assert original['order_id'] == 'rank-b'
        assert account.cancels == [('rank-b',)] and account.posts == []
        sdk_runtime.clock[0] = datetime.fromisoformat(saved['plan_wait']['deadline'])
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        action = state['last_round']['actions'][0]
        assert action['state'] == 'rejected' and action['reason'] == 'venue_denied'
        assert action['order_id'] == 'rank-b' and action['action_id'] == original['action_id']
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        assert state['plan_wait']['kind'] == 'round' and state['slots']['occupied'] == 2
        assert account.orders == orders
        audit = [a for a in store.lp_actions(original['session_id']) if a.get('order_id') == 'rank-b']
        assert len(audit) == 1 and audit[0]['state'] == 'rejected' and audit[0]['reason'] == 'venue_denied'
        scheduler.request_check()
        assert not scheduler.run_due()
        engine.lp_auto_run_once()
        engine.lp_tick()
        assert account.cancels == [('rank-b',)] and account.posts == []
        assert [a for a in store.lp_actions(original['session_id']) if a.get('order_id') == 'rank-b'] == audit
        return
    from tests.test_lp_auto_plan_scheduler import restart_engine
    from tests.test_lp_auto_target_convergence import publish_candidates, REWARDS
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    install_order_result_reader(exchange, monkeypatch)
    cancel = exchange.cancel_order
    rejected = [True]
    def venue(order_id):
        if order_id == 'original-A' and rejected[0]:
            exchange.cancels.append(order_id)
            return dict(canceled=[], not_canceled={order_id: 'venue_denied'})
        return cancel(order_id)
    monkeypatch.setattr(exchange, 'cancel_order', venue)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    original = engine.lp_auto_state()['active_plan']
    action = next(a for a in original['actions'] if a['condition_id'] == 'A')
    original_session = action['session_id']
    assert exchange.cancels == ['original-A', 'original-D', 'original-I']
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']))
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    action = next(a for a in state['last_round']['actions'] if a['condition_id'] == 'A')
    assert action['state'] == 'rejected' and action['reason'] == 'venue_denied'
    assert live_orders(exchange)['A'] == 'original-A'
    assert [p['token_id'] for p in exchange.posts] == ['E', 'F']
    assert state['last_round']['actions'][-1]['reason'] == 'target_filled'
    assert state['last_round']['completed_at'] and state['active_plan'] is None
    audit = [a for a in store.lp_actions(original_session) if a.get('order_id') == 'original-A']
    assert len(audit) == 1 and audit[0]['state'] == 'rejected'
    assert audit[0]['reason'] == 'venue_denied'
    session = store.lp_session(original_session)
    assert session['entry_cancel_requested'] is False
    assert all(b['state'] in ('registered', 'monitoring')
        for b in engine._lp._queue_protection_levels(session).values())
    scheduler.request_check()
    assert not scheduler.run_due()
    engine.lp_tick()
    assert exchange.cancels.count('original-A') == 1
    engine = restart_engine(engine, exchange)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert engine.lp_auto_state()['plan_wait'] == state['plan_wait']
    scheduler.request_check()
    assert not scheduler.run_due()
    engine.lp_tick()
    assert exchange.cancels.count('original-A') == 1
    assert [a for a in store.lp_actions(original_session) if a.get('order_id') == 'original-A'] == audit
    rejected[0] = False
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
    # A fresh runtime needs normal external discovery for the next plan.
    publish_candidates(exchange, engine._lp, engine._store, REWARDS)
    assert scheduler.run_due()
    assert exchange.cancels.count('original-A') == 2
    attempts = [a for a in store.lp_actions(original_session) if a.get('order_id') == 'original-A']
    assert len(attempts) == 2 and attempts[0] == audit[0]


@pytest.mark.parametrize('result', [
    'canceled', 'LIVE', 'missing_order', 'incomplete_open_orders', 'uuid_owner_canceled', 'wrong_maker',
])
def test_order_confirmation_does_not_depend_on_balance_transport(runtime, monkeypatch, result):
    from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic, NOW
    from tests.test_lp_order_registration_contract import _open_order, WALLET
    first = _open_order('rank-a', 'BUY', price='.39', original='20')
    second = _open_order('rank-b', 'BUY', price='.39', original='20')
    store, adapter, account, _, engine = runtime(
        orders=(first, second), public_client=_CandidateSDKPublic(NOW))
    engine.lp_auto_configure(dict(budget_usd='39', target_buy_count=1, buy_price_level=2))
    engine.lp_auto_set_desired_running(True)
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    with engine._lock_path.open('a+') as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert scheduler.run_due()
            state = engine.lp_auto_state()
            original = state['active_plan']
            assert original and state['plan_wait']['kind'] == 'order', state['last_round']
            assert account.posts == account.cancels == []
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    financial_as_of = state['funds']['as_of']
    balance_reads = account.balance_reads
    def unavailable(**kwargs):
        account.balance_reads += 1
        raise TimeoutError('SDK balance transport unavailable')
    monkeypatch.setattr(account, 'get_balance_allowance', unavailable)
    runtime.clock[0] = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert scheduler.run_due()
    assert account.cancels == [('rank-b',)]
    assert account.balance_reads == balance_reads
    if result != 'LIVE':
        account.orders = (first,)
    reads = []
    def lookup(*, order_id):
        reads.append(order_id)
        assert order_id == 'rank-b'
        if result == 'missing_order':
            return None
        row = _open_order(order_id, 'BUY', price='.39', original='20', status='CANCELED')
        if result in ('uuid_owner_canceled', 'wrong_maker'):
            row = row.model_copy(update=dict(owner='550e8400-e29b-41d4-a716-446655440000',
                maker_address=WALLET if result == 'uuid_owner_canceled' else '0x' + 'b' * 40))
        return row
    monkeypatch.setattr(account, 'get_order', lookup, raising=False)
    if result == 'incomplete_open_orders':
        monkeypatch.setattr(account, 'list_open_orders', lambda **kwargs: None)
    state = engine.lp_auto_state()
    runtime.clock[0] = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert account.balance_reads == balance_reads
    assert state['last_round']['round_id'] == original['round_id']
    assert state['funds']['as_of'] == financial_as_of
    assert account.posts == []
    if result in ('canceled', 'uuid_owner_canceled'):
        assert reads == ['rank-b']
        assert state['slots']['occupied'] == 1
        assert state['last_round']['actions'][0]['state'] == 'success'
        assert state['last_round']['completed_at'] and state['active_plan'] is None
    elif result == 'LIVE':
        assert reads == []
        assert state['slots']['occupied'] == 2
        assert state['active_plan'] and state['plan_wait']['kind'] == 'order'
    else:
        assert state['active_plan'] and not state['last_round'].get('completed_at')
        assert state['last_round']['actions'][0]['state'] == 'canceling'
        assert state['plan_wait']['kind'] == 'api'
        if result == 'wrong_maker':
            assert state['slots']['occupied'] == 2


@pytest.mark.parametrize('receipt', ['timeout', 'accepted_idless', 'sdk_opaque', 'ACK_LIVE', 'late_receipt'])
def test_unknown_submission_keeps_identity_until_result_recovery(tmp_path, monkeypatch, receipt):
    from tests.test_lp_auto_plan_scheduler import restart_engine
    from polymarket.models.clob.order_response import (
        AcceptedOrder, RawOrderResponse, normalize_order_response,
    )
    initial = ('A', 'B', 'C', 'D', 'I') if receipt == 'ACK_LIVE' else ()
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, initial)
    result_reader = install_order_result_reader(exchange, monkeypatch)
    post = exchange.lp_post_order
    entered, release = Event(), Event()
    if receipt == 'ACK_LIVE':
        exchange.cancel_terminal = False
    else:
        def venue(signed):
            if signed['token_id'] != 'B':
                return post(signed)
            if receipt == 'late_receipt':
                entered.set()
                assert release.wait(5), 'Independent original POST watchdog'
                response = post(signed)
                return AcceptedOrder(order_id=response['order_id'], status='live',
                    making_amount=Decimal('7.80'), taking_amount=Decimal('20'),
                    trade_ids=(), transactions_hashes=())
            exchange.posts.append(signed)
            if receipt == 'timeout':
                raise TimeoutError('original POST result unknown')
            if receipt == 'accepted_idless':
                return dict(accepted=True, status='ACCEPTED')
            return normalize_order_response(RawOrderResponse(success=True, errorMsg='',
                orderID='', status='live', makingAmount='7.80', takingAmount='20',
                tradeIDs=(), transactionsHashes=()))
        monkeypatch.setattr(exchange, 'lp_post_order', venue)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    if receipt == 'late_receipt':
        with ThreadPoolExecutor(max_workers=1) as workers:
            running = workers.submit(scheduler.run_due)
            try:
                assert entered.wait(3), 'Original POST did not start'
                before = engine.lp_auto_state()
                original = before['active_plan']
                first = original['actions'][0]
                session = next(s for s in store.lp_sessions()
                    if s['idempotency_key'] == 'lp-auto:' + first['action_id'])
                assert session['post_started'] is True and not exchange.posts
                scheduler.request_check()
                assert not scheduler.run_due()
                restored = restart_engine(engine, exchange)
                assert restored.lp_auto_run_once()['round_reason'] == 'round_in_progress'
                assert exchange.posts == []
            finally:
                release.set()
            assert running.result(timeout=5)
        state = engine.lp_auto_state()
        assert state['last_round']['round_id'] == original['round_id']
        assert [p['token_id'] for p in exchange.posts] == ['B', 'C', 'E', 'F', 'G']
        completed = state['last_round']['actions'][0]
        assert completed['session_id'] == session['session_id']
        assert completed['order_id'] == 'o1' and completed['action_id'] == first['action_id']
        audit, = store.lp_actions(session['session_id'])
        assert audit['state'] == 'accepted' and audit['order_id'] == 'o1'
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        return
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    original = state['active_plan']
    assert original and not state['last_round'].get('completed_at')
    assert state['plan_wait']['kind'] == 'order'
    if not initial:
        assert original['actions'][0]['state'] == 'unknown'
        assert [p['token_id'] for p in exchange.posts] == ['B', 'C', 'E', 'F', 'G']
        assert all(a['state'] == 'success' for a in original['actions'][1:])
        assert state['slots']['occupied'] == 5
    original_ids = [(a['action_id'], a.get('session_id'), a.get('order_id')) for a in original['actions']]
    original_audits = {s['session_id']: store.lp_actions(s['session_id']) for s in store.lp_sessions()}
    assert all(a['state'] != 'rejected' for rows in original_audits.values() for a in rows)
    failed_at, queries = [], []
    fail = [True]
    query_entered, query_release = Event(), Event()
    block = [False]
    def results(request):
        queries.append(pool.NOW)
        if fail[0]:
            pool.NOW += timedelta(seconds=3)
            failed_at.append(pool.NOW)
            raise TimeoutError('necessary order-result read unavailable')
        if block[0]:
            query_entered.set()
            assert query_release.wait(5), 'Independent result-query watchdog'
        return result_reader(request)
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
    exchange.account_failure = True
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert len(failed_at) == 1 and state['plan_wait']['kind'] == 'api'
    started = datetime.fromisoformat(state['plan_wait']['started_at'])
    deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert started == failed_at[0] and deadline == started + timedelta(seconds=60)
    counts = len(exchange.posts), len(exchange.cancels), len(queries)
    engine = restart_engine(engine, exchange)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    for elapsed in (1, 59):
        monkeypatch.setattr(pool, 'NOW', started + timedelta(seconds=elapsed))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert (len(exchange.posts), len(exchange.cancels), len(queries)) == counts
    fail[0], block[0] = False, True
    exchange.cancel_terminal = True
    monkeypatch.setattr(pool, 'NOW', deadline)
    with ThreadPoolExecutor(max_workers=1) as workers:
        running = workers.submit(scheduler.run_due)
        try:
            assert query_entered.wait(3), 'Recovery query did not start'
            scheduler.request_check()
            assert not scheduler.run_due()
            assert (len(exchange.posts), len(exchange.cancels)) == counts[:2]
        finally:
            query_release.set()
        assert running.result(timeout=5)
    state = engine.lp_auto_state()
    assert state['last_round']['round_id'] == original['round_id']
    assert state['plan_wait']['kind'] == 'order'
    if initial:
        assert exchange.cancels == ['original-A', 'original-D', 'original-I'] * 2
        assert exchange.posts == []
        assert state['slots']['occupied'] == 5
        block[0] = False
        monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        assert [p['token_id'] for p in exchange.posts] == ['E', 'F', 'G']
        assert state['last_round']['completed_at']
        for sid, rows in original_audits.items():
            assert all(a in store.lp_actions(sid) for a in rows)
    else:
        # Complete list absence is insufficient to settle this idless request.
        assert state['active_plan'] and not state['last_round'].get('completed_at')
        assert [(a['action_id'], a.get('session_id'), a.get('order_id'))
            for a in state['active_plan']['actions']] == original_ids
        assert state['active_plan']['targets'] == original['targets']
        assert state['active_plan']['actions'][0]['state'] == 'unknown'
        assert state['slots']['occupied'] == 5
        assert len(exchange.posts) == 5
        assert {sid: store.lp_actions(sid) for sid in original_audits} == original_audits


@pytest.mark.parametrize('resource', [
    'execution_lock_before_cancel', 'execution_lock_after_sign', 'read_capacity', 'same_market',
])
def test_resource_wait_resumes_fixed_plan_without_balance_recheck(tmp_path, monkeypatch, resource):
    engine, exchange, lp, _ = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    reader = install_order_result_reader(exchange, monkeypatch)
    result_reads = []
    def results(request):
        result_reads.append(pool.NOW)
        return reader(request)
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    held = []
    def hold():
        if not held:
            handle = engine._lock_path.open('a+')
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(handle)
    if resource == 'execution_lock_before_cancel':
        hold()
    try:
        assert scheduler.run_due()
        original = engine.lp_auto_state()['active_plan']
        assert original
        metadata = exchange.lp_market_metadata_fresh
        blockers = ('Y', 'Z') if resource == 'read_capacity' else ('E',) if resource == 'same_market' else ()
        entered = {token: Event() for token in blockers}
        completed = {token: Event() for token in blockers}
        release = Event()
        def blocking(ids, **kwargs):
            for token in blockers:
                if token in ids:
                    try:
                        entered[token].set()
                        assert release.wait(5), 'Independent underlying read watchdog'
                        return metadata(ids, **kwargs)
                    finally:
                        completed[token].set()
            return metadata(ids, **kwargs)
        monkeypatch.setattr(exchange, 'lp_market_metadata_fresh', blocking)
        if resource == 'execution_lock_after_sign':
            exchange.before_sign = hold
        forbidden = []
        arm = [False]
        for name in ('lp_account_snapshot', 'lp_account_snapshot_shared'):
            read = getattr(exchange, name)
            def balance(*args, _read=read, _name=name, **kwargs):
                if arm[0]:
                    forbidden.append(_name)
                    raise TimeoutError('financial transport unavailable during resource wait')
                return _read(*args, **kwargs)
            monkeypatch.setattr(exchange, name, balance)
        # Caller timeout must not release actual underlying read capacity.
        if resource == 'read_capacity':
            lp._market_read_timeout = .25
        with ThreadPoolExecutor(max_workers=2) as workers:
            previews = [workers.submit(engine.lp_candidate_preview,
                dict(market_id=token, condition_id=token, token_id=token, outcome='YES')) for token in blockers]
            try:
                for token in blockers:
                    assert entered[token].wait(3), 'Blocking external read did not start'
                if resource == 'read_capacity':
                    for preview in previews:
                        assert preview.result(timeout=3)['state'] == 'rejected'
                    assert not any(event.is_set() for event in completed.values())
                    lp._market_read_timeout = 10
                arm[0] = True
                before_results = len(result_reads)
                monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline'])
                    + timedelta(seconds=61))
                assert scheduler.run_due()
                state = engine.lp_auto_state()
                assert state['active_plan']['round_id'] == original['round_id']
                assert state['active_plan']['targets'] == original['targets']
                assert state['plan_wait']['kind'] == 'order'
                assert forbidden == []
                if resource == 'read_capacity':
                    assert len(result_reads) == before_results
                    assert not any(event.is_set() for event in completed.values())
                if resource != 'same_market':
                    assert exchange.posts == []
                else:
                    assert [p['token_id'] for p in exchange.posts] == ['F', 'G']
                scheduler.request_check()
                assert not scheduler.run_due()
            finally:
                release.set()
                for handle in held:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
                held.clear()
                exchange.before_sign = None
                for preview in previews:
                    preview.result(timeout=5)
                for token in blockers:
                    assert completed[token].wait(3)
        lp._market_read_timeout = 10
        monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']))
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        if state['active_plan'] and any(a['state'] == 'canceling' for a in state['active_plan']['actions']):
            monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
            assert scheduler.run_due()
            state = engine.lp_auto_state()
        assert forbidden == []
        assert [p['token_id'] for p in exchange.posts] == (['F', 'G', 'E'] if resource == 'same_market' else ['E', 'F', 'G'])
        assert exchange.cancels == ['original-A', 'original-D', 'original-I']
        assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20 for p in exchange.posts)
        assert state['last_round']['round_id'] == original['round_id']
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        if resource in ('read_capacity', 'same_market'):
            with lp._market_reads_lock:
                assert not any(not job.done() for job in lp._market_reads.values())
    finally:
        for handle in held:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        exchange.before_sign = None
        lp._market_read_timeout = 10


@pytest.mark.parametrize('control,window', [
    (control, window) for control in ('stop', 'breaker', 'owner', 'account', 'config')
    for window in ('after_plan', 'after_sign')
] + [('order_identity', 'before_cancel'), ('protection', 'before_cancel')])
def test_execution_controls_still_fence_an_approved_plan(tmp_path, monkeypatch, control, window):
    from open_trader.prediction_runtime import _RuntimeOwnershipLock
    initial = ('A', 'B', 'C', 'D', 'I') if window == 'before_cancel' else ()
    if control == 'protection':
        from tests.test_lp_auto_target_convergence import publish_candidates, REWARDS
        engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, tokens=initial)
        engine.lp_auto_run_once()
        original_a = live_orders(exchange)['A']
        exchange.posts.clear()
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
        publish_candidates(exchange, lp, store, REWARDS)
    else:
        engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, initial)
        original_a = 'original-A'
    install_order_result_reader(exchange, monkeypatch)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    owner = _RuntimeOwnershipLock(tmp_path / 'runtime.lock')
    owner.acquire()
    lp.owner_lock = owner
    captured, target_summaries = [], []

    def change_control():
        if captured:
            return
        captured.append(engine.lp_auto_state()['active_plan'])
        assert captured[0] and captured[0]['actions']
        target_summaries.append(engine.lp_auto_state()['last_round']['targets'])
        if control == 'stop':
            engine.lp_auto_set_desired_running(False)
        elif control == 'breaker':
            # Public startup reconciliation opens the real breaker when its
            # external account reader cannot establish a usable account.
            assert engine.reconcile_startup()['state'] == 'locked'
            assert engine.lp_mutation_allowed() is False
        elif control == 'owner':
            owner.release()
            assert owner.held is False
        elif control == 'account':
            exchange.config = SimpleNamespace(wallet_address='another-wallet')
        elif control == 'config':
            if window == 'after_sign':
                before = engine.lp_auto_state()
                with pytest.raises(ValueError, match='^pause_and_finish_automatic_buys_before_configuring$'):
                    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=5, buy_price_level=1))
                after = engine.lp_auto_state()
                for key in ('config_version', 'trading_config_version', 'buy_price_level', 'budget_usd'):
                    assert after[key] == before[key]
                assert after['active_plan']['targets'] == before['active_plan']['targets']
            else:
                engine.lp_auto_set_desired_running(False)
                engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=5, buy_price_level=1))
                engine.lp_auto_set_desired_running(True)
        elif control == 'order_identity':
            next(o for o in exchange.orders if o['order_id'] == 'original-A')['token_id'] = 'unowned-token'
        else:
            direction = exchange.direction
            def depleted(token):
                result = direction(token)
                if token == 'A':
                    result['book']['bids'][1]['size'] = '3000'
                return result
            exchange.direction = depleted
            exchange.cancel_terminal = False
            engine.lp_tick()
            session = next(s for s in store.lp_sessions() if s['token_id'] == 'A')
            assert any(b['state'] in ('triggered', 'canceling', 'cancel_unknown')
                       for b in session['queue_protection']['levels'].values())

    held = None
    try:
        if window == 'after_sign':
            exchange.before_sign = change_control
            assert scheduler.run_due()
        else:
            held = engine._lock_path.open('a+')
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert scheduler.run_due()
            waiting = engine.lp_auto_state()
            assert waiting['active_plan'] and waiting['plan_wait']['kind'] == 'order'
            assert exchange.posts == exchange.cancels == []
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
            held = None
            change_control()
            monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(waiting['plan_wait']['deadline']))
            assert scheduler.run_due()
        assert len(captured) == 1
        state = engine.lp_auto_state()
        assert state['last_round']['round_id'] == captured[0]['round_id']
        if control == 'config' and window == 'after_sign':
            assert [p['token_id'] for p in exchange.posts] == ['B', 'C', 'E', 'F', 'G']
            assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20 for p in exchange.posts)
            assert exchange.cancels == []
            assert state['last_round']['completed_at'] and state['active_plan'] is None
            assert state['last_round']['targets'] == target_summaries[0]
            engine.lp_auto_run_once()
            assert len(exchange.posts) == 5
        elif window != 'before_cancel':
            assert exchange.posts == exchange.cancels == []
            assert state['last_round']['targets'] == target_summaries[0]
            if control == 'config':
                assert all(a['state'] == 'rejected' and a['reason'] == 'config_version_changed'
                           for a in state['last_round']['actions'])
                assert state['last_round']['completed_at'] and state['active_plan'] is None
                completed = datetime.fromisoformat(state['last_round']['completed_at'])
                assert state['plan_wait']['kind'] == 'round'
                assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=60)
                reads = len(exchange.reads)
                monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=59))
                scheduler.request_check()
                assert not scheduler.run_due()
                assert len(exchange.reads) == reads and exchange.posts == exchange.cancels == []
                monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=60))
                from tests.test_lp_auto_target_convergence import publish_candidates, REWARDS
                publish_candidates(exchange, lp, store, REWARDS)
                assert scheduler.run_due()
                assert engine.lp_auto_state()['last_round']['round_id'] != captured[0]['round_id']
                assert [p['token_id'] for p in exchange.posts] == ['B', 'C', 'E', 'F', 'G']
                assert all(Decimal(p['price']) == Decimal('.40') and Decimal(p['quantity']) == 20 for p in exchange.posts)
            if window == 'after_sign':
                audits = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])]
                assert audits and all(a['post_started'] is False and a['state'] == 'rejected' for a in audits)
                before = deepcopy(audits)
                engine.lp_auto_run_once()
                assert [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])] == before
        elif control == 'order_identity':
            assert 'original-A' not in exchange.cancels
            assert next(o for o in exchange.orders if o['order_id'] == 'original-A')['status'] == 'LIVE'
        else:
            # The public protection loop owns its own cancel. Approved yield
            # rotation cannot append a second cancel or rewrite that audit.
            assert exchange.cancels.count(original_a) == 1
            session = next(s for s in store.lp_sessions() if s['token_id'] == 'A')
            audit = store.lp_actions(session['session_id'])
            assert any(a['role'] == 'entry-protection-cancel' for a in audit)
            assert not any(a['role'] == 'yield-rotation-cancel' for a in audit)
            assert not any(p['token_id'] == 'A' for p in exchange.posts)
    finally:
        exchange.before_sign = None
        owner.release()
        if held is not None:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()


@pytest.mark.parametrize('route', ['manual', 'augment', 'forged_manual', 'forged_augment', 'automatic_augment'])
@pytest.mark.parametrize('money', ['insufficient', 'unknown'])
def test_manual_and_augment_entries_keep_full_financial_validation(tmp_path, monkeypatch, route, money):
    from tests.test_polymarket_lp import _Exchange, _queue_book_snapshot, _request, _augment_preview_snapshot
    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    if route in ('augment', 'forged_augment'):
        store, exchange = pool.PredictionArbitrageStore(tmp_path / 'manual.sqlite'), _Exchange()
        exchange.snapshot_value = _queue_book_snapshot(pool.NOW, Decimal('120'))
        lp = pool.PolymarketLPService(store, exchange, clock=lambda: pool.NOW)
        entry = {**_request(pool.NOW), 'quantity': Decimal('120')}
        store.lp_save_price_history(entry['condition_id'], entry['token_id'], [], dict(state='known',
            amplitude=Decimal('.005'), checked_at=pool.NOW, valid_until=pool.NOW + timedelta(days=1)))
        preview = lp.preview(entry)
        started = lp.start(preview['preview_id'], 'manual-existing')
        assert started['state'] == 'entry_open'
        exchange.snapshot_value = _augment_preview_snapshot(pool.NOW, level=Decimal('380'), own_original=Decimal('120'))
        exchange.snapshot_value['account']['balance'] = '0' if money == 'insufficient' else None
        engine = PredictionExecutionService(store=store, monitor=SimpleNamespace(), trading=exchange,
            notifier=SimpleNamespace(), lock_path=tmp_path / 'execution.lock', lp=lp)
        engine._breaker_open = False  # Existing fixture's simulated clean startup.
        request = dict(session_id=started['session_id'], quantity='20', price='.29', idempotency_key='independent-augment')
        submit = engine.lp_submit_augment
    else:
        engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch)
        if route == 'automatic_augment':
            engine.lp_auto_run_once()
            started = next(s for s in store.lp_sessions() if s['token_id'] == 'B')
            request = dict(session_id=started['session_id'], quantity='20', price='.38', idempotency_key='automatic-augment')
            submit = engine.lp_submit_augment
        else:
            request = dict(market_id='X', condition_id='X', token_id='X', outcome='YES', price='.39', quantity='20',
                review_at=pool.NOW + timedelta(minutes=10), idempotency_key='independent-entry')
            submit = engine.lp_submit_entry
        account = exchange.lp_account_snapshot
        monkeypatch.setattr(exchange, 'lp_account_snapshot', lambda: {
            **account(), 'balance': '0' if money == 'insufficient' else None})
    if route.startswith('forged_'):
        # Client data cannot construct the server-owned authorization object.
        request.update(plan_authorization={'approved': True, 'round_id': 'forged', 'account_id': 'test-wallet',
            'price': '.39', 'quantity': '20'}, approved_plan=True, skip_account_validation=True,
            origin='automatic', active_plan={'version': 2, 'actions': [{'state': 'pending', 'kind': 'buy'}]})
    previous_posts = deepcopy(exchange.posts)
    result = submit(request)
    assert result['state'] == 'rejected'
    assert result['reason'] == ('automatic_session_augmentation_disabled' if route == 'automatic_augment'
        else 'balance_insufficient' if money == 'insufficient' else 'balance_unknown')
    assert exchange.posts == previous_posts
    assert not any(a.get('post_started') for s in store.lp_sessions()
        if s.get('idempotency_key') == request['idempotency_key'] for a in store.lp_actions(s['session_id']))


@pytest.mark.parametrize('interval', [60, 90], ids=['default60', 'custom90'])
def test_next_round_uses_new_facts_after_the_full_interval(tmp_path, monkeypatch, interval):
    from tests.test_lp_auto_target_convergence import publish_candidates, REWARDS
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, budget='39')
    install_order_result_reader(exchange, monkeypatch)
    if interval != 60:
        engine.lp_auto_configure(dict(round_interval_seconds=interval))
    account, post = exchange.lp_account_snapshot, exchange.lp_post_order
    new_facts, captured = [False], []
    reads = []
    def financial():
        result = account()
        reads.append((pool.NOW, new_facts[0]))
        if new_facts[0]:
            result.update(balance='60', allowance='60')
        return result
    def venue(signed):
        if not captured:
            captured.append(engine.lp_auto_state()['active_plan'])
            exchange.posts.append(signed)
            new_facts[0] = True
            exchange.rewards['H'] = Decimal('999')
            exchange.positions = [dict(token_id='A', condition_id='A', size='8', average_price='.39')]
            return dict(accepted=False, status='REJECTED', reason='venue_denied')
        return post(signed)
    monkeypatch.setattr(exchange, 'lp_account_snapshot', financial)
    monkeypatch.setattr(exchange, 'lp_post_order', venue)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['last_round']['completed_at'] and state['active_plan'] is None
    assert [p['token_id'] for p in exchange.posts] == ['B', 'C', 'E', 'F', 'G']
    assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
    assert [r['condition_id'] for r in state['last_round']['targets']] == ['B', 'C', 'E', 'F', 'G']
    assert not any(changed for _, changed in reads), 'Current plan re-read changed financial facts'
    completed = datetime.fromisoformat(state['last_round']['completed_at'])
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=interval)
    before = len(reads)
    for elapsed in (59, interval - 1):
        monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=elapsed))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert len(reads) == before and len(exchange.posts) == 5
        assert engine.lp_auto_state()['last_round']['round_id'] == captured[0]['round_id']
    monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=interval))
    publish_candidates(exchange, lp, store, REWARDS)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert len(reads) > before and any(changed for _, changed in reads[before:])
    assert state['last_round']['round_id'] != captured[0]['round_id']
    # New planning sees 3.12 inventory: 39 - 3.12 = 35.88, so only
    # four 7.80 targets fit. H now wins; B/C/E follow the literal rewards.
    assert [r['condition_id'] for r in state['last_round']['targets']] == ['H', 'B', 'C', 'E']
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.12')
    assert exchange.cancels == ['o4', 'o5']
    if state['active_plan']:
        monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
        assert scheduler.run_due()
    assert [p['token_id'] for p in exchange.posts] == ['B', 'C', 'E', 'F', 'G', 'H', 'B']
    assert set(live_orders(exchange)) == {'H', 'B', 'C', 'E'}
    assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20 for p in exchange.posts)


@pytest.mark.parametrize('scenario', [
    'approved_unsent', 'cancel_pending', 'unknown_post', 'accepted_idless', 'completed_wait',
    'legacy_approved_unsent', 'legacy_cancel_pending', 'legacy_unknown_post',
    'legacy_accepted_idless', 'legacy_completed_wait', 'accepted_before_progress', 'receipt_await_apply', 'sign_interrupted',
    'rejected_before_progress', 'receipt_await_apply_terminal', 'receipt_await_apply_terminal_fill',
])
def test_restart_and_legacy_plan_upgrade_preserve_progress(tmp_path, monkeypatch, scenario):
    import base64
    import hashlib
    import json
    from pathlib import Path
    import sqlite3
    import zlib
    from tests.test_lp_auto_plan_scheduler import restart_engine
    from tests.test_lp_auto_target_convergence import PlanExchange
    legacy = scenario.startswith('legacy_')
    case = scenario.removeprefix('legacy_')
    initial = ('A', 'B', 'C', 'D', 'I') if case == 'cancel_pending' else ()
    held = None
    trigger = False
    if legacy:
        manifest = json.loads((Path(__file__).parent / 'fixtures/lp_auto_legacy_plans.json').read_text())
        sample = manifest['samples'][case]
        assert manifest['baseline_sha'] == sample['baseline_sha'] == 'ad59c636970acd81c06f76c5a6f1aefef7df770a'
        image = zlib.decompress(base64.b64decode(sample['database_zlib_base64']))
        assert hashlib.sha256(image).hexdigest() == sample['database_sha256']
        store = pool.PredictionArbitrageStore(tmp_path / 'state.sqlite')
        store.path.write_bytes(image)
        store = pool.PredictionArbitrageStore(tmp_path / 'state.sqlite')
        monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(sample['captured_at']))
        exchange = PlanExchange()
        for key, value in sample['exchange'].items():
            setattr(exchange, key, deepcopy(value))
        lp = pool.PolymarketLPService(store, exchange, clock=lambda: pool.NOW)
        exchange.lp = lp
        engine = pool.PredictionExecutionService(store=store, monitor=SimpleNamespace(), trading=exchange,
            notifier=SimpleNamespace(), lock_path=tmp_path / 'execution.lock', lp=lp)
        engine._breaker_open = False  # Isolated simulated startup, as existing fixture.
    else:
        engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, initial)
    install_order_result_reader(exchange, monkeypatch)
    try:
        if not legacy:
            if case == 'approved_unsent':
                held = engine._lock_path.open('a+')
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif case == 'cancel_pending':
                exchange.cancel_terminal = False
            elif case in ('unknown_post', 'accepted_idless'):
                post = exchange.lp_post_order
                def unknown(signed):
                    if signed['token_id'] == 'B':
                        exchange.posts.append(signed)
                        if case == 'unknown_post':
                            raise TimeoutError('actual POST outcome unknown')
                        return dict(accepted=True, status='ACCEPTED')
                    return post(signed)
                monkeypatch.setattr(exchange, 'lp_post_order', unknown)
            elif case in ('receipt_await_apply', 'receipt_await_apply_terminal', 'receipt_await_apply_terminal_fill'):
                post = exchange.lp_post_order
                def accepted(signed):
                    nonlocal held
                    response = post(signed)
                    if signed['token_id'] == 'B':
                        held = engine._lock_path.open('a+')
                        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return response
                monkeypatch.setattr(exchange, 'lp_post_order', accepted)
            elif case == 'sign_interrupted':
                def interrupted():
                    raise SystemExit('original sign invocation ended before POST')
                exchange.before_sign = interrupted
            elif case == 'rejected_before_progress':
                post = exchange.lp_post_order
                def rejected(signed):
                    if signed['token_id'] == 'B':
                        exchange.posts.append(signed)
                        return dict(accepted=False, status='REJECTED', reason='balance_insufficient')
                    return post(signed)
                monkeypatch.setattr(exchange, 'lp_post_order', rejected)
                with sqlite3.connect(store.path) as connection:
                    connection.execute('''CREATE TRIGGER fail_plan_progress BEFORE UPDATE ON lp_auto_pool
                        WHEN EXISTS (SELECT 1 FROM lp_actions a JOIN lp_sessions s USING(session_id)
                            WHERE a.state='rejected' AND s.state='entry_rejected'
                            AND json_extract(a.payload,'$.role')='entry'
                            AND json_extract(a.payload,'$.token_id')='B'
                            AND json_extract(s.payload,'$.token_id')='B'
                            AND json_extract(a.payload,'$.post_started')=1
                            AND json_extract(s.payload,'$.post_started')=1
                            AND json_extract(a.payload,'$.reason')='balance_insufficient'
                            AND json_extract(s.payload,'$.reason')='balance_insufficient')
                        BEGIN SELECT RAISE(ABORT, 'rejected receipt persisted before plan progress'); END''')
                trigger = True
            elif case == 'accepted_before_progress':
                # Actual external storage failure after the original receipt
                # and session, before any corresponding pool progress write.
                with sqlite3.connect(store.path) as connection:
                    connection.execute('''CREATE TRIGGER fail_plan_progress BEFORE UPDATE ON lp_auto_pool
                        WHEN EXISTS (SELECT 1 FROM lp_actions a JOIN lp_sessions s USING(session_id)
                            WHERE a.state='accepted' AND s.state='entry_open'
                            AND json_extract(a.payload,'$.role')='entry'
                            AND json_extract(a.payload,'$.order_id')=json_extract(s.payload,'$.entry_order_id'))
                        BEGIN SELECT RAISE(ABORT, 'accepted receipt persisted before plan progress'); END''')
                trigger = True
            if case == 'sign_interrupted':
                with pytest.raises(SystemExit, match='original sign invocation ended before POST'):
                    engine.lp_auto_run_once()
                exchange.before_sign = None
            elif trigger:
                with pytest.raises(sqlite3.IntegrityError, match=f'{"rejected" if case == "rejected_before_progress" else "accepted"} receipt persisted before plan progress'):
                    engine.lp_auto_run_once()
            else:
                engine.lp_auto_run_once()
        saved = engine.lp_auto_state()
        assert saved['last_round']['round_id']
        previous_posts, previous_cancels = deepcopy(exchange.posts), list(exchange.cancels)
        audit = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        if case == 'sign_interrupted':
            first = saved['active_plan']['actions'][0]
            original_request = first.get('request_id', first['action_id'])
            original_intent = next(i for i in saved['intents'] if i['intent_id'] == original_request)
            original_session = store.lp_session(original_intent['session_id'])
            original_audit = audit[original_session['session_id']][0]
            assert first['state'] == 'pending' and original_intent['state'] == 'reserved'
            assert original_session['idempotency_key'] == f'lp-auto:{original_request}'
            assert original_session['submit_stage'] == original_audit['submit_stage'] == 'preparing'
            assert original_session['post_started'] is original_audit['post_started'] is False
            assert original_audit['state'] == 'pending'
            assert not original_audit.get('order_id') and not original_session.get('entry_order_id')
            assert not lp.entry_send_inflight(original_session['session_id'])
            assert exchange.posts == []
        if case == 'rejected_before_progress':
            assert [p['token_id'] for p in exchange.posts] == ['B']
            first = saved['active_plan']['actions'][0]
            assert first['state'] == 'pending'
            original_request = first.get('request_id', first['action_id'])
            original_intent = next(i for i in saved['intents'] if i['intent_id'] == original_request)
            original_session = store.lp_session(original_intent['session_id'])
            original_audit = audit[original_session['session_id']][0]
            assert original_session['state'] == 'entry_rejected'
            assert original_session['idempotency_key'] == f'lp-auto:{original_request}'
            assert original_session['post_started'] is original_audit['post_started'] is True
            assert original_audit['state'] == 'rejected'
            assert original_session['reason'] == original_audit['reason'] == 'balance_insufficient'
            with sqlite3.connect(store.path) as connection:
                connection.execute('DROP TRIGGER fail_plan_progress')
            trigger = False
        if case in ('accepted_before_progress', 'receipt_await_apply', 'receipt_await_apply_terminal', 'receipt_await_apply_terminal_fill'):
            assert [p['token_id'] for p in exchange.posts] == ['B']
            first = saved['active_plan']['actions'][0]
            assert first['state'] == ('pending' if trigger else 'unknown')
            session = next(s for s in store.lp_sessions() if s['token_id'] == 'B')
            accepted_audit = [a for a in audit[session['session_id']] if a['role'] == 'entry']
            assert len(accepted_audit) == 1 and accepted_audit[0]['state'] == 'accepted'
            assert accepted_audit[0]['order_id'] == live_orders(exchange)['B'] == 'o1'
            assert accepted_audit[0]['post_started'] is True
            if case in ('receipt_await_apply_terminal', 'receipt_await_apply_terminal_fill'):
                assert not session.get('entry_order_id')
                next(o for o in exchange.orders if o['order_id'] == 'o1').update(status='CANCELED',
                    size_matched='8' if case == 'receipt_await_apply_terminal_fill' else '0')
                if case == 'receipt_await_apply_terminal_fill':
                    exchange.positions = [dict(token_id='B', condition_id='B', size='8', average_price='.39')]
                    exchange.trades = [dict(trade_id='actual-B-fill', order_id='o1', condition_id='B', token_id='B', side='BUY',
                        size='8', price='.39', fee='0', status='CONFIRMED', timestamp=pool.NOW, trader_side='MAKER',
                        taker_order_id='external-taker', maker_orders=[dict(order_id='o1', token_id='B', side='BUY',
                            price='.39', matched_amount='8', fee='0', maker_address='test-wallet', owner='credential-owner-uuid')])]
            if trigger:
                assert session['state'] == 'entry_open' and session['entry_order_id'] == 'o1'
                with sqlite3.connect(store.path) as connection:
                    connection.execute('DROP TRIGGER fail_plan_progress')
                trigger = False
        deadline = datetime.fromisoformat(saved['plan_wait']['deadline']) if saved.get('plan_wait') else pool.NOW
        restored = restart_engine(engine, exchange)
        state = restored.lp_auto_state()
        assert state['active_plan'] == saved['active_plan']
        assert state['plan_wait'] == saved['plan_wait']
        assert state['last_round']['actions'] == saved['last_round']['actions']
        assert state['last_round']['targets'] == saved['last_round']['targets']
        scheduler = LPAutoScheduler(restored, clock=lambda: pool.NOW)
        if deadline > pool.NOW:
            monkeypatch.setattr(pool, 'NOW', deadline - timedelta(seconds=1))
            assert not scheduler.run_due()
            assert exchange.posts == previous_posts and exchange.cancels == previous_cancels
            assert {sid: store.lp_actions(sid) for sid in audit} == audit
        if case == 'completed_wait':
            assert state['active_plan'] is None and state['plan_wait']['kind'] == 'round'
            return
        if held is not None:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
            held = None
        if case == 'cancel_pending':
            for order in exchange.orders:
                if order['order_id'] in exchange.cancels:
                    order['status'] = 'CANCELED'
        original_financial = deepcopy(state['funds'])
        def unavailable(*args, **kwargs):
            pytest.fail('Restart reapproved original financial facts')
        monkeypatch.setattr(exchange, 'lp_account_snapshot', unavailable)
        monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', unavailable)
        monkeypatch.setattr(pool, 'NOW', max(pool.NOW, deadline))
        assert scheduler.run_due()
        state = restored.lp_auto_state()
        assert state['last_round']['round_id'] == saved['last_round']['round_id']
        assert state['last_round']['targets'] == saved['last_round']['targets']
        assert [a['action_id'] for a in state['last_round']['actions']] == [a['action_id'] for a in saved['last_round']['actions']]
        if case == 'sign_interrupted':
            previous = store.lp_actions(original_session['session_id'])[0]
            assert previous['state'] == 'rejected' and previous['submit_stage'] == 'pre_send_rejected'
            assert previous['reason'] == 'preparation_interrupted' and previous['post_started'] is False
            assert previous['recovery_observed_at']
            for key in ('action_id', 'action_key', 'session_id', 'created_at', 'submit_requested_at', 'token_id', 'side'):
                assert previous[key] == original_audit[key]
            old_session = store.lp_session(original_session['session_id'])
            assert old_session['state'] == 'entry_rejected' and old_session['idempotency_key'] == original_session['idempotency_key']
            first = state['last_round']['actions'][0]
            assert first['request_id'] != original_request and first['session_id'] != original_session['session_id']
            assert first['attempts'] == [dict(request_id=original_request, session_id=original_session['session_id'],
                state='not_sent', reason='preparation_interrupted')]
            accepted = store.lp_actions(first['session_id'])[0]
            assert accepted['state'] == 'accepted' and accepted['order_id'] == 'o1'
            before_attempts = deepcopy(first['attempts'])
            with ThreadPoolExecutor(2) as workers:
                list(workers.map(lambda _: restored.lp_auto_run_once(), range(2)))
            again = restart_engine(restored, exchange)
            again.lp_auto_run_once()
            assert len(exchange.posts) == 5
            assert again.lp_auto_state()['last_round']['actions'][0]['attempts'] == before_attempts
            assert store.lp_actions(original_session['session_id'])[0] == previous
        else:
            assert {sid: store.lp_actions(sid) for sid in audit} == audit
        if case in ('unknown_post', 'accepted_idless'):
            assert [p['token_id'] for p in exchange.posts] == ['B', 'C', 'E', 'F', 'G']
            assert state['active_plan']['actions'][0]['state'] == 'unknown'
            assert all(a['state'] == 'success' for a in state['active_plan']['actions'][1:])
            assert state['plan_wait']['kind'] == 'order' and not state['last_round']['completed_at']
        else:
            if case == 'rejected_before_progress':
                first = state['last_round']['actions'][0]
                assert first['state'] == 'rejected' and first['reason'] == 'balance_insufficient'
                assert first.get('request_id', first['action_id']) == original_request
                assert first['session_id'] == original_session['session_id']
                assert all(a['state'] == 'success' for a in state['last_round']['actions'][1:])
            else:
                assert all(a['state'] == 'success' for a in state['last_round']['actions']), state['last_round']
            assert state['last_round']['completed_at'] and state['active_plan'] is None
            assert [p['token_id'] for p in exchange.posts] == (['E', 'F', 'G'] if initial else ['B', 'C', 'E', 'F', 'G'])
            assert exchange.cancels == previous_cancels
            completed = datetime.fromisoformat(state['last_round']['completed_at'])
            assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=60)
            if case in ('rejected_before_progress', 'receipt_await_apply_terminal', 'receipt_await_apply_terminal_fill'):
                assert state['slots']['occupied'] == 4
                assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
                before_posts = deepcopy(exchange.posts)
                with ThreadPoolExecutor(2) as workers:
                    list(workers.map(lambda _: restored.lp_auto_run_once(), range(2)))
                again = restart_engine(restored, exchange)
                again.lp_auto_run_once()
                assert exchange.posts == before_posts
                assert {sid: store.lp_actions(sid) for sid in audit} == audit
                if case == 'receipt_await_apply_terminal_fill':
                    original_b = store.lp_session(session['session_id'])
                    assert Decimal(original_b['buy_filled_quantity']) == Decimal(original_b['residual_quantity']) == 8
                    assert Decimal(original_b['buy_cost']) == Decimal('3.12')
                    assert original_b['entry_order_id'] == 'o1'
                    assert original_b['order_history']['o1']['status'] == 'CANCELED'
                    assert exchange.positions == [dict(token_id='B', condition_id='B', size='8', average_price='.39')]
                    assert all(p['side'] == 'BUY' for p in exchange.posts)
        assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20 for p in exchange.posts)
        assert state['funds']['as_of'] == original_financial['as_of']
        if case == 'receipt_await_apply_terminal_fill':
            assert state['funds']['status'] == 'unknown'
    finally:
        if trigger:
            with sqlite3.connect(store.path) as connection:
                connection.execute('DROP TRIGGER IF EXISTS fail_plan_progress')
        if held is not None:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()


@pytest.mark.parametrize('outcome', ['live_before_cancel', 'terminal_allowed', 'terminal_rejected'])
def test_partial_fill_preserves_inventory_without_replanning(tmp_path, monkeypatch, outcome):
    from tests.test_lp_auto_target_convergence import publish_candidates, REWARDS
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'), budget='39')
    install_order_result_reader(exchange, monkeypatch)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    held = engine._lock_path.open('a+')
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    cancel, post = exchange.cancel_order, exchange.lp_post_order
    def fill():
        next(o for o in exchange.orders if o['order_id'] == 'original-A')['size_matched'] = '8'
        exchange.positions = [dict(token_id='A', condition_id='A', size='8', average_price='.39')]
        exchange.trades = [dict(trade_id='actual-A-fill', order_id='original-A', condition_id='A', token_id='A', side='BUY',
            size='8', price='.39', fee='0', status='CONFIRMED', timestamp=pool.NOW, trader_side='MAKER',
            taker_order_id='external-taker', maker_orders=[dict(order_id='original-A', token_id='A', side='BUY',
                price='.39', matched_amount='8', fee='0', maker_address='test-wallet', owner='credential-owner-uuid')])]
    def venue_cancel(oid):
        if oid == 'original-A' and outcome != 'live_before_cancel':
            fill()
        return cancel(oid)
    def venue_post(signed):
        if outcome == 'terminal_rejected' and signed['token_id'] == 'E':
            exchange.posts.append(signed)
            return dict(accepted=False, status='REJECTED', reason='balance_insufficient')
        return post(signed)
    monkeypatch.setattr(exchange, 'cancel_order', venue_cancel)
    monkeypatch.setattr(exchange, 'lp_post_order', venue_post)
    try:
        assert scheduler.run_due()
        saved = engine.lp_auto_state()
        assert saved['active_plan'] and exchange.posts == exchange.cancels == []
        assert [t['condition_id'] for t in saved['last_round']['targets']] == ['B', 'C', 'E', 'F', 'G']
        original_a = next(s for s in store.lp_sessions() if s['entry_order_id'] == 'original-A')
        original_protection = deepcopy(original_a['queue_protection'])
        if outcome == 'live_before_cancel':
            fill()
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
        held = None
        account, shared = exchange.lp_account_snapshot, exchange.lp_account_snapshot_shared
        forbidden = []
        def unavailable(*args, **kwargs):
            forbidden.append(pool.NOW)
            raise TimeoutError('balance/allowance unavailable during approved partial-fill execution')
        monkeypatch.setattr(exchange, 'lp_account_snapshot', unavailable)
        monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', unavailable)
        for _ in range(2):
            state = engine.lp_auto_state()
            monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
            assert scheduler.run_due()
            if not engine.lp_auto_state()['active_plan']:
                break
        state = engine.lp_auto_state()
        assert forbidden == []
        assert state['last_round']['round_id'] == saved['last_round']['round_id']
        assert state['last_round']['targets'] == saved['last_round']['targets']
        assert [a['action_id'] for a in state['last_round']['actions']] == [a['action_id'] for a in saved['last_round']['actions']]
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        session = engine.lp_status(original_a['session_id'])
        assert Decimal(session['buy_filled_quantity']) == 8
        assert Decimal(session['buy_cost']) == Decimal('3.12')
        assert Decimal(session['residual_quantity']) == 8
        assert exchange.positions == [dict(token_id='A', condition_id='A', size='8', average_price='.39')]
        assert all(p['side'] == 'BUY' and Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20 for p in exchange.posts)
        assert state['funds']['as_of'] == saved['funds']['as_of']
        if outcome == 'live_before_cancel':
            assert exchange.cancels == ['original-D', 'original-I']
            assert [p['token_id'] for p in exchange.posts] == ['E', 'F']
            assert live_orders(exchange)['A'] == 'original-A'
            assert store.lp_session(original_a['session_id'])['queue_protection'] == original_protection
            assert state['last_round']['actions'][-1]['reason'] == 'target_filled'
        else:
            assert exchange.cancels == ['original-A', 'original-D', 'original-I']
            assert [p['token_id'] for p in exchange.posts] == ['E', 'F', 'G']
            cancel_action = next(a for a in state['last_round']['actions'] if a.get('order_id') == 'original-A')
            assert cancel_action['state'] == 'success' and Decimal(cancel_action['filled_quantity']) == 8
            expected = {'B', 'C', 'E', 'F', 'G'} if outcome == 'terminal_allowed' else {'B', 'C', 'F', 'G'}
            assert set(live_orders(exchange)) == expected
            # Actual allowed exposure can be 39 BUYs + 3.12 inventory.
            actual_buys = sum(Decimal(o['price']) * (Decimal(o['original_size']) - Decimal(o['size_matched']))
                for o in exchange.orders if o['status'] == 'LIVE')
            assert actual_buys + Decimal(session['buy_cost']) == (Decimal('42.12')
                if outcome == 'terminal_allowed' else Decimal('34.32'))
            if outcome == 'terminal_rejected':
                rejected = next(a for a in state['last_round']['actions'] if a.get('condition_id') == 'E' and a['kind'] == 'buy')
                assert rejected['state'] == 'rejected' and rejected['reason'] == 'balance_insufficient'
        # Only the next complete financial read accounts for this inventory
        # while selecting a new affordable target set.
        completed = datetime.fromisoformat(state['last_round']['completed_at'])
        before = len(exchange.posts)
        monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=59))
        assert not scheduler.run_due() and len(exchange.posts) == before
        monkeypatch.setattr(exchange, 'lp_account_snapshot', account)
        monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', shared)
        monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=60))
        publish_candidates(exchange, lp, store, REWARDS)
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        assert state['last_round']['round_id'] != saved['last_round']['round_id']
        assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.12')
        if outcome != 'live_before_cancel':
            assert [t['condition_id'] for t in state['last_round']['targets']] == ['B', 'C', 'E', 'F']
        assert not any(p['side'] == 'SELL' for p in exchange.posts)
    finally:
        if held is not None:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()


@pytest.mark.parametrize('financial_change', ['ttl', 'generation'])
def test_execution_diagnostics_separate_planning_and_result_reads(runtime, monkeypatch, caplog, financial_change):
    import logging
    from polymarket.models.clob import RejectedOrder
    from tests.test_lp_auto_refill_contract import prepare, RefillPublic
    from tests.test_lp_account_reservation_reconciliation import _refill_identity
    from tests.test_lp_causal_diagnostics import events
    from tests.test_lp_read_diagnostics import wait_read_logs
    wait_read_logs()
    caplog.set_level(logging.INFO)
    public = RefillPublic(runtime.clock)
    public.rates = {1: '120', 2: '96', 3: '72'}
    store, _, account, lp, engine, _ = prepare(runtime, count=3, target=3, public=public)
    engine.lp_auto_set_desired_running(False)
    engine.lp_auto_configure(dict(budget_usd='39', target_buy_count=3, buy_price_level=2))
    engine.lp_auto_set_desired_running(True)
    balance, sign, post = account.get_balance_allowance, account.create_limit_order, account.post_order
    all_balance_calls, during_plan, captured = [], [], []
    def observe_balance(**kwargs):
        active = engine.lp_auto_state()['active_plan']
        all_balance_calls.append((runtime.clock[0], bool(active)))
        if active:
            during_plan.append((runtime.clock[0], kwargs))
            raise TimeoutError('actual SDK balance/allowance unavailable while public active_plan exists')
        return balance(**kwargs)
    def observe_sign(**kwargs):
        if not captured:
            state = engine.lp_auto_state()
            captured.append(deepcopy(state))
            assert state['active_plan'], 'Actual first preparation occurs after durable plan'
            if financial_change == 'ttl':
                runtime.clock[0] += timedelta(seconds=61)
            else:
                assert store.lp_advance_trade_generation(store.lp_trade_generation())
        return sign(**kwargs)
    def venue(signed):
        if signed.token_id == _refill_identity(2)[2]:
            account.posts.append(signed)
            return RejectedOrder(code='not_enough_balance', message='venue balance/allowance denied')
        if signed.token_id == _refill_identity(3)[2]:
            account.posts.append(signed)
            raise TimeoutError('actual original POST receipt unknown')
        return post(signed)
    monkeypatch.setattr(account, 'get_balance_allowance', observe_balance)
    monkeypatch.setattr(account, 'create_limit_order', observe_sign)
    monkeypatch.setattr(account, 'post_order', venue)
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert len(captured) == 1 and during_plan == []
    assert all_balance_calls and all(not active for _, active in all_balance_calls)
    assert len(account.posts) == 3
    assert [p.token_id for p in account.posts] == [_refill_identity(i)[2] for i in (1, 2, 3)]
    assert all(Decimal(p.maker_amount) / p.taker_amount == Decimal('.39')
        and Decimal(p.taker_amount) / 1000000 == 20 for p in account.posts)
    actions = state['active_plan']['actions']
    assert [a['state'] for a in actions] == ['success', 'rejected', 'unknown']
    assert actions[1]['reason'] == 'not_enough_balance'
    audits = {a['session_id']: store.lp_actions(a['session_id']) for a in actions}
    assert [audits[a['session_id']][0]['state'] for a in actions] == ['accepted', 'rejected', 'unknown']
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['as_of'] == captured[0]['funds']['as_of']
    assert state['runtime_state'] == 'running'
    assert state['reason'] == state['last_round']['reason'] == 'submission_unknown'
    assert state['plan_wait']['kind'] == 'order' and not state['last_round']['completed_at']
    open_orders = account.list_open_orders
    failed_at = []
    def unavailable(**kwargs):
        runtime.clock[0] += timedelta(seconds=3)
        failed_at.append(runtime.clock[0])
        raise TimeoutError('necessary actual SDK order result read failed')
    monkeypatch.setattr(account, 'list_open_orders', unavailable)
    runtime.clock[0] = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert failed_at and state['plan_wait']['kind'] == 'api'
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == failed_at[-1] + timedelta(seconds=60)
    assert state['runtime_state'] == 'running'
    assert state['reason'] == state['last_round']['reason'] == 'order_result_incomplete'
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == captured[0]['funds']['as_of']
    assert during_plan == [] and len(account.posts) == 3
    failed_end = failed_at[-1]
    calls = len(all_balance_calls)
    for elapsed in (1, 59):
        runtime.clock[0] = failed_end + timedelta(seconds=elapsed)
        scheduler.request_check()
        assert not scheduler.run_due()
        assert len(failed_at) == 1 and len(all_balance_calls) == calls
        assert len(account.posts) == 3 and during_plan == []
    monkeypatch.setattr(account, 'list_open_orders', open_orders)
    runtime.clock[0] = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['active_plan']['actions'][2]['state'] == 'unknown'
    assert state['runtime_state'] == 'running' and state['reason'] == 'submission_unknown'
    assert {sid: store.lp_actions(sid) for sid in audits} == audits
    assert during_plan == [] and len(account.posts) == 3
    wait_read_logs()
    assert not events(caplog, 'auto_presend_account_use') and not events(caplog, 'auto_send_account_use')
    planning = events(caplog, 'auto_account_use')
    assert len(planning) == 1
    assert planning[0]['checked_at'] == captured[0]['funds']['as_of']
    results = events(caplog, 'auto_order_result_use')
    assert len(results) == 1 and results[0]['read_started_at'] and results[0]['read_ended_at']
    assert datetime.fromisoformat(results[0]['read_ended_at']) > datetime.fromisoformat(planning[0]['read_ended_at'])
    assert state['funds']['as_of'] == captured[0]['funds']['as_of']
