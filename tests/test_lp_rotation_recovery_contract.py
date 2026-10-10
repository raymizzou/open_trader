"""Public offline contracts for receipt recovery and configured-level rotation."""
import fcntl
from datetime import timedelta
from datetime import datetime
from decimal import Decimal

import pytest

from tests import test_lp_auto_pool as pool
from tests import test_lp_auto_rotation as rotation


class RecoveryExchange(rotation.RotationExchange):
    def lp_account_snapshot(self):
        return {**super().lp_account_snapshot(),
                'open_orders': [o for o in self.orders if o['status'] not in {'CANCELED', 'FILLED', 'EXPIRED'}]}


def _delayed_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, 'Exchange', RecoveryExchange)
    engine, exchange, lp, store = pool.setup(tmp_path, 2)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    engine.lp_auto_set_desired_running(True)
    post = exchange.lp_post_order
    held = []

    def accepted_with_busy_apply_lock(signed):
        result = post(signed)
        handle = engine._lock_path.open('a+')
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(handle)
        return result

    exchange.lp_post_order = accepted_with_busy_apply_lock
    try:
        state = engine.lp_auto_run_once(round_id='delayed-entry')
    finally:
        for handle in held:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        exchange.lp_post_order = post
    session_id = state['intents'][0]['session_id']
    session = store.lp_session(session_id)
    assert session['submit_stage'] == 'sending'
    assert not lp.entry_send_inflight(session_id)
    action = next(a for a in store.lp_actions(session_id) if a.get('role') == 'entry')
    assert action['state'] == 'accepted' and action['submit_stage'] == 'receipt_received'
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
    def shared_account(**kwargs):
        return pool._fresh_registration_bundle(exchange, lp)

    exchange.lp_account_snapshot_shared = shared_account
    assert lp.reconcile_facts(session_id)[3] is None
    return engine, exchange, lp, store, session_id, session, action


def _assert_actual_sender_retains_reservation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    monkeypatch.setattr(pool, 'Exchange', RecoveryExchange)
    engine, exchange, lp, store = pool.setup(tmp_path, 2)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    engine.lp_auto_set_desired_running(True)
    entered, release = Event(), Event()
    post = exchange.lp_post_order

    def pending_venue_response(signed):
        result = post(signed)
        entered.set()
        assert release.wait(5), 'Independent sender watchdog'
        return result

    exchange.lp_post_order = pending_venue_response
    with ThreadPoolExecutor(1) as workers:
        running = workers.submit(engine.lp_auto_run_once, round_id='real-sender')
        try:
            assert entered.wait(2)
            session_id = store.lp_sessions()[0]['session_id']
            assert lp.entry_send_inflight(session_id)
            # A complete account read cannot cover a POST still in progress.
            lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))
            state = engine.lp_auto_run_once(round_id='while-sending')
            assert state['slots']['occupied'] >= 1
            assert Decimal(state['funds']['buy_reserved_usd']) >= 8
            assert not store.lp_session(session_id).get('reservation_coverage')
            assert state['funds']['status'] == 'unknown'
            assert len(exchange.posts) == 1 and exchange.cancels == []
        finally:
            release.set()
        running.result(timeout=5)
    assert not lp.entry_send_inflight(session_id)


@pytest.mark.parametrize('block', [None, 'independent-buy', 'independent-sell', 'identity', 'receipt-token', 'receipt-side', 'inflight'])
def test_verified_receipt_recovery_restores_rotation_without_erasing_audit(tmp_path, monkeypatch, block):
    if block == 'inflight':
        _assert_actual_sender_retains_reservation(tmp_path, monkeypatch)
        return
    engine, exchange, lp, store, session_id, original, action = _delayed_entry(tmp_path, monkeypatch)
    if block in ('independent-buy', 'independent-sell'):
        side = 'BUY' if block == 'independent-buy' else 'SELL'
        store.lp_upsert_action(session_id, 'independent-request', state='unknown', payload={
            'role': 'augment' if side == 'BUY' else 'passive_exit', 'side': side,
            'token_id': 'm00', 'price': '.40', 'quantity': '20', 'submit_stage': 'sending'})
    elif block == 'identity':
        store.lp_update_session(session_id, patch={'order_identity_conflict': True})
    elif block in {'receipt-token', 'receipt-side'}:
        payload = {**action, **({'token_id': 'other-token'} if block == 'receipt-token' else {'side': 'SELL'})}
        store.lp_upsert_action(session_id, action['action_key'], state='accepted', payload=payload)
        action = next(a for a in store.lp_actions(session_id) if a.get('role') == 'entry')
    exchange.rewards['m01'] = Decimal('30')
    rotation.refresh(lp, exchange, 2)
    state = engine.lp_auto_run_once(round_id='recovery-rotation')
    assert exchange.cancels == ([] if block else ['o1']), state['last_round']
    if block == 'independent-sell':
        records = state['last_round']['candidate_filter']['rotation_guards']
        assert records[0]['first_failing_predicate'] == 'independent_unresolved_action'
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] >= 1
    assert Decimal(state['funds']['buy_reserved_usd']) >= 8
    recovered = store.lp_session(session_id)
    for key in ('submit_stage', 'submit_requested_at', 'submit_post_started_at', 'submit_finished_at', 'submit_receipt_at'):
        assert recovered.get(key) == original.get(key)
    entry = next(a for a in store.lp_actions(session_id) if a.get('role') == 'entry')
    assert entry == action


def _level_pool(tmp_path, monkeypatch, level=2, count=1, target=1):
    monkeypatch.setattr(pool, 'Exchange', RecoveryExchange)
    engine, exchange, lp, store = pool.setup(tmp_path, count)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=target, buy_price_level=level))
    engine.lp_auto_set_desired_running(True)
    state = engine.lp_auto_run_once()
    assert len(exchange.posts) == min(count, target), state['last_round']
    return engine, exchange, lp, store


def _change_level_book(exchange, variant):
    direction = exchange.direction

    def changed(token):
        result = direction(token)
        if token != 'm00':
            return result
        if variant == 'remove':
            bids = [('.39', '1000'), ('.38', '1000')]
        elif variant == 'add':
            bids = [('.41', '1000'), ('.40', '1000'), ('.39', '1000')]
        elif variant == 'same':
            # Duplicate and zero-depth prices do not create another level.
            bids = [('.42', '0'), ('.40', '600'), ('.40', '400'), ('.39', '1000')]
        else:
            bids = [('.40', '1000'), ('.39', '1000')]
        result['book']['bids'] = [dict(price=p, size=q) for p, q in bids]
        result['book']['asks'] = [dict(price='.43', size='1000')]
        if variant == 'stale':
            result['book']['received_at'] = pool.NOW - timedelta(seconds=61)
        elif variant == 'unknown':
            result['book']['received_at'] = None
        return result

    exchange.direction = changed


@pytest.mark.parametrize('target', [1, 5])
@pytest.mark.parametrize('level, book, canceled', [
    (2, 'remove', True), (2, 'add', True), (1, 'add', True),
    (2, 'same', False), (2, 'unknown', False), (2, 'stale', False),
])
def test_configured_price_level_departure_cancels_before_reselection(tmp_path, monkeypatch, level, book, canceled, target):
    engine, exchange, lp, _ = _level_pool(tmp_path, monkeypatch, level, target=target)
    assert Decimal(exchange.posts[0]['price']) == (Decimal('.39') if level == 2 else Decimal('.40'))
    _change_level_book(exchange, book)
    assert engine.lp_auto_run_once()['last_round']['completed_at']
    assert exchange.cancels == []
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']))
    state = engine.lp_auto_run_once()
    assert exchange.cancels == (['o1'] if canceled else []), state['last_round']
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1


@pytest.mark.parametrize('winner, fill', [('same', '0'), ('other', '0'), ('same', '8')])
def test_cancel_confirmation_precedes_global_refill_and_can_reselect_same_market(tmp_path, monkeypatch, winner, fill):
    engine, exchange, lp, _ = _level_pool(tmp_path, monkeypatch, count=2)
    _change_level_book(exchange, 'add')
    assert engine.lp_auto_run_once()['last_round']['completed_at']
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']))
    engine.lp_auto_run_once()
    assert exchange.cancels == ['o1']
    # ACK alone leaves the old BUY live and prevents a second order.
    state = engine.lp_auto_run_once()
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.8')
    if winner == 'other':
        exchange.rewards['m01'] = Decimal('100')
    else:
        exchange.rewards['m00'] = Decimal('100')
    exchange.orders[0].update(status='CANCELED', size_matched=fill)
    if fill != '0':
        exchange.positions = [dict(token_id='m00', condition_id='m00', size=fill)]
    rotation.refresh(lp, exchange, 2)
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']))
    state = engine.lp_auto_run_once()
    if fill != '0':
        assert len(exchange.posts) == 1
        assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.12')
        assert Decimal(state['funds']['available_usd']) == Decimal('96.88')
        assert state['last_round']['reason'] == 'rotation_filled'
    else:
        # The originally selected m01 executes before the later ranking change.
        assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01'], str(state['last_round'])
        assert Decimal(exchange.posts[-1]['price']) == Decimal('.39')
        assert exchange.orders[-1]['order_id'] != 'o1'
        assert state['slots']['occupied'] == 1
        if winner == 'same':
            monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
            rotation.refresh(lp, exchange, 2)
            state = engine.lp_auto_run_once()
            assert exchange.cancels[-1] == exchange.orders[-1]['order_id']
            exchange.orders[-1]['status'] = 'CANCELED'
            monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
            state = engine.lp_auto_run_once()
            assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01', 'm00']
            assert Decimal(exchange.posts[-1]['price']) == Decimal('.40')
            assert exchange.orders[-1]['order_id'] not in {'o1', 'o2'}
            assert state['slots']['occupied'] == 1


def test_full_pool_greedy_ranking_skips_unaffordable_candidates(tmp_path, monkeypatch):
    engine, exchange, lp, _ = _level_pool(tmp_path, monkeypatch, level=1, count=11, target=5)
    direction = exchange.direction
    sizes = {'m05': '320', 'm06': '120', 'm07': '40', 'm08': '20', 'm09': '20', 'm10': '20'}
    rewards = {'m05': '6000', 'm06': '2000', 'm07': '1000', 'm08': '500', 'm09': '400', 'm10': '300'}

    def affordable_book(token):
        result = direction(token)
        if token in sizes:
            result['market'].update(minimum_order_size=Decimal(sizes[token]), reward_min_size=Decimal(sizes[token]))
            result['book'].update(bids=[dict(price='.25', size='1000'), dict(price='.24', size='1000')],
                                  asks=[dict(price='.26', size='1000')])
        return result

    exchange.direction = affordable_book
    exchange.rewards.update({k: Decimal(v) for k, v in rewards.items()})
    # Reward catalog is a separate venue boundary and carries the same minimum.
    catalog = exchange.lp_reward_catalog

    def reward_catalog(**kwargs):
        result = catalog(**kwargs)
        for row in result['markets']:
            if row['condition_id'] in sizes:
                row['rewards_min_size'] = sizes[row['condition_id']]
        return result

    exchange.lp_reward_catalog = reward_catalog
    rotation.refresh(lp, exchange, 11)
    state = engine.lp_auto_run_once()
    assert [r['condition_id'] for r in state['last_round']['candidates']] == list(sizes)
    assert [r['condition_id'] for r in state['last_round']['targets']] == ['m05', 'm07', 'm08', 'm09'], state['last_round']
    assert set(exchange.cancels) == {'o1', 'o2', 'o3', 'o4', 'o5'}
    assert len(exchange.posts) == 5
    assert state['slots']['occupied'] == 5
    assert any(r['reason'] == 'rotation_budget_insufficient' for r in state['last_round']['blocked'])
    for order in exchange.orders:
        order['status'] = 'CANCELED'
    state = engine.lp_auto_run_once()
    assert [p['token_id'] for p in exchange.posts[5:]] == ['m05', 'm07', 'm08', 'm09']
    assert state['slots']['occupied'] == 4
    assert Decimal(state['funds']['buy_reserved_usd']) == 100


def test_independent_rotation_continues_while_one_order_reconciles(tmp_path, monkeypatch):
    engine, exchange, lp, _ = _level_pool(tmp_path, monkeypatch, level=1, count=10, target=5)
    exchange.rewards.update({f'm{i:02}': Decimal('100') for i in range(5, 10)})
    rotation.refresh(lp, exchange, 10)
    snapshot = exchange.lp_snapshot

    def temporary_market_wait(request):
        if request['token_id'] == 'm00':
            raise ValueError('order_read_failed')
        return snapshot(request)

    exchange.lp_snapshot = temporary_market_wait
    state = engine.lp_auto_run_once()
    assert set(exchange.cancels) == {'o2', 'o3', 'o4', 'o5'}, state['last_round']
    assert len(exchange.posts) == 5
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    for order in exchange.orders[1:]:
        order['status'] = 'CANCELED'
    state = engine.lp_auto_run_once()
    assert state['slots']['occupied'] == 5
    assert len(exchange.posts) == 9
    assert exchange.orders[0]['status'] == 'LIVE'
    # Ordinary public reconciliation recovers the retained order; no toggle/restart.
    exchange.lp_snapshot = snapshot
    engine.lp_auto_reconcile_unknown()
    state = engine.lp_auto_run_once()
    assert set(exchange.cancels) == {'o1', 'o2', 'o3', 'o4', 'o5'}, state['last_round']
    assert len(exchange.posts) == 9
    exchange.orders[0]['status'] = 'CANCELED'
    state = engine.lp_auto_run_once()
    assert {o['token_id'] for o in exchange.orders if o['status'] == 'LIVE'} == {f'm{i:02}' for i in range(5, 10)}
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40


@pytest.mark.parametrize('sink', ['delayed', 'broken', 'overflow'])
def test_rotation_diagnostics_capture_actual_block_and_recovery_inputs(tmp_path, monkeypatch, sink):
    import hashlib
    import json
    import threading
    from open_trader import polymarket_lp_auto as auto_module
    from open_trader import polymarket_trading as trading_module

    engine, exchange, lp, store, session_id, _, _ = _delayed_entry(tmp_path, monkeypatch)
    store.lp_update_session(session_id, state='needs_attention', patch={'facts_error': 'market_read_cooling_down', 'resume_state': 'entry_open'})
    exchange.rewards['m01'] = Decimal('100')
    rotation.refresh(lp, exchange, 2)
    emitted = []
    entered, release = threading.Event(), threading.Event()

    def output(message, *args, **kwargs):
        if not message.startswith('lp_rotation_guard '):
            return
        entered.set()
        assert release.wait(5)
        if sink == 'broken':
            raise RuntimeError('offline diagnostic sink failure')
        emitted.append(json.loads(args[0]))

    # The output boundary can stall or fail; the real bounded queue remains in use.
    if hasattr(auto_module, 'logger'):
        monkeypatch.setattr(auto_module.logger, 'info', output)
    try:
        state = engine.lp_auto_run_once(round_id='blocked-diagnostic-round')
        assert exchange.cancels == []
        records = state['last_round']['candidate_filter'].get('rotation_guards', [])
        assert records, 'missing actual rotation predicate diagnostic'
        blocked = records[0]
        assert blocked['first_failing_predicate'] == 'session_not_entry_open'
        assert blocked['predicate_inputs']['session_not_entry_open'] is True
        assert blocked['round_id'] == hashlib.sha256(b'blocked-diagnostic-round').hexdigest()[:16]
        assert blocked['account_checked_at'] and blocked['account_generation'] is not None
        assert blocked['predicate_inputs']['unresolved_submission'] is False
        assert session_id not in str(blocked) and 'o1' not in str(blocked)
        assert entered.wait(2)
        if sink == 'overflow':
            with trading_module._lp_read_log_lock:
                before = trading_module._lp_read_log_dropped
            for _ in range(40):
                trading_module._lp_capture_read_log(lambda: None)
            assert trading_module._lp_read_log_queue.qsize() <= 32
            with trading_module._lp_read_log_lock:
                assert trading_module._lp_read_log_dropped > before
        # Change persisted state after decision capture, before deferred output.
        engine.lp_tick()
        recovered = engine.lp_auto_run_once(round_id='recovered-diagnostic-round')
        assert exchange.cancels == ['o1'], recovered['last_round']
        assert len(exchange.posts) == 1
        assert recovered['slots']['occupied'] == 1
        recovery = recovered['last_round']['candidate_filter']['rotation_guards']
        assert any(r['outcome'] == 'recovered' and not r['predicate_inputs']['unresolved_submission'] for r in recovery)
    finally:
        release.set()
        queue = trading_module._lp_read_log_queue
        with queue.all_tasks_done:
            assert queue.all_tasks_done.wait_for(lambda: queue.unfinished_tasks == 0, timeout=5)
    if sink == 'delayed':
        logged = next(r for r in emitted if r['round_id'] == hashlib.sha256(b'blocked-diagnostic-round').hexdigest()[:16])
        assert logged == blocked


@pytest.mark.parametrize('restriction', ['funds', 'missing-configured-level'])
def test_off_level_cancel_does_not_require_replacement_admission(tmp_path, monkeypatch, restriction):
    engine, exchange, lp, _ = _level_pool(tmp_path, monkeypatch)
    assert Decimal(exchange.posts[0]['price']) == Decimal('.39')
    assert Decimal(exchange.posts[0]['quantity']) == 20
    if restriction == 'funds':
        _change_level_book(exchange, 'add')
        account = exchange.lp_account_snapshot
        exchange.lp_account_snapshot = lambda: {**account(), 'balance': '7.80', 'allowance': '7.80'}
    else:
        direction = exchange.direction

        def only_positive_buy_one(token):
            result = direction(token)
            result['book']['bids'] = [dict(price='.39', size='1000')]
            return result

        exchange.direction = only_positive_buy_one
    state = engine.lp_auto_run_once(round_id='off-level-without-admission')
    assert exchange.cancels == ['o1'], state['last_round']
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.80')
    # ACK keeps the exact original hold until the venue proves terminal facts.
    state = engine.lp_auto_run_once()
    assert exchange.cancels == ['o1']
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.80')
    exchange.orders[0]['status'] = 'CANCELED'
    state = engine.lp_auto_run_once()
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 0
    assert Decimal(state['funds']['buy_reserved_usd']) == 0


@pytest.mark.parametrize('change, predicate', [('stop', 'stop_requested'), ('protection', 'rotation_protection_active')])
def test_final_precancel_guard_logs_its_actual_blocking_inputs(tmp_path, monkeypatch, change, predicate):
    import hashlib

    engine, exchange, lp, store = _level_pool(tmp_path, monkeypatch, level=1, count=2)
    exchange.rewards['m01'] = Decimal('25')
    rotation.refresh(lp, exchange, 2)
    session_id = engine.lp_auto_state()['intents'][0]['session_id']
    read = exchange.lp_reward_catalog

    def change_after_initial_guard(**kwargs):
        if change == 'stop':
            patch = {'stop_requested': True}
        else:
            protection = store.lp_session(session_id)['queue_protection']
            for bucket in protection.get('levels', {'entry': protection}).values():
                bucket['state'] = 'triggered'
            patch = {'queue_protection': protection}
        store.lp_update_session(session_id, patch=patch)
        return read(**kwargs)

    exchange.lp_reward_catalog = change_after_initial_guard
    state = engine.lp_auto_run_once(round_id='final-precancel-guard')
    assert exchange.cancels == []
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    assert state['last_round']['reason'] == ('rotation_awaiting_reconciliation' if change == 'stop' else predicate)
    records = state['last_round']['candidate_filter']['rotation_guards']
    assert any(r['outcome'] == 'eligible' and not r['predicate_inputs'].get(predicate, False) for r in records)
    final = [r for r in records if r['outcome'] == 'blocked' and r['first_failing_predicate'] == predicate]
    assert final, records
    assert final[-1]['predicate_inputs'][predicate] is True
    assert final[-1]['reason'] == state['last_round']['reason']
    assert final[-1]['round_id'] == hashlib.sha256(b'final-precancel-guard').hexdigest()[:16]
    assert final[-1]['order_id'] == hashlib.sha256(b'o1').hexdigest()[:16]
    assert final[-1]['session_id'] == hashlib.sha256(session_id.encode()).hexdigest()[:16]


@pytest.mark.parametrize('source_stale', ['reward', 'book'])
def test_confirmed_level_departure_uses_cancel_fact_freshness(tmp_path, monkeypatch, source_stale):
    engine, exchange, lp, _ = _level_pool(tmp_path, monkeypatch)
    _change_level_book(exchange, 'add')
    if source_stale == 'reward':
        catalog = exchange.lp_reward_catalog

        def expired_reward_receipt(**kwargs):
            result = catalog(**kwargs)
            for row in result['markets']:
                row['reward_checked_at'] = pool.NOW - timedelta(seconds=61)
            return result

        exchange.lp_reward_catalog = expired_reward_receipt
    else:
        books = exchange.lp_order_books

        def expired_book_receipt(ids, **kwargs):
            result = books(ids, **kwargs)
            for book in result.values():
                book['received_at'] = pool.NOW - timedelta(seconds=61)
            return result

        exchange.lp_order_books = expired_book_receipt
    state = engine.lp_auto_run_once(round_id='cancel-fact-freshness')
    expected_cancels = ['o1'] if source_stale == 'reward' else []
    assert exchange.cancels == expected_cancels, state['last_round']
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.80')
    state = engine.lp_auto_run_once()
    assert exchange.cancels == expected_cancels
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.80')
    if source_stale == 'reward':
        exchange.orders[0]['status'] = 'CANCELED'
        state = engine.lp_auto_run_once()
        assert exchange.cancels == ['o1']
        assert len(exchange.posts) == 1
        assert state['slots']['occupied'] == 0
        assert Decimal(state['funds']['buy_reserved_usd']) == 0
