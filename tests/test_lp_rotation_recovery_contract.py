"""Public offline contracts for receipt recovery and configured-level rotation."""

from tests.test_lp_auto_pool import advance_auto_wait
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
    from tests.test_lp_auto_plan_execution import _native_unbound_receipt
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    monkeypatch.setattr(pool, 'Exchange', RecoveryExchange)
    engine, exchange, lp, store = pool.setup(tmp_path, 2)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    engine.lp_auto_set_desired_running(True)
    with _native_unbound_receipt(store, 'm00'):
        state = engine.lp_auto_run_once(round_id='delayed-entry')
    session_id = state['intents'][0]['session_id']
    session = store.lp_session(session_id)
    assert session['submit_stage'] == 'sending' and not session.get('entry_order_id')
    assert not lp.entry_send_inflight(session_id)
    action, = [a for a in store.lp_actions(session_id) if a.get('role') == 'entry']
    assert action['state'] == 'accepted' and action['submit_stage'] == 'receipt_received'
    assert action['order_id'] == 'o1' and action['post_started'] is True
    assert len(exchange.posts) == 1
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
    exchange.lp_account_snapshot_shared = lambda **kwargs: pool._fresh_registration_bundle(exchange, lp)
    assert lp.reconcile_facts(session_id)[3] is None
    assert next(a for a in store.lp_actions(session_id) if a['action_key'] == action['action_key']) == action
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
    from uuid import UUID
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    if block == 'inflight':
        _assert_actual_sender_retains_reservation(tmp_path, monkeypatch)
        return
    engine, exchange, lp, store, session_id, original, action = _delayed_entry(tmp_path, monkeypatch)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    engine.lp_auto_run_once()
    advance_auto_wait(engine, monkeypatch)
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
    if block == 'independent-sell':
        # Ordinary automatic planning reads actual current account inputs,
        # not the full-history/reporting carrier from the normal fixture.
        def current_only(*, trade_generation_provider, retry_seconds, previous_components):
            packet = pool._fresh_registration_bundle(exchange, lp)
            packet.update(trade_generation=trade_generation_provider(),
                trades_complete=False, raw_trades=None, history_state='not_read',
                current_pagination_complete=True)
            return packet
        exchange._lp_planning_account_snapshot = current_only
    state = engine.lp_auto_run_once(round_id='recovery-rotation')
    if block is None:
        cancel, = _wait_round_cancels(store, state)
        assert cancel['order_id'] == 'o1' and cancel['session_id'] == session_id
        assert cancel['rotation_round_id'] == 'recovery-rotation' and cancel['state'] == 'accepted'
        assert cancel['post_started'] is True and UUID(cancel['action_id'])
        assert cancel['action_key'].startswith(session_id + ':reconciliation-cancel:')
        assert next(a for a in store.lp_actions(session_id) if a['action_key'] == cancel['action_key']) == cancel
    else:
        assert not state['last_round'].get('cancel_registration')
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
    monkeypatch.setattr(pool, "NOW", pool.NOW)
    engine, exchange, lp, store = _level_pool(tmp_path, monkeypatch, level, target=target)
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    assert Decimal(exchange.posts[0]['price']) == (Decimal('.39') if level == 2 else Decimal('.40'))
    _change_level_book(exchange, book)
    immediate = engine.lp_auto_run_once()
    assert immediate['last_round']['completed_at']
    _wait_round_cancels(store, immediate)
    assert exchange.cancels == (['o1'] if canceled else [])
    state = immediate
    assert exchange.cancels == (['o1'] if canceled else []), state['last_round']
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1


@pytest.mark.parametrize('winner, fill', [('same', '0'), ('other', '0'), ('same', '8')])
def test_cancel_confirmation_precedes_global_refill_and_can_reselect_same_market(tmp_path, monkeypatch, winner, fill):
    monkeypatch.setattr(pool, "NOW", pool.NOW)
    import json
    from copy import deepcopy
    engine, exchange, lp, store = _level_pool(tmp_path, monkeypatch, count=2)
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    _change_level_book(exchange, 'add')
    cancel_state = engine.lp_auto_run_once()
    assert cancel_state['last_round']['completed_at']
    _wait_round_cancels(store, cancel_state)
    assert exchange.cancels == ['o1']
    # ACK alone leaves the old BUY live and prevents a second order.
    state = engine.lp_auto_run_once(round_id=cancel_state['last_round']['round_id'])
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.8')
    original, = [i for i in state['intents'] if i['order_id'] == 'o1']
    original_audits = deepcopy(store.lp_actions(original['session_id']))
    if winner == 'other':
        exchange.rewards['m01'] = Decimal('100')
    else:
        exchange.rewards['m00'] = Decimal('100')
    exchange.orders[0].update(status='CANCELED', size_matched=fill)
    if fill != '0':
        exchange.positions = [dict(token_id='m00', condition_id='m00', size='8', average_price='.39')]
        exchange.trades = [dict(trade_id='actual-o1-fill', status='CONFIRMED', trader_side='MAKER',
            taker_order_id='external-taker', token_id='m00', condition_id='m00', side='BUY', size='8', price='.39',
            fee='0', timestamp=pool.NOW, maker_orders=[dict(order_id='o1', token_id='m00', side='BUY',
                price='.39', matched_amount='8', fee='0', maker_address='test-wallet')])]
    rotation.refresh(lp, exchange, 2)
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
    state = engine.lp_auto_run_once()
    if fill != '0':
        assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01']
        assert exchange.posts[-1]['price'] == Decimal('.39') and exchange.posts[-1]['quantity'] == 20
        assert state['slots']['occupied'] == 1
        assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.8')
        assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.12')
        assert state['funds']['status'] == 'known' and state['funds']['as_of']
        assert Decimal(state['funds']['available_usd']) == Decimal('89.08')
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        assert state['plan_wait'] is None
        assert all(p['side'] == 'BUY' for p in exchange.posts)
    elif winner == 'same':
        assert [p['token_id'] for p in exchange.posts] == ['m00', 'm00'], state['last_round']
        assert exchange.posts[-1]['price'] == Decimal('.40') and exchange.posts[-1]['quantity'] == 20
        assert state['slots']['occupied'] == 1 and Decimal(state['funds']['buy_reserved_usd']) == 8
        new_action, = [a for a in state['last_round']['actions'] if a['kind'] == 'buy']
        assert new_action['session_id'] != original['session_id'] and new_action['order_id'] != 'o1'
        new_audit, = [a for a in store.lp_actions(new_action['session_id']) if a.get('role') == 'entry']
        original_entry, = [a for a in original_audits if a.get('role') == 'entry']
        assert new_audit['action_id'] != original_entry['action_id'] and new_audit['action_key'] != original_entry['action_key']
        assert state['active_plan'] is None and state['last_round']['completed_at'] and state['plan_wait'] is None
        current_oid = new_action['order_id']
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
        rotation.refresh(lp, exchange, 2)
        state = engine.lp_auto_run_once()
        assert [p['token_id'] for p in exchange.posts] == ['m00', 'm00']
        assert exchange.cancels == ['o1']
        assert next(o for o in exchange.orders if o['status'] == 'LIVE')['order_id'] == current_oid
        assert state['slots']['occupied'] == 1 and Decimal(state['funds']['buy_reserved_usd']) == 8
    else:
        # The other winner's fresh current quote retains its original expectations.

        assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01'], str(state['last_round'])
        assert Decimal(exchange.posts[-1]['price']) == Decimal('.39')
        assert exchange.orders[-1]['order_id'] != 'o1'
        assert state['slots']['occupied'] == 1
    assert store.lp_actions(original['session_id']) == original_audits
    (tmp_path / 'CI-C-dependent-reselect-proof.json').write_text(json.dumps(dict(
        state=state, original=original, original_audits=original_audits,
        sessions=store.lp_sessions(), posts=exchange.posts, cancels=exchange.cancels), default=str, indent=2))


def test_full_pool_greedy_ranking_skips_unaffordable_candidates(tmp_path, monkeypatch):
    from collections import Counter
    engine, exchange, lp, store = _level_pool(tmp_path, monkeypatch, level=1, count=11, target=5)
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
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
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert [r['condition_id'] for r in state['last_round']['candidates']] == list(sizes)
    assert [r['condition_id'] for r in state['last_round']['targets']] == ['m05', 'm07', 'm08', 'm09'], state['last_round']
    _wait_round_cancels(store, state)
    assert set(exchange.cancels) == {'o1', 'o2', 'o3', 'o4', 'o5'}
    assert len(exchange.posts) == 5
    assert state['slots']['occupied'] == 5
    assert any(r['reason'] == 'rotation_budget_insufficient' for r in state['last_round']['blocked'])
    for order in exchange.orders:
        order['status'] = 'CANCELED'
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert Counter(p['token_id'] for p in exchange.posts[5:]) == Counter(['m05', 'm07', 'm08', 'm09'])
    assert state['slots']['occupied'] == 4
    assert Decimal(state['funds']['buy_reserved_usd']) == 100


def test_current_order_facts_override_stale_session_snapshot_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, "NOW", pool.NOW)
    import json
    from collections import Counter
    from copy import deepcopy
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    engine, exchange, lp, store = _level_pool(tmp_path, monkeypatch, level=1, count=10, target=5)
    install_order_result_reader(exchange, monkeypatch)
    original_intents = {i['token_id']: deepcopy(i) for i in engine.lp_auto_state()['intents']}
    assert set(original_intents) == {f'm{i:02}' for i in range(5)}
    binding = {}
    for token, intent in original_intents.items():
        session = store.lp_session(intent['session_id'])
        audit, = [a for a in store.lp_actions(intent['session_id']) if a.get('role') == 'entry']
        assert session['entry_order_id'] == intent['order_id'] == audit['order_id']
        assert session['token_id'] == token and audit['state'] == 'accepted'
        binding[token] = deepcopy(dict(intent=intent, session=session, audit=audit))
    retained_id = original_intents['m00']['order_id']
    sid = original_intents['m00']['session_id']
    other_ids = [original_intents[f'm{i:02}']['order_id'] for i in range(1, 5)]
    original_ids = [retained_id, *other_ids]
    for order in exchange.orders:
        assert order['order_id'] in original_ids and order['status']=='LIVE' and order['side']=='BUY'
        assert Decimal(order['size_matched'])==0 and Decimal(order['original_size'])==20
        assert Decimal(order['price'])==Decimal('.40')
        order['fill_quantity_known'] = True
    (tmp_path / 'CI-B-native-binding.json').write_text(json.dumps(binding, default=str, indent=2))
    exchange.rewards.update({f'm{i:02}': Decimal('100') for i in range(5, 10)})
    rotation.refresh(lp, exchange, 10)
    snapshot = exchange.lp_snapshot
    monitoring_reads, accounts, rounds = [], [], []
    def temporary_market_wait(request):
        monitoring_reads.append(deepcopy(request))
        if request['token_id'] == 'm00':
            raise ValueError('order_read_failed')
        return snapshot(request)
    exchange.lp_snapshot = temporary_market_wait
    failed = lp.reconcile_facts(sid)
    assert monitoring_reads and monitoring_reads[-1]['token_id']=='m00'
    assert failed[3]=='external_snapshot_unknown'
    failed_session = store.lp_session(sid)
    assert failed_session['facts_error']=='external_snapshot_unknown'
    assert failed_session['entry_order_id']==retained_id and failed_session['token_id']=='m00'
    assert store.lp_actions(sid)==[binding['m00']['audit']]
    def complete_account(*, max_age_seconds=0, trade_generation_provider=None):
        del max_age_seconds
        started = pool.NOW
        packet = pool._fresh_registration_bundle(exchange, lp)
        packet['open_orders'] = deepcopy([o for o in exchange.orders if o['status']=='LIVE'])
        pool.NOW += timedelta(microseconds=1)
        packet.update(read_started_at=started, read_ended_at=pool.NOW, checked_at=pool.NOW)
        if trade_generation_provider is not None:
            packet['trade_generation'] = trade_generation_provider()
        accounts.append(deepcopy(packet))
        return packet
    exchange.lp_account_snapshot_shared = complete_account
    def closed(state, expected_ids):
        assert state['active_plan'] is None and state['last_round']['completed_at']
        assert all(a['kind']=='buy' for a in state['last_round']['actions'])
        rows = _wait_round_cancels(store, state)
        assert Counter(a['order_id'] for a in rows)==Counter(expected_ids)
        assert all(a['state']=='accepted' and a['post_started'] is True for a in rows)
        assert all(a['rotation_round_id']==state['last_round']['round_id'] for a in rows)
        keys = {a['action_key'] for prior in rounds for a in prior}
        assert all(a['action_key'] not in keys for a in rows)
        for prior in rounds:
            for audit in prior:
                assert next(a for a in store.lp_actions(audit['session_id']) if a['action_key']==audit['action_key'])==audit
        rounds.append(deepcopy(rows))
        assert state['slots']['occupied']==5 and Decimal(state['funds']['buy_reserved_usd'])==40
        completed = datetime.fromisoformat(state['last_round']['completed_at'])
        assert state['plan_wait'] is None
        before = len(accounts), deepcopy(exchange.posts), Counter(exchange.cancels)
        for elapsed in (1,29):
            monkeypatch.setattr(pool,'NOW',completed+timedelta(seconds=elapsed))
            engine.lp_auto_run_once(round_id=state['last_round']['round_id'])
            assert len(accounts) >= before[0]
            assert (exchange.posts, Counter(exchange.cancels)) == before[1:]
            for prior in rounds:
                for audit in prior:
                    assert next(a for a in store.lp_actions(audit['session_id']) if a['action_key']==audit['action_key'])==audit
        return rows
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once(round_id='known-current-despite-stale-monitoring')
    first = closed(state, original_ids)
    packet = accounts[0]
    assert all(packet[key] is True for key in ('authenticated','balance_complete','open_orders_complete',
        'positions_complete','trades_complete','pagination_complete'))
    assert packet['wallet_address']==packet['account_id']=='test-wallet'
    assert {o['order_id'] for o in packet['open_orders']}==set(original_ids)
    assert all(o['status']=='LIVE' and o['side']=='BUY' and o['fill_quantity_known'] is True
        and Decimal(o['price'])==Decimal('.40') and Decimal(o['original_size'])==20
        and Decimal(o['size_matched'])==0 for o in packet['open_orders'])
    assert packet['positions']==packet['raw_trades']==[]
    assert packet['read_started_at']<=packet['checked_at']==packet['read_ended_at']
    assert Counter(exchange.cancels)==Counter(original_ids) and len(exchange.posts)==5
    assert {o['order_id'] for o in exchange.orders if o['status']=='LIVE'}==set(original_ids)
    for order in exchange.orders:
        if order['order_id'] in other_ids:
            order['status']='CANCELED'
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once(round_id='four-real-terminal-one-current-live')
    second = closed(state, [retained_id])
    assert Counter(exchange.cancels)==Counter(original_ids+[retained_id])
    assert len(exchange.posts)==9
    assert next(o for o in exchange.orders if o['order_id']==retained_id)['status']=='LIVE'
    current_m00, = [row for row in state['account_buys'] if row['order_id']==retained_id]
    assert Decimal(current_m00['reserved_usd'])==8 and current_m00['session_id']==sid
    # Normal public monitoring recovers on its original external snapshot boundary.
    exchange.lp_snapshot = snapshot
    reconciled = lp.reconcile_facts(sid)
    assert reconciled[3] is None
    assert store.lp_session(sid)['order_history'][retained_id]['status']=='LIVE'
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once(round_id='fresh-live-still-outside-selection')
    third = closed(state, [retained_id])
    assert Counter(exchange.cancels)==Counter(original_ids+[retained_id,retained_id])
    assert len(exchange.posts)==9
    assert first[0]['rotation_round_id']!=second[0]['rotation_round_id']!=third[0]['rotation_round_id']
    next(o for o in exchange.orders if o['order_id']==retained_id)['status']='CANCELED'
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once(round_id='actual-last-terminal-final-refill')
    closed(state, [])
    assert {o['token_id'] for o in exchange.orders if o['status']=='LIVE'}=={f'm{i:02}' for i in range(5,10)}
    assert len(exchange.posts)==10 and all(p['side']=='BUY' for p in exchange.posts)
    assert Counter(exchange.cancels)==Counter(original_ids+[retained_id,retained_id])
    replacements = {o['order_id'] for o in exchange.orders if o['status']=='LIVE'}
    assert replacements.isdisjoint(original_ids) and len(replacements)==5
    for token, original in binding.items():
        assert next(a for a in store.lp_actions(original['session']['session_id']) if a['action_key']==original['audit']['action_key'])==original['audit']
    (tmp_path/'CI-current-vs-stale-monitoring.json').write_text(json.dumps(dict(
        failed_session=failed_session,monitoring_reads=monitoring_reads,accounts=accounts,rounds=rounds,
        state=state,orders=exchange.orders,posts=exchange.posts,cancels=exchange.cancels),default=str,indent=2)+'\n')


@pytest.mark.parametrize('sink', ['delayed', 'broken', 'overflow'])
def test_rotation_diagnostics_capture_actual_block_and_recovery_inputs(tmp_path, monkeypatch, sink):
    import hashlib
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from copy import deepcopy
    import json
    import threading
    from open_trader import polymarket_lp_auto as auto_module
    from open_trader import polymarket_trading as trading_module

    engine, exchange, lp, store, session_id, _, _ = _delayed_entry(tmp_path, monkeypatch)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    engine.lp_auto_run_once()
    advance_auto_wait(engine, monkeypatch)
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
        request, = _wait_round_cancels(store, state)
        assert request['order_id']=='o1' and request['state']=='accepted'
        assert exchange.cancels == ['o1'] and len(exchange.posts)==1
        records = state['last_round']['candidate_filter'].get('rotation_guards', [])
        assert records, 'missing actual rotation predicate diagnostic'
        blocked = records[0]
        assert blocked['outcome']=='recovered' and blocked['first_failing_predicate'] is None
        assert blocked['predicate_inputs']['session_not_entry_open'] is False
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
        advance_auto_wait(engine, monkeypatch)
        engine.lp_tick()
        exchange.rewards['m00']=Decimal('200')
        rotation.refresh(lp, exchange, 2)
        recovered = engine.lp_auto_run_once(round_id='recovered-diagnostic-round')
        assert exchange.cancels == ['o1'], recovered['last_round']
        assert len(exchange.posts) == 1
        assert recovered['slots']['occupied'] == 1
        recovery = recovered['last_round']['candidate_filter']['rotation_guards']
        current, = [r for r in recovery if r['outcome']=='recovered' and not r['predicate_inputs']['unresolved_submission']]
        assert current['round_id']==hashlib.sha256(b'recovered-diagnostic-round').hexdigest()[:16]
        assert current['account_checked_at']!=blocked['account_checked_at']
        assert current['first_failing_predicate'] is None
        assert session_id not in str(current) and 'test-wallet' not in str(current)
        assert blocked['round_id']==hashlib.sha256(b'blocked-diagnostic-round').hexdigest()[:16]
        assert recovered['active_plan'] is None and recovered['last_round']['completed_at']
        assert all(p['side']=='BUY' for p in exchange.posts)
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
    monkeypatch.setattr(pool, "NOW", pool.NOW)
    engine, exchange, lp, store = _level_pool(tmp_path, monkeypatch)
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
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
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once(round_id='off-level-without-admission')
    _wait_round_cancels(store, state)
    assert exchange.cancels == ['o1'], state['last_round']
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.80')
    # ACK keeps the exact original hold until the venue proves terminal facts.
    original_requests = _wait_round_cancels(store, state)
    original_audits = [dict(a) for a in original_requests]
    state = engine.lp_auto_run_once()
    fresh_requests = _wait_round_cancels(store, state)
    assert len(fresh_requests) == 1 and fresh_requests[0]['order_id'] == 'o1'
    assert {a['action_key'] for a in fresh_requests}.isdisjoint(a['action_key'] for a in original_audits)
    assert all(next(a for a in store.lp_actions(old['session_id']) if a['action_key'] == old['action_key']) == old for old in original_audits)
    assert exchange.cancels == ['o1', 'o1']
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.80')
    exchange.orders[0]['status'] = 'CANCELED'
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 0
    assert Decimal(state['funds']['buy_reserved_usd']) == 0


@pytest.mark.parametrize('change, predicate', [('stop', 'stop_requested'), ('protection', 'rotation_protection_active')])
def test_selected_independent_cancel_preserves_late_business_controls(tmp_path, monkeypatch, change, predicate):
    import hashlib, json, sqlite3
    from copy import deepcopy
    from pathlib import Path
    from threading import Event
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    monkeypatch.setattr(pool,'NOW',pool.NOW)
    engine,exchange,lp,store=_level_pool(tmp_path,monkeypatch,level=1,count=2)
    read=install_order_result_reader(exchange,monkeypatch)
    advance_auto_wait(engine,monkeypatch)
    exchange.rewards['m01']=Decimal('25');rotation.refresh(lp,exchange,2)
    session_id=engine.lp_auto_state()['intents'][0]['session_id']
    original_entry=deepcopy([a for a in store.lp_actions(session_id) if a.get('role')=='entry'])
    captures, account_packets, result_packets, sdk_calls = [], [], [], []
    control_committed = Event()
    round_id='selected-independent-late-controls'
    def current_account(*,max_age_seconds=0,trade_generation_provider=None):
        del max_age_seconds
        started=pool.NOW
        packet=pool._fresh_registration_bundle(exchange,lp)
        packet['open_orders']=deepcopy([o for o in exchange.orders if o['status']=='LIVE'])
        pool.NOW+=timedelta(microseconds=1)
        packet.update(read_started_at=started,read_ended_at=pool.NOW,checked_at=pool.NOW)
        if trade_generation_provider is not None:packet['trade_generation']=trade_generation_provider()
        account_packets.append(deepcopy(packet))
        return packet
    exchange.lp_account_snapshot_shared=current_account
    def current_result(request):
        packet=read(request)
        result_packets.append(dict(request=deepcopy(request),packet=deepcopy(packet)))
        return packet
    exchange.lp_order_result_snapshot=current_result
    connect=sqlite3.connect
    class RegistrationConnection(sqlite3.Connection):
        def execute(self,sql,parameters=(),/):
            result=super().execute(sql,parameters)
            command=sql.strip().upper()
            if command in ('BEGIN','BEGIN IMMEDIATE'):
                self.registered_cancel=None
            elif command.startswith('INSERT INTO LP_ACTIONS') and len(parameters)>=5:
                if parameters[1]!=session_id or parameters[3]!='pending':return result
                row=super().execute('SELECT * FROM lp_actions WHERE action_key=?',(parameters[2],)).fetchone()
                payload=json.loads(row['payload'])
                if (payload.get('role')!='reconciliation_cancel' or payload.get('order_id')!='o1'
                        or payload.get('rotation_round_id')!=round_id
                        or payload.get('post_started') is True or payload.get('submit_post_started_at')):return result
                document=json.loads(super().execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
                plan=document['active_plan'];registration=plan['cancel_registration']
                assert plan['round_id']==registration['round_id']==round_id
                assert registration['sessions']=={'o1':session_id} and registration['completed'] is False
                assert all(a['kind']=='buy' for a in plan['actions'])
                assert {r['order_id'] for r in plan['victims']}=={'o1'}
                assert self.in_transaction
                assert row['state']=='pending' and row['action_id']==parameters[0]
                assert row['action_key'].startswith(session_id+':reconciliation-cancel:')
                self.registered_cancel=dict(row=dict(row),payload=payload,plan=deepcopy(plan))
            elif command=='COMMIT' and getattr(self,'registered_cancel',None) is not None:
                pending=self.registered_cancel;self.registered_cancel=None
                assert not self.in_transaction, 'Actual successful registration COMMIT returned'
                actual=super().execute('SELECT * FROM lp_actions WHERE action_key=?',(pending['row']['action_key'],)).fetchone()
                assert dict(actual)==pending['row'], 'Native observer never rewrites the pending audit'
                assert captures==[] and sdk_calls==[] and exchange.cancels==[]
                assert engine.lp_mutation_allowed() is True
                assert account_packets and result_packets
                packet=account_packets[-1]
                assert all(packet[k] is True for k in ('authenticated','balance_complete','open_orders_complete',
                    'positions_complete','trades_complete','pagination_complete'))
                assert packet['wallet_address']==packet['account_id']=='test-wallet'
                order,=packet['open_orders']
                assert order['order_id']=='o1' and order['status']=='LIVE' and order['side']=='BUY'
                assert order['token_id']==order['condition_id']=='m00'
                assert Decimal(order['price'])==Decimal('.40') and Decimal(order['original_size'])==20 and Decimal(order['size_matched'])==0
                before=store.lp_session(session_id);generation=store.lp_trade_generation()
                before_revision=store.lp_session_revision(session_id)
                # Existing native control fixture, not a public operator action.
                if change=='stop':patch={'stop_requested':True}
                else:
                    protection=deepcopy(before['queue_protection'])
                    for bucket in protection.get('levels',{'entry':protection}).values():bucket['state']='triggered'
                    patch={'queue_protection':protection}
                # The observer still owns this connection's writer permit.
                # Commit the late control in a new transaction on that same
                # connection, before returning to dispatch; never nest a writer.
                super().execute('BEGIN IMMEDIATE')
                after=store.lp_update_session(session_id,patch=patch,connection=self)
                super().execute('COMMIT')
                after_revision=store.lp_session_revision(session_id)
                assert after_revision>before_revision and store.lp_trade_generation()==generation
                captures.append(dict(pending=pending,before=deepcopy(before),after=deepcopy(after),patch=deepcopy(patch),
                    before_revision=before_revision,after_revision=after_revision,before_generation=generation,
                    after_generation=store.lp_trade_generation(),native_commit_returned=True,in_transaction=self.in_transaction))
                assert [a for a in store.lp_actions(session_id) if a.get('role')=='entry']==original_entry
                control_committed.set()
            return result
    def observed_connect(database,*args,**kwargs):
        if Path(database).resolve()==store.path.resolve():kwargs.setdefault('factory',RegistrationConnection)
        return connect(database,*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',observed_connect)
    cancel=exchange.cancel_order
    def venue(order_id):
        assert control_committed.is_set(), 'Native control patch COMMIT precedes actual SDK entry'
        assert len(captures)==1 and captures[0]['native_commit_returned'] and not captures[0]['in_transaction']
        assert engine.lp_mutation_allowed() is True
        assert order_id=='o1'
        sdk_calls.append(dict(order_id=order_id,action_key=captures[0]['pending']['row']['action_key']))
        return cancel(order_id)
    exchange.cancel_order=venue
    advance_auto_wait(engine,monkeypatch)
    try:
        state=engine.lp_auto_run_once(round_id=round_id)
        assert control_committed.wait(5), 'Exact registration COMMIT/control fixture watchdog'
        request,=_wait_round_cancels(store,state)
        assert len(captures)==1 and len(sdk_calls)==1
        pending=captures[0]['pending']['row']
        assert request['action_key']==pending['action_key'] and request['action_id']==pending['action_id']
        assert request['session_id']==session_id and request['order_id']=='o1' and request['rotation_round_id']==round_id
        assert request['state']=='accepted' and request['post_started'] is True
        assert exchange.cancels==['o1'] and len(exchange.posts)==1
        original,=exchange.orders
        assert original['order_id']=='o1' and original['status']=='LIVE' and original['token_id']=='m00' and original['side']=='BUY'
        assert Decimal(original['price'])==Decimal('.40') and Decimal(original['original_size'])==20 and Decimal(original['size_matched'])==0
        assert state['slots']['occupied']==1 and Decimal(state['funds']['buy_reserved_usd'])==8
        assert state['funds']['as_of'] and state['active_plan'] is None and state['last_round']['completed_at']
        assert all(a['kind']=='buy' for a in state['last_round']['actions'])
        current=store.lp_session(session_id)
        assert all(current.get(k)==v for k,v in captures[0]['patch'].items())
        assert [a for a in store.lp_actions(session_id) if a.get('role')=='entry']==original_entry
        records=state['last_round']['candidate_filter']['rotation_guards']
        eligible,=[r for r in records if r['outcome']=='eligible' and r['first_failing_predicate'] is None]
        assert eligible['predicate_inputs']['unresolved_submission'] is False
        assert predicate not in eligible['predicate_inputs']
        assert eligible['round_id']==hashlib.sha256(round_id.encode()).hexdigest()[:16]
        assert eligible['order_id']==hashlib.sha256(b'o1').hexdigest()[:16]
        assert eligible['session_id']==hashlib.sha256(session_id.encode()).hexdigest()[:16]
        packet=account_packets[-1]
        assert eligible['account_read_started_at']==packet['read_started_at'].isoformat()
        assert eligible['account_read_ended_at']==packet['read_ended_at'].isoformat()
        assert eligible['account_checked_at']==packet['checked_at'].isoformat()
        assert eligible['account_generation']==packet['trade_generation']
        assert session_id not in json.dumps(records) and 'test-wallet' not in json.dumps(records) and round_id not in json.dumps(records)
        observed=result_packets[-1]
        assert observed['request']['owned_order_ids']==['o1']
        assert all(observed['packet'][k] is True for k in ('authenticated','open_orders_complete','positions_complete','trades_complete','pagination_complete'))
        end=datetime.fromisoformat(state['last_round']['completed_at'])
        assert state['plan_wait'] is None
        audits=deepcopy(store.lp_actions(session_id));before_reads=(len(account_packets),len(result_packets))
        for elapsed in (1,29):
            monkeypatch.setattr(pool,'NOW',end+timedelta(seconds=elapsed))
            engine.lp_auto_run_once(round_id=round_id)
            assert engine.lp_auto_state()['last_round']==state['last_round']
            assert exchange.cancels==['o1'] and len(exchange.posts)==1 and len(sdk_calls)==1
            assert len(account_packets) >= before_reads[0] and len(result_packets) >= before_reads[1]
            assert store.lp_actions(session_id)==audits
            assert all(store.lp_session(session_id).get(k)==v for k,v in captures[0]['patch'].items())
        assert all(p['side']=='BUY' for p in exchange.posts)
    finally:
        (tmp_path/'IJ-late-controls-native-proof.json').write_text(json.dumps(dict(change=change,predicate=predicate,
            captures=captures,current=engine.lp_auto_state(),session=store.lp_session(session_id),
            account_packets=account_packets,result_packets=result_packets,sdk_calls=sdk_calls,
            audits=store.lp_actions(session_id),posts=exchange.posts,cancels=exchange.cancels),default=str,indent=2)+'\n')


@pytest.mark.parametrize('source_stale', ['reward', 'book'])
def test_confirmed_level_departure_uses_cancel_fact_freshness(tmp_path, monkeypatch, source_stale):
    monkeypatch.setattr(pool, "NOW", pool.NOW)
    engine, exchange, lp, store = _level_pool(tmp_path, monkeypatch)
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
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
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once(round_id='cancel-fact-freshness')
    _wait_round_cancels(store, state)
    expected_cancels = ['o1'] if source_stale == 'reward' else []
    assert exchange.cancels == expected_cancels, state['last_round']
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.80')
    original_requests = _wait_round_cancels(store, state)
    original_audits = [dict(a) for a in original_requests]
    state = engine.lp_auto_run_once()
    fresh_requests = _wait_round_cancels(store, state)
    assert [a['order_id'] for a in fresh_requests] == expected_cancels
    assert {a['action_key'] for a in fresh_requests}.isdisjoint(a['action_key'] for a in original_audits)
    assert all(next(a for a in store.lp_actions(old['session_id']) if a['action_key'] == old['action_key']) == old for old in original_audits)
    assert exchange.cancels == expected_cancels * 2
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.80')
    if source_stale == 'reward':
        exchange.orders[0]['status'] = 'CANCELED'
        advance_auto_wait(engine, monkeypatch)
        state = engine.lp_auto_run_once()
        assert exchange.cancels == ['o1', 'o1']
        assert len(exchange.posts) == 1
        assert state['slots']['occupied'] == 0
        assert Decimal(state['funds']['buy_reserved_usd']) == 0
