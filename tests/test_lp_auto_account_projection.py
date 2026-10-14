"""Account coverage replaces temporary automatic allocations without reattribution."""

from tests.test_lp_auto_pool import advance_auto_wait
from copy import deepcopy
from datetime import timedelta
import hashlib
from decimal import Decimal

import pytest

from tests import test_lp_auto_pool as pool


def covered_pool(tmp_path, *, buys=(), inventory='0', pnl='0'):
    engine, exchange, lp, store = pool.setup(tmp_path, 3)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=2))
    engine.lp_auto_set_desired_running(True)
    auto = engine._auto_pool
    facts = dict(account_id='test-wallet', pool_account_id=engine._lp_account_id(),
        checked_at=pool.NOW.isoformat(), read_started_at=pool.NOW.isoformat(),
        read_ended_at=pool.NOW.isoformat(), trade_generation=store.lp_trade_generation(),
        inventory_cost_usd=inventory, realized_pnl_usd=pnl,
        financial_status='known', reason_codes=[], buys=list(buys))
    auto._update(lambda d: d.update(account_financial_facts=facts))
    return engine, exchange, lp, store


def buy(order_id, *, token='m00', reserved='8', state='active'):
    return dict(order_id=order_id, side='BUY', session_id='account-session', condition_id=token,
        token_id=token, market_id=token, outcome='YES', price='.4', quantity='20',
        original_quantity='20', filled_quantity='0', reserved_usd=reserved,
        state=state, financial_status='known', checked_at=pool.NOW.isoformat())


def old_intent(**updates):
    return dict(intent_id='old', session_id='old-session', order_id=None,
        condition_id='m00', token_id='m00', market_id='m00', outcome='YES',
        price='.4', quantity='20', reserved_usd='8', inventory_cost_usd='0',
        realized_pnl_usd='0', financial_status='unknown', state='unknown',
        created_at=pool.NOW.isoformat(), checked_at=pool.NOW.isoformat(),
        reservation_coverage={'version': 1, 'state': 'covered', 'account_id': 'test-wallet',
            'pool_account_id': hashlib.sha256(b'test-wallet').hexdigest(),
            'snapshot_id': 'snapshot', 'session_id': 'old-session', 'intent_id': 'old',
            'read_started_at': pool.NOW.isoformat(), 'checked_at': pool.NOW.isoformat()}, **updates)


@pytest.mark.parametrize('pnl', ['3', '-20', None])
def test_account_projection_counts_order_ids_inventory_and_covered_audit_once(tmp_path, pnl):
    engine, _, _, _ = covered_pool(tmp_path, buys=[buy('a'), buy('b')], inventory='12', pnl=pnl)
    intent = old_intent()
    engine._auto_pool._update(lambda d: d['intents'].update(old=intent))
    state = engine.lp_auto_state()
    assert state['slots'] == dict(active=2, pending=0, pending_review=0, canceling=0, occupied=2)
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    assert Decimal(state['funds']['inventory_cost_usd']) == 12
    assert Decimal(state['funds']['total_usd']) == 100
    assert Decimal(state['funds']['spendable_usd']) == 72
    assert Decimal(state['funds']['isolated_reserved_usd']) == 0
    assert state['funds']['status'] == 'known'
    assert state['intents'] == [intent]
    assert not state['admission_block_reasons']
    assert 'submission_unknown' not in state['block_reasons']


def test_late_callback_cannot_restore_covered_funds_or_slot(tmp_path):
    engine, _, _, _ = covered_pool(tmp_path)
    auto = engine._auto_pool
    auto._update(lambda d: d['intents'].update(old=old_intent()))
    before = deepcopy(auto.state()['funds'])
    auto._record_session('old', dict(session_id='old-session'), error='late_network_failure',
        connection=None)
    state = auto.state()
    assert state['intents'][0]['state'] == 'unknown'
    assert 'reconcile_error' not in state['intents'][0]
    assert state['intents'][0]['reservation_coverage']
    assert state['funds'] == before
    assert state['slots']['occupied'] == 0


@pytest.mark.parametrize('invalid', ['stale', 'generation', 'inventory_unknown', 'wrong_account'])
def test_account_projection_failure_never_reenables_covered_reservations(tmp_path, invalid):
    engine, _, _, store = covered_pool(tmp_path)
    auto = engine._auto_pool
    def change(d):
        d['intents']['old'] = old_intent()
        facts = d['account_financial_facts']
        if invalid == 'stale':
            facts['checked_at'] = (pool.NOW - timedelta(seconds=61)).isoformat()
        elif invalid == 'inventory_unknown':
            facts.update(financial_status='unknown', inventory_cost_usd=None,
                         reason_codes=['account_inventory_cost_unknown'])
        elif invalid == 'wrong_account':
            facts['pool_account_id'] = 'other-wallet'
    auto._update(change)
    if invalid == 'generation':
        store.lp_advance_trade_generation(store.lp_trade_generation())
    state = auto.state()
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['spendable_usd'] is None
    assert state['funds']['available_usd'] is None
    assert state['admission_block_reasons']
    assert state['slots']['occupied'] == 0
    assert Decimal(state['funds']['buy_reserved_usd']) == 0
    assert Decimal(state['funds']['isolated_reserved_usd']) == 0


def test_uncovered_temporary_reservation_remains_beside_real_order(tmp_path):
    engine, _, _, _ = covered_pool(tmp_path, buys=[buy('a')])
    intent = old_intent()
    intent.pop('reservation_coverage')
    intent.update(state='reserved', financial_status='known', condition_id='m01', token_id='m01')
    engine._auto_pool._update(lambda d: d['intents'].update(old=intent))
    state = engine.lp_auto_state()
    assert state['slots']['occupied'] == 2
    assert state['slots']['pending'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    assert Decimal(state['funds']['spendable_usd']) == 84


def rotation_pool(tmp_path, monkeypatch, *, same_token=False, target=1):
    from tests import test_lp_auto_rotation as rotation
    engine, exchange, lp, store = rotation.setup(tmp_path, monkeypatch, count=3, target=1)
    original = engine.lp_auto_state()['intents'][0]
    session = store.lp_session(original['session_id'])
    rows = [buy('o1')]
    if same_token:
        exchange.orders.append({**exchange.orders[0], 'order_id': 'manual-second'})
        store.lp_register_exchange_orders('test-wallet', 'm00', exchange.orders, session=session)
        rows.append(buy('manual-second'))
    for row in rows:
        row['session_id'] = session['session_id']
    facts = dict(account_id='test-wallet', pool_account_id=engine._lp_account_id(),
        checked_at=pool.NOW.isoformat(), trade_generation=store.lp_trade_generation(),
        inventory_cost_usd='0', realized_pnl_usd='0', financial_status='known',
        reason_codes=[], buys=rows, order_fills={row['order_id']: '0' for row in rows})
    # Imported account orders have no automatic intent. The durable sessions
    # are the same execution objects regardless of their submitting origin.
    engine._auto_pool._update(lambda d: d.update(intents={}, account_financial_facts=facts,
                                                 target_buy_count=target))
    return engine, exchange, lp, store, rotation


def test_account_rotation_ranks_and_cancels_real_id_without_fake_intent(tmp_path, monkeypatch):
    engine, exchange, lp, store, rotation = rotation_pool(tmp_path, monkeypatch)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    rotation.refresh(lp, exchange, 3)
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    requests = _wait_round_cancels(store, state)
    assert exchange.cancels == ['o1']
    assert len(exchange.posts) == 1
    assert state['intents'] == []
    assert state['slots']['occupied'] == 1
    assert state['slots']['canceling'] == 0
    assert state['funds']['spendable_usd'] is None
    assert len(requests) == 1 and requests[0]['state'] == 'accepted'
    assert not store.lp_session(requests[0]['session_id']).get('yield_rotation_cancel_owners')
    assert any(a.get('order_id') == 'o1' and a.get('role') == 'reconciliation_cancel'
               for a in store.lp_actions(state['account_buys'][0]['session_id']))


def test_overcapacity_same_token_buys_use_two_slots_and_exact_rotation_victim(tmp_path, monkeypatch):
    engine, exchange, lp, store, rotation = rotation_pool(tmp_path, monkeypatch, same_token=True)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m00'] = Decimal('30')
    rotation.refresh(lp, exchange, 3)
    state = engine.lp_auto_state()
    assert state['slots']['occupied'] == 2
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('16')
    targets, victims, _, blocked = engine._auto_pool._ranked_buys(state)
    assert not blocked
    assert len(targets) == 1
    assert len(victims) == 1
    assert targets[0]['order_id'] != victims[0]['order_id']
    assert {targets[0]['order_id'], victims[0]['order_id']} == {'o1', 'manual-second'}
    completed = engine.lp_auto_run_once()
    assert completed['active_plan'] is None and completed['last_round']['completed_at']
    receipt, = _wait_round_cancels(store, completed)
    assert receipt['state'] == 'accepted' and receipt['order_id'] == victims[0]['order_id']
    assert receipt['session_id'] == victims[0]['session_id']
    assert receipt['rotation_round_id'] == completed['last_round']['round_id']
    assert receipt['action_id'] and receipt['action_key']
    assert any(a['action_id'] == receipt['action_id'] and a['action_key'] == receipt['action_key']
        for a in store.lp_actions(receipt['session_id']))
    assert completed['last_round']['targets'][0]['order_id'] == targets[0]['order_id']
    assert exchange.cancels == [victims[0]['order_id']]
    assert len(exchange.cancels) == 1
    assert len(exchange.posts) == 1


def test_account_rotation_fill_keeps_inventory_budget_without_sale(tmp_path, monkeypatch):
    import json
    import sqlite3
    from copy import deepcopy
    from collections import Counter
    monkeypatch.setattr(pool,'NOW',pool.NOW)
    from tests import test_lp_auto_rotation as rotation
    engine,exchange,lp,store=rotation.setup(tmp_path,monkeypatch,count=3,target=1)
    pool.NOW+=timedelta(microseconds=1)
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange,lp))['state']=='registered'
    registered=engine.lp_auto_state()
    assert [r['order_id'] for r in registered['account_buys']]==['o1']
    original=deepcopy(registered['intents'][0])
    assert original['reservation_coverage']['state']=='covered'
    assert registered['slots']['occupied']==1 and Decimal(registered['funds']['buy_reserved_usd'])==8
    assert registered['account_buys'][0]['session_id']==original['session_id']
    entry_round=deepcopy(registered['last_round']);sid=original['session_id']
    original_order=deepcopy(next(o for o in exchange.orders if o['order_id']==original['order_id']))
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader=install_order_result_reader(exchange,monkeypatch)
    exchange.rewards['m01']=Decimal('25')
    phase, evidence = ['setup'], dict(states=[],full_reads=[],result_reads=[],financial_reads=[],signed=[])
    def document():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state=deepcopy(engine.lp_auto_state())
        evidence['states'].append(dict(label=label,state=state,document=document(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=deepcopy(exchange.orders),positions=deepcopy(exchange.positions),trades=deepcopy(exchange.trades),
            posts=deepcopy(exchange.posts),cancels=list(exchange.cancels)))
        return state
    def exact_quantities(rows):
        for row in rows:
            if (row.get('side')=='BUY' and row.get('token_id')==row.get('condition_id')
                    and row.get('status') in ('LIVE','CANCELED') and row.get('fill_quantity_known') in (None,True)
                    and Decimal(str(row['price']))==Decimal('.40') and Decimal(str(row['original_size']))==20
                    and Decimal(str(row['size_matched'])) in (Decimal(0),Decimal(8))):
                row['fill_quantity_known']=True
        return rows
    account=exchange.lp_account_snapshot
    def financial():
        packet=account()
        evidence['financial_reads'].append(dict(phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),at=pool.NOW))
        return packet
    monkeypatch.setattr(exchange,'lp_account_snapshot',financial)
    def complete_account_round(*,max_age_seconds=0,trade_generation_provider=None):
        del max_age_seconds
        started=pool.NOW
        packet=pool._fresh_registration_bundle(exchange,engine._lp)
        pool.NOW+=timedelta(microseconds=1)
        packet.update(read_started_at=started,read_ended_at=pool.NOW,checked_at=pool.NOW,
            open_orders=exact_quantities(deepcopy([o for o in exchange.orders if o['status']=='LIVE'])),
            positions=deepcopy(exchange.positions),raw_trades=deepcopy(exchange.trades))
        if trade_generation_provider is not None:packet['trade_generation']=trade_generation_provider()
        evidence['full_reads'].append(deepcopy(dict(phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),packet=packet)))
        return packet
    monkeypatch.setattr(exchange,'lp_account_snapshot_shared',complete_account_round,raising=False)
    def results(request):
        packet=result_reader(request)
        for field in ('orders','open_orders'):exact_quantities(packet[field])
        evidence['result_reads'].append(deepcopy(dict(phase=phase[0],request=request,packet=packet)))
        return packet
    monkeypatch.setattr(exchange,'lp_order_result_snapshot',results)
    def signed():
        state=engine.lp_auto_state()
        evidence['signed'].append(deepcopy(dict(phase=phase[0],at=pool.NOW,as_of=state['funds']['as_of'],
            round=state['active_plan']['round_id'],account=state['account_buys'])))
    exchange.before_sign=signed
    def closed(state):
        assert state['active_plan'] is None and state['last_round']['completed_at']
        assert state['plan_wait'] is None
    def save(name):
        evidence.update(phase=phase[0],clock=pool.NOW,state=engine.lp_auto_state(),document=document(),
            sessions=store.lp_sessions(),audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=exchange.orders,positions=exchange.positions,trades=exchange.trades,posts=exchange.posts,cancels=exchange.cancels)
        (tmp_path/name).write_text(json.dumps(evidence,indent=2,default=str)+'\n')
    evidence['public_original_account_row']=deepcopy(registered['account_buys'][0])
    evidence['original_registered_coverage']=deepcopy(original['reservation_coverage'])
    try:
        phase[0]='new_current_original_cancel'
        advance_auto_wait(engine,monkeypatch)
        rotation.refresh(lp,exchange,3)
        sent=engine.lp_auto_run_once();closed(sent);cancel_round=capture('original_cancel_locally_complete')
        from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
        requests = _wait_round_cancels(store, sent)
        assert exchange.cancels==[original_order['order_id']] and len(exchange.posts)==1
        assert not any(a['kind']=='cancel' for a in cancel_round['last_round']['actions'])
        cancel_action, = requests
        assert cancel_action['session_id']==sid and cancel_action['order_id']==original_order['order_id']
        assert [t['token_id'] for t in cancel_round['last_round']['targets']]==['m01']
        old_audits=deepcopy(store.lp_actions(sid))
        original_cancel,=[a for a in old_audits if a.get('role')=='reconciliation_cancel']
        assert original_cancel['state']=='accepted' and original_cancel['order_id']==original_order['order_id']
        assert original_cancel['rotation_round_id']==cancel_round['last_round']['round_id']
        live_original=next(o for o in exchange.orders if o['order_id']==original_order['order_id'])
        live_original.update(status='CANCELED',size_matched='8')
        exchange.positions=[dict(token_id='m00',condition_id='m00',size='8',average_price='.40')]
        exchange.trades=[dict(trade_id='actual-o1-fill',status='CONFIRMED',trader_side='MAKER',
            taker_order_id='external-taker',token_id='m00',condition_id='m00',side='BUY',size='8',price='.40',
            fee='0',timestamp=pool.NOW,match_time=pool.NOW,maker_orders=[dict(order_id=original_order['order_id'],token_id='m00',side='BUY',
                price='.40',matched_amount='8',fee='0',maker_address='test-wallet')])]
        phase[0]='following_current_owned_positive_fill'
        advance_auto_wait(engine,monkeypatch)
        state=engine.lp_auto_run_once();closed(state);replacement=capture('new_current_m01_with_inventory')
        assert replacement['last_round']['round_id']!=cancel_round['last_round']['round_id']
        assert exchange.cancels==[original_order['order_id']]
        assert Counter(p['token_id'] for p in exchange.posts)==Counter(['m00','m01'])
        assert all(p['side']=='BUY' and Decimal(p['price'])==Decimal('.40') and Decimal(p['quantity'])==20 and p['post_only'] is True for p in exchange.posts)
        action,=[a for a in replacement['last_round']['actions'] if a['kind']=='buy']
        assert action['state']=='success' and action['token_id']=='m01' and action['session_id']!=sid
        assert action['action_id'] not in {a['action_id'] for a in cancel_round['last_round']['actions']+entry_round['actions']}
        assert state['slots']['occupied']==1 and Decimal(state['funds']['buy_reserved_usd'])==8
        assert Decimal(state['funds']['inventory_cost_usd'])==Decimal('3.20')
        planning,=[x for x in evidence['signed'] if x['round']==replacement['last_round']['round_id']]
        assert state['funds']['status']=='unknown' and state['funds']['as_of']==planning['as_of'] and state['funds']['available_usd'] is None
        registration=lp.register_account_snapshot;publication_results=[]
        def observed_publication(snapshot):
            result=registration(snapshot);publication_results.append(deepcopy(result));return result
        monkeypatch.setattr(lp,'register_account_snapshot',observed_publication)
        phase[0]='explicit_complete_fill_publication'
        pool.NOW+=timedelta(microseconds=1)
        published=engine.refresh_lp_dashboard_snapshot()
        assert published['state']=='ready',(published,publication_results)
        financial=capture('complete_fill_finance_88_80')
        reconciled = lp.reconcile_facts(sid)
        assert reconciled[3] is None, reconciled[3]
        owned=store.lp_session(sid)
        assert Decimal(owned['buy_filled_quantity'])==Decimal(owned['residual_quantity'])==8
        assert Decimal(owned['buy_cost'])==Decimal('3.20') and Decimal(owned['buy_fees'])==0
        assert owned['position_reconciled'] is True
        assert store.lp_actions(sid)==old_audits
        evidence['ledger']={k:deepcopy(owned.get(k)) for k in ('buy_filled_quantity','residual_quantity','buy_cost','buy_fees','fee_status','position_reconciled','verified_order_fills')}
        assert financial['funds']['status']=='known' and financial['slots']['occupied']==1
        assert Decimal(financial['funds']['buy_reserved_usd'])==8 and Decimal(financial['funds']['inventory_cost_usd'])==Decimal('3.20')
        assert Decimal(financial['funds']['available_usd'])==Decimal('88.80')
        assert store.lp_actions(sid)==old_audits and exchange.cancels==[original_order['order_id']] and len(exchange.posts)==2
        assert not any(p['side']=='SELL' for p in exchange.posts)
        engine.lp_auto_set_desired_running(False)
        counts=(len(evidence['financial_reads']),len(evidence['result_reads']),len(evidence['full_reads']))
        engine.lp_auto_run_once()
        current = engine.lp_auto_state()
        assert current['last_round']['round_id'] != financial['last_round']['round_id']
        assert current['active_plan'] is None and current['last_round']['actions'] == []
        assert len(evidence['financial_reads']) > counts[0] and len(evidence['full_reads']) > counts[2]
        assert Decimal(current['funds']['buy_reserved_usd']) == 8 and Decimal(current['funds']['inventory_cost_usd']) == Decimal('3.20')
        assert len(exchange.posts)==2 and exchange.cancels==[original_order['order_id']] and store.lp_actions(sid)==old_audits
        assert not any(x['active_plan'] for x in evidence['financial_reads'])
        evidence['publication_results']=publication_results;evidence['completed']='full'
    finally:save('KF-account-partial-diagnostics.json')

def test_late_known_receipt_cannot_reattribute_covered_unknown_request(tmp_path):
    engine, _, _, store = covered_pool(tmp_path, buys=[buy('account-order')])
    auto = engine._auto_pool
    old = old_intent()
    auto._update(lambda d: d['intents'].update(old=deepcopy(old)))
    discovered = dict(session_id='old-session', state='entry_open', entry_order_id='account-order',
        condition_id='m00', token_id='m00', submit_status='accepted',
        order_history={'account-order': dict(status='LIVE', side='BUY', token_id='m00')})
    with store._transaction() as connection:
        auto._record_session('old', discovered, connection=connection)
    assert auto.state()['intents'] == [old]
    assert auto._read()['events'] == {}
    assert auto.state()['slots']['occupied'] == 1
    assert Decimal(auto.state()['funds']['buy_reserved_usd']) == 8


@pytest.mark.parametrize('returned', [True, False])
def test_refresh_generation_wait_preserves_health_and_does_not_retry_in_round(tmp_path, returned):
    from open_trader.polymarket_lp import LpObservationWait
    engine, exchange, lp, _ = covered_pool(tmp_path)
    calls = []
    def snapshot(**kwargs):
        calls.append(kwargs)
        if not returned:
            raise LpObservationWait('account_round_invalid')
        return {}
    exchange.lp_account_snapshot_shared = snapshot
    lp.register_account_snapshot = lambda snapshot: dict(state='skipped', reason='account_round_invalid')
    prior = lp._account_order_sync_error
    state = engine.lp_auto_reconcile_unknown()
    assert len(calls) == 1
    assert calls[0]['max_age_seconds'] == 0
    assert callable(calls[0]['trade_generation_provider'])
    assert lp._account_order_sync_error == prior
    assert state['funds']['spendable_usd'] is None
    assert 'account_round_invalid' in state['admission_block_reasons']


def test_unknown_real_order_capital_keeps_slot_and_blocks_admission(tmp_path):
    row = buy('missing-remaining', state='canceling')
    row.update(reserved_usd=None, quantity=None, financial_status='unknown')
    engine, _, _, _ = covered_pool(tmp_path, buys=[row])
    state = engine.lp_auto_state()
    assert state['slots']['occupied'] == 1
    assert state['slots']['canceling'] == 1
    assert state['funds']['buy_reserved_usd'] is None
    assert state['funds']['spendable_usd'] is None
    assert state['funds']['status'] == 'unknown'


def test_rotation_metadata_cannot_overwrite_new_partial_fill_capital(tmp_path):
    engine, _, _, _ = covered_pool(tmp_path, buys=[{**buy('a'), 'quantity': '12', 'reserved_usd': '4.8'}], inventory='3.2')
    engine._auto_pool._update(lambda d: d.update(account_rotations={'a': {
        **buy('a'), 'rotation_requested_at': pool.NOW.isoformat(), 'rotation_cancel_acknowledged': True}}))
    state = engine.lp_auto_state()
    assert state['slots']['canceling'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('4.8')
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.2')
    assert Decimal(state['funds']['spendable_usd']) == 92


def test_first_account_coverage_preserves_inflight_rotation_fill_stop(tmp_path, monkeypatch):
    from tests import test_lp_auto_rotation as rotation
    engine, exchange, lp, store = rotation.setup(tmp_path, monkeypatch, count=2, target=1)
    from copy import deepcopy
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    rotation.refresh(lp, exchange, 2)
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    rotation_state = engine.lp_auto_run_once()
    requests = _wait_round_cancels(store, rotation_state)
    assert len(requests) == 1 and requests[0]['order_id'] == 'o1' and requests[0]['state'] == 'accepted'
    intent = engine.lp_auto_state()['intents'][0]
    assert intent['state'] == 'active'
    assert exchange.cancels == ['o1']
    original_audit = deepcopy(store.lp_actions(intent['session_id']))
    exchange.orders[0].update(status='CANCELED', size_matched='8')
    exchange.positions = [dict(token_id='m00', condition_id='m00', size='8', average_price='.40')]
    exchange.trades = [dict(trade_id='actual-o1-fill', status='CONFIRMED', trader_side='MAKER',
        taker_order_id='external-taker', token_id='m00', condition_id='m00', side='BUY', size='8', price='.40',
        fee='0', timestamp=pool.NOW, maker_orders=[dict(order_id='o1', token_id='m00', side='BUY',
            price='.40', matched_amount='8', fee='0', maker_address='test-wallet')])]
    advance_auto_wait(engine, monkeypatch)
    pool.NOW += timedelta(microseconds=1)
    packet = pool._fresh_registration_bundle(exchange, lp)
    packet['open_orders'] = [o for o in exchange.orders if o['status'] == 'LIVE']
    assert lp.register_account_snapshot(packet)['state'] == 'registered'
    covered = engine.lp_auto_state()['intents'][0]
    assert covered['reservation_coverage']['state'] == 'covered'
    assert covered['state'] == 'active'
    original_as_of = engine.lp_auto_state()['funds']['as_of']
    state = engine.lp_auto_run_once()
    assert state['last_round']['completed_at'] and state['active_plan'] is None
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.2')
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01']
    assert exchange.posts[-1]['price'] == Decimal('.40') and exchange.posts[-1]['quantity'] == 20
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == original_as_of
    assert store.lp_actions(intent['session_id']) == original_audit
    original = next(i for i in state['intents'] if i['intent_id'] == intent['intent_id'])
    assert original['state'] == 'active'  # Original audit stays frozen after coverage.
    assert original['reservation_coverage'] == covered['reservation_coverage']
    assert all(p['side'] == 'BUY' for p in exchange.posts)


def test_initial_configuration_accepts_known_manual_account_overcapacity(tmp_path):
    engine, exchange, _, store = pool.setup(tmp_path, 3)
    engine._lp_auto_pool()._update(lambda d: d.update(account_financial_facts=dict(
        account_id='test-wallet', pool_account_id=engine._lp_account_id(),
        checked_at=pool.NOW.isoformat(), trade_generation=store.lp_trade_generation(),
        financial_status='known', reason_codes=[], inventory_cost_usd='0',
        realized_pnl_usd='0', buys=[buy('manual-a'), buy('manual-b')])))
    state = engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    assert state['slots']['occupied'] == 2
    assert state['target_buy_count'] == 1
    assert Decimal(state['funds']['spendable_usd']) == 84
    assert state['intents'] == []
    with pytest.raises(ValueError, match='pause_and_finish_automatic_buys_before_configuring'):
        engine.lp_auto_configure(dict(budget_usd='120', target_buy_count=3))
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once()
    assert exchange.posts == []


def test_covered_request_attention_is_inactive_but_audit_is_preserved(tmp_path, monkeypatch):
    from open_trader.polymarket_lp_notification_batches import ChannelDeliveryResult
    engine, _, _, _ = covered_pool(tmp_path)
    auto = engine._auto_pool
    intent = old_intent(attention_due=True, attention_recovery_due=True,
        attention_episode='old-fault', attention_since=pool.NOW.isoformat(), manual_attention=True)
    auto._update(lambda d: d['intents'].update(old=deepcopy(intent)))
    auto._deliver_attention('old')
    auto._deliver_attention('old', recovery=True)
    auto._finish_attention_delivery({('old', False, 'old-fault'): ChannelDeliveryResult({'test'}, {'test'})}, recovery=False)
    calls = []
    monkeypatch.setattr(auto, '_deliver_attention', lambda *args, **kwargs: calls.append(args))
    auto.flush_attention()
    auto.reconcile_attention()
    assert calls == []
    assert auto._read()['intents']['old'] == intent


def test_covered_unknown_session_keeps_its_original_request_unbound(tmp_path, monkeypatch):
    import json
    import sqlite3
    from copy import deepcopy
    from collections import Counter
    monkeypatch.setattr(pool,'NOW',pool.NOW)
    from tests import test_lp_auto_rotation as rotation
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    from tests.test_lp_auto_plan_scheduler import restart_engine
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    monkeypatch.setattr(pool,'Exchange',rotation.RotationExchange)
    engine,exchange,lp,store=pool.setup(tmp_path,2)
    result_reader=install_order_result_reader(exchange,monkeypatch)
    engine.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    engine.lp_auto_set_desired_running(True)
    exchange.fail=True
    engine.lp_auto_run_once()
    original=deepcopy(engine.lp_auto_state()['intents'][0]);sid=original['session_id']
    original_audit=deepcopy(store.lp_actions(sid));original_round=deepcopy(engine.lp_auto_state()['last_round'])
    assert original['state']=='unknown' and original['order_id'] is None
    assert len(original_audit)==1 and original_audit[0]['state']=='unknown' and original_audit[0]['post_started'] is True and not original_audit[0].get('order_id')
    assert original_round['completed_at'] and engine.lp_auto_state()['active_plan'] is None
    exchange.fail=False
    phase, evidence = ['setup'], dict(states=[],full_reads=[],result_reads=[],financial_reads=[],signed=[])
    def document():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state=deepcopy(engine.lp_auto_state())
        evidence['states'].append(dict(label=label,state=state,document=document(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=deepcopy(exchange.orders),positions=deepcopy(exchange.positions),trades=deepcopy(exchange.trades),
            posts=deepcopy(exchange.posts),cancels=list(exchange.cancels)))
        return state
    def exact_quantities(rows):
        for row in rows:
            if (row.get('side')=='BUY' and row.get('token_id')==row.get('condition_id')
                    and row.get('status') in ('LIVE','CANCELED') and row.get('fill_quantity_known') in (None,True)
                    and Decimal(str(row['price']))==Decimal('.40') and Decimal(str(row['original_size']))==20
                    and Decimal(str(row['size_matched'])) in (Decimal(0),Decimal(8))):
                row['fill_quantity_known']=True
        return rows
    account=exchange.lp_account_snapshot
    def financial():
        packet=account()
        evidence['financial_reads'].append(dict(phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),at=pool.NOW))
        return packet
    monkeypatch.setattr(exchange,'lp_account_snapshot',financial)
    def complete_account_round(*,max_age_seconds=0,trade_generation_provider=None):
        del max_age_seconds
        started=pool.NOW
        packet=pool._fresh_registration_bundle(exchange,engine._lp)
        pool.NOW+=timedelta(microseconds=1)
        packet.update(read_started_at=started,read_ended_at=pool.NOW,checked_at=pool.NOW,
            open_orders=exact_quantities(deepcopy([o for o in exchange.orders if o['status']=='LIVE'])),
            positions=deepcopy(exchange.positions),raw_trades=deepcopy(exchange.trades))
        if trade_generation_provider is not None:packet['trade_generation']=trade_generation_provider()
        evidence['full_reads'].append(deepcopy(dict(phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),packet=packet)))
        return packet
    monkeypatch.setattr(exchange,'lp_account_snapshot_shared',complete_account_round,raising=False)
    def results(request):
        packet=result_reader(request)
        for field in ('orders','open_orders'):exact_quantities(packet[field])
        evidence['result_reads'].append(deepcopy(dict(phase=phase[0],request=request,packet=packet)))
        return packet
    monkeypatch.setattr(exchange,'lp_order_result_snapshot',results)
    def signed():
        state=engine.lp_auto_state()
        evidence['signed'].append(deepcopy(dict(phase=phase[0],at=pool.NOW,as_of=state['funds']['as_of'],
            round=state['active_plan']['round_id'],account=state['account_buys'])))
    exchange.before_sign=signed
    def closed(state):
        assert state['active_plan'] is None and state['last_round']['completed_at']
        assert state['plan_wait'] is None
    def save(name):
        evidence.update(phase=phase[0],clock=pool.NOW,state=engine.lp_auto_state(),document=document(),
            sessions=store.lp_sessions(),audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=exchange.orders,positions=exchange.positions,trades=exchange.trades,posts=exchange.posts,cancels=exchange.cancels)
        (tmp_path/name).write_text(json.dumps(evidence,indent=2,default=str)+'\n')
    original_event=deepcopy(document()['events'].get(original['intent_id']+':unknown'))
    def assert_original_request():
        current=document()['intents'][original['intent_id']]
        for key in ('intent_id','session_id','created_at','state','order_id','market_id','condition_id','token_id','price','quantity','reserved_usd'):
            assert current.get(key)==original.get(key),key
        assert current['settled'] is False and Decimal(current['reserved_usd'])==8
        assert next(a for a in store.lp_actions(sid) if a['action_key']==original_audit[0]['action_key'])==original_audit[0]
        assert document()['events'].get(original['intent_id']+':unknown')==original_event
        assert not current.get('order_id') and current['state']=='unknown'
    try:
        phase[0]='actual_unrelated_manual_late_coverage'
        exchange.orders=[dict(order_id='manual-late',token_id='m00',condition_id='m00',market_id='m00',outcome='YES',
            side='BUY',status='LIVE',price='.40',original_size='20',size_matched='0')]
        pool.NOW+=timedelta(seconds=1)
        result=lp.register_account_snapshot(pool._fresh_registration_bundle(exchange,lp))
        assert result['state']=='registered',result
        managed=store.lp_session(sid)
        assert managed['state']=='entry_open' and managed['submit_status']=='unknown'
        assert_original_request()
        covered=capture('public_manual_late_coverage_original_request_unknown')
        marker=document()['intents'][original['intent_id']]['reservation_coverage']
        assert marker['state']=='covered' and marker['intent_id']==original['intent_id'] and marker['session_id']==sid
        assert covered['slots']['occupied']==1 and Decimal(covered['funds']['buy_reserved_usd'])==8 and len(exchange.posts)==1
        engine.lp_auto_set_desired_running(False)
        phase[0]='paused_current_coverage_no_new_authority'
        advance_auto_wait(engine,monkeypatch)
        state=engine.lp_auto_run_once()
        assert state['active_plan'] is None and state['last_round']['actions']==[] and state['last_round']['checked_at']
        assert state['plan_wait'] is None
        assert_original_request();assert exchange.cancels==[] and len(exchange.posts)==1
        exchange.rewards['m01']=Decimal('25')
        rotation.refresh(lp,exchange,2)
        phase[0]='new_current_manual_late_cancel'
        advance_auto_wait(engine,monkeypatch)
        assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange,lp))['state']=='registered'
        engine.lp_auto_set_desired_running(True)
        state=engine.lp_auto_run_once();closed(state);new_round=capture('distinct_current_manual_late_request_not_original_receipt')
        assert new_round['last_round']['round_id']!=original_round['round_id']
        from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
        cancel_action,=_wait_round_cancels(store,new_round)
        assert exchange.cancels==['manual-late'] and len(exchange.posts)==1
        assert cancel_action['order_id']=='manual-late' and cancel_action['state']=='accepted'
        assert all(a['kind']=='buy' for a in new_round['last_round']['actions'])
        new_audit,=[a for a in store.lp_actions(cancel_action['session_id']) if a.get('rotation_round_id')==new_round['last_round']['round_id']]
        assert new_audit['order_id']=='manual-late' and new_audit['state']=='accepted' and new_audit['role']=='reconciliation_cancel'
        assert new_audit['action_id']!=original_audit[0]['action_id'] and new_audit['action_key']!=original_audit[0]['action_key']
        assert new_audit['rotation_round_id']!=original_round['round_id']
        target,=[t for t in new_round['last_round']['targets'] if t['token_id']=='m01']
        assert Decimal(target['price'])==Decimal('.40') and Decimal(target['quantity'])==20
        dependent,=[a for a in new_round['last_round']['actions'] if a['kind']=='buy']
        assert dependent['token_id']=='m01' and dependent['state']=='not_sent' and dependent['reason']=='next_round_capacity'
        assert not any(s.get('token_id')=='m01' for s in store.lp_sessions())
        assert [o['order_id'] for o in exchange.orders if o['status']=='LIVE']==['manual-late']
        assert state['slots']['occupied']==1 and Decimal(state['funds']['buy_reserved_usd'])==8
        assert_original_request()
        evidence['original_round']=original_round;evidence['original_entry_audit']=original_audit;evidence['current_cancel_audit']=new_audit
        engine.lp_auto_set_desired_running(False)
        audits=deepcopy({s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()})
        counts=(len(evidence['financial_reads']),len(evidence['full_reads']),len(evidence['result_reads']))
        completed=pool.datetime.fromisoformat(new_round['last_round']['completed_at'])
        scheduler=LPAutoScheduler(engine,clock=lambda:pool.NOW)
        for elapsed in (1,29):
            scheduler.request_check();scheduler.run_due();engine.lp_auto_run_once()
            current = engine.lp_auto_state()
            assert current['last_round']['round_id'] != new_round['last_round']['round_id']
            assert current['active_plan'] is None and current['last_round']['actions'] == []
            assert len(evidence['financial_reads']) > counts[0] and len(evidence['full_reads']) > counts[1]
            assert {s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()}==audits
            assert exchange.cancels==['manual-late'] and len(exchange.posts)==1
            assert_original_request()
        phase[0]='full_cold_current_observation'
        engine=restart_engine(engine,exchange)
        state=engine.lp_auto_run_once()
        assert state['active_plan'] is None and state['last_round']['actions'] == []
        assert len(evidence['financial_reads']) > counts[0] and len(evidence['full_reads']) > counts[1]
        assert exchange.cancels==['manual-late'] and len(exchange.posts)==1
        assert state['slots']['occupied']==1 and Decimal(state['funds']['buy_reserved_usd'])==8
        assert {s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()}==audits
        assert_original_request()
        assert not any(x['active_plan'] for x in evidence['financial_reads'])
        evidence['completed']='full'
    finally:save('KF-covered-unknown-diagnostics.json')

def test_account_row_fill_blocks_rotation_even_before_session_fill_updates(tmp_path, monkeypatch):
    from tests.test_lp_auto_independent_cancel_contract import _five_resting, _wait_round_cancels
    from polymarket.models.data.portfolio import Position
    from tests.test_lp_account_reservation_reconciliation import _refill_identity
    from tests.test_lp_order_registration_contract import WALLET
    from tests.test_lp_account_reservation_reconciliation import runtime as sdk_fixture
    fixture_lifetime = sdk_fixture.__wrapped__(tmp_path, monkeypatch)
    runtime = next(fixture_lifetime)
    try:
        store, adapter, account, lp, engine, public = _five_resting(runtime, f_price='.20')
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        session = next(s for s in store.lp_sessions() if s['entry_order_id'] == 'original-E')
        assert Decimal(session['buy_filled_quantity']) == 0
        account.orders = tuple(o.model_copy(update={'size_matched': Decimal('8')}) if o.id == 'original-E' else o
            for o in account.orders)
        account.positions = (Position(conditionId=_refill_identity(5)[1], proxyWallet=WALLET,
            asset=_refill_identity(5)[2], size=Decimal('8'), avgPrice=Decimal('.40'), outcome='YES'),)
        assert Decimal(store.lp_session(session['session_id'])['buy_filled_quantity']) == 0
        state = engine.lp_auto_run_once(round_id='current-api-partial')
        requests = _wait_round_cancels(store, state)
        assert account.cancels == [('original-E',)] and len(requests) == 1
        assert requests[0]['session_id'] == session['session_id'] and requests[0]['order_id'] == 'original-E'
        assert account.posts == account.market_orders == []
        assert state['slots']['occupied'] == 5
        assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('36.8')
        assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.2')
    finally:
        fixture_lifetime.close()


def test_rotation_reanchors_same_price_survivor_without_resetting_protection(tmp_path, monkeypatch):
    from tests import test_lp_auto_rotation as rotation
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    monkeypatch.setattr(pool, 'Exchange', rotation.RotationExchange)
    engine, exchange, lp, store = pool.setup(tmp_path, 2)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    install_order_result_reader(exchange, monkeypatch)
    engine.lp_auto_configure(dict(budget_usd='16', target_buy_count=1))
    exchange.orders = [dict(order_id=oid, token_id='m00', condition_id='m00', market_id='m00',
        outcome='YES', side='BUY', status='LIVE', price='.4', original_size='20', remaining_size='20', size_matched='0')
        for oid in ('rank-b', 'rank-a')]
    exchange.rewards['m00'] = Decimal('30')
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    session = store.lp_active_sessions()[0]
    before = lp._queue_protection_levels(session)
    assert len(before) == 1
    key, bucket = next(iter(before.items()))
    assert bucket['order_id'] == 'rank-b'
    assert bucket['state'] == 'registered'
    assert lp._queue_protection_gate_open(session)
    rotation.refresh(lp, exchange, 2)
    engine.lp_auto_set_desired_running(True)
    state=engine.lp_auto_run_once()
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    request,=_wait_round_cancels(store,state)
    assert state['last_round']['actions']==[]
    assert request['order_id']=='rank-b' and request['state']=='accepted'
    assert exchange.cancels == ['rank-b']
    unchanged=store.lp_session(session['session_id'])
    assert lp._queue_protection_levels(unchanged)[key]==bucket
    assert not lp._order_cancel_requested(unchanged,'rank-a')
    assert not lp._order_cancel_requested(unchanged,'rank-b')
    next(o for o in exchange.orders if o['order_id']=='rank-b')['status']='CANCELED'
    pool.NOW+=timedelta(seconds=1)
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange,lp))['state']=='registered'
    account_reader=exchange.lp_account_snapshot
    def complete_account():
        snapshot=account_reader()
        snapshot.update(balance_complete=True,trades_complete=True,pagination_complete=True,
            raw_trades=deepcopy(exchange.trades))
        return snapshot
    monkeypatch.setattr(exchange,'lp_account_snapshot',complete_account)
    reconciled=lp.reconcile_facts(session['session_id'])
    assert reconciled[3] is None, reconciled[3]
    after = store.lp_session(session['session_id'])
    assert lp._queue_protection_levels(after)[key] == {**bucket, 'order_id': 'rank-a'}
    assert lp._queue_protection_gate_open(after)
    assert not lp._order_cancel_requested(after, 'rank-a')
    assert not lp._order_cancel_requested(after, 'rank-b')
    assert next(a for a in store.lp_actions(session['session_id']) if a['action_key']==request['action_key'])==request
    assert exchange.posts == []


@pytest.mark.parametrize('blocked', ['cancel_batch', 'different_price', 'filled', 'unknown'])
def test_rotation_does_not_reanchor_to_unprotected_or_canceling_order(tmp_path, monkeypatch, blocked):
    engine, _, lp, store, _ = rotation_pool(tmp_path, monkeypatch, same_token=True)
    state = engine.lp_auto_state()
    anchor = next(row for row in state['account_buys'] if row['order_id'] == 'o1')
    session = store.lp_session(anchor['session_id'])
    before = lp._queue_protection_levels(session)
    assert next(iter(before.values()))['order_id'] == 'o1'
    if blocked == 'cancel_batch':
        # All victims are registered before per-session cancel flags change.
        lp.begin_order_cancel(['o1', 'manual-second'])
    else:
        history = deepcopy(session['order_history'])
        patch = {'different_price': {'price': '.39'}, 'filled': {'size_matched': '1'},
                 'unknown': {'status': 'UNKNOWN'}}[blocked]
        history['manual-second'].update(patch)
        store.lp_update_session(session['session_id'], patch={'order_history': history})
    engine._auto_pool._mark_rotation_cancel_requested(anchor)
    after = store.lp_session(session['session_id'])
    for key, bucket in lp._queue_protection_levels(after).items():
        if before[key]['order_id'] == 'o1':
            assert bucket['order_id'] == 'o1'
            assert bucket['state'] == 'canceling'
            assert bucket['baseline_price'] == before[key]['baseline_price']


def test_manual_account_inventory_does_not_become_known_zero_historical_report(tmp_path):
    engine, _, _, _ = covered_pool(tmp_path, inventory='12', pnl='3')
    result = engine.lp_auto_report_facts(period_start=pool.NOW - timedelta(hours=1), period_end=pool.NOW)
    assert result['state']['intents'] == []
    assert result['funds']['status'] == 'known'
    assert Decimal(result['funds']['inventory_cost_usd']) == 12
    assert Decimal(result['funds']['realized_pnl_usd']) == 3
    period = result['financial_period']
    assert period['status'] == 'unknown'
    assert period['reason'] == 'account_historical_boundary_unavailable'
    assert period['inventory_cost_usd'] is None
    assert period['inventory_quantity'] is None
    assert period['realized_pnl_usd'] is None
    assert 'inventories' not in period


@pytest.mark.parametrize('publication_timing', ['during_read', 'after_wait'])
def test_fresh_dashboard_publication_recovers_pool_wait_without_another_helper_read(tmp_path, monkeypatch, publication_timing):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from open_trader.polymarket_lp import LpObservationWait
    engine, exchange, lp, _ = pool.setup(tmp_path)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    auto = engine._auto_pool
    entered, release = Event(), Event()
    reads = []
    def delayed_failure(**kwargs):
        reads.append(kwargs)
        entered.set()
        assert release.wait(5)
        raise LpObservationWait('account_round_invalid')
    exchange.lp_account_snapshot_shared = delayed_failure
    def publish_dashboard():
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
        assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    with ThreadPoolExecutor(1) as workers:
        pending = workers.submit(auto._refresh_account_facts)
        try:
            assert entered.wait(3)
            if publication_timing == 'during_read':
                # Capture the baseline before read I/O, not at failed completion.
                publish_dashboard()
        finally:
            release.set()
        assert pending.result(timeout=3) is False
    if publication_timing == 'after_wait':
        assert 'account_round_invalid' in auto.state()['admission_block_reasons']
        publish_dashboard()
    assert len(reads) == 1
    state = auto.state()
    assert 'account_round_invalid' not in state['admission_block_reasons']
    assert state['funds']['status'] == 'known'
    assert Decimal(state['funds']['spendable_usd']) == 100


@pytest.mark.parametrize('older_outcome', ['success', 'wait', 'error'])
def test_older_pool_success_cannot_clear_newer_concurrent_wait(tmp_path, monkeypatch, older_outcome):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from open_trader.polymarket_lp import LpObservationWait
    engine, exchange, lp, _ = pool.setup(tmp_path)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
    old_success = pool._fresh_registration_bundle(exchange, lp)
    entered, release = Event(), Event()
    lock, reads = Lock(), []
    def overlapping(**kwargs):
        with lock:
            index = len(reads)
            reads.append(kwargs)
        if index == 0:
            entered.set()
            assert release.wait(5)
            if older_outcome == 'wait':
                raise LpObservationWait('session_changed')
            if older_outcome == 'error':
                raise TimeoutError('older_endpoint_failure')
            return old_success
        raise LpObservationWait('account_round_invalid')
    exchange.lp_account_snapshot_shared = overlapping
    auto = engine._auto_pool
    baseline = deepcopy(auto._read()['account_financial_facts'])
    with ThreadPoolExecutor(2) as workers:
        old = workers.submit(auto._refresh_account_facts)
        try:
            assert entered.wait(3)
            new = workers.submit(auto._refresh_account_facts)
            assert new.result(timeout=3) is False
            assert 'account_round_invalid' in auto.state()['admission_block_reasons']
        finally:
            release.set()
        old.result(timeout=3)
    assert len(reads) == 2
    assert auto._read()['account_financial_facts'] == baseline
    assert lp._account_order_sync_error is None
    state = auto.state()
    assert 'account_round_invalid' in state['admission_block_reasons']
    assert state['funds']['spendable_usd'] is None


@pytest.mark.parametrize('partial_status,mismatched_original_size',
    [('known', False), ('unknown', False), ('known', True)],
    ids=['known', 'unknown', 'original-size-mismatch'])
def test_account_overcapacity_cancels_only_unfilled_extra_beside_partial_buy(
        tmp_path, monkeypatch, partial_status, mismatched_original_size):
    import json
    from datetime import datetime
    monkeypatch.setattr(pool,'NOW',pool.NOW)
    if partial_status == 'known':
        from tests import test_lp_auto_rotation as rotation
        from tests.test_lp_auto_plan_execution import install_order_result_reader
        engine,exchange,lp,store = rotation.setup(tmp_path,monkeypatch,count=3,target=1)
        result_reader = install_order_result_reader(exchange,monkeypatch)
        original_order = deepcopy(next(o for o in exchange.orders if o['order_id'] == 'o1'))
        original_sid = engine.lp_auto_state()['intents'][0]['session_id']
        entry_audits = deepcopy(store.lp_actions(original_sid))
        partial = dict(order_id='partial-buy',token_id='m01',condition_id='m01',side='BUY',status='LIVE',
            price='.4',original_size='20',size_matched='1',fill_quantity_known=True)
        exchange.orders.append(partial)
        exchange.positions=[dict(token_id='m01',condition_id='m01',size='1',average_price='.40')]
        for row in exchange.orders: row['fill_quantity_known']=True
        exchange.trades=[dict(trade_id='partial-maker-fill',status='CONFIRMED',trader_side='MAKER',
            taker_order_id='outside-taker',token_id='m01',condition_id='m01',side='BUY',size='1',price='.40',
            fee='0',timestamp=pool.NOW,match_time=pool.NOW,maker_orders=[dict(order_id='partial-buy',
                token_id='m01',side='BUY',price='.40',matched_amount='1',fee='0',maker_address='test-wallet')])]
        store.lp_create_session('partial-session','manual-partial',state='entry_open',payload=dict(
            account_id='test-wallet',condition_id='m01',token_id='m01',market_id='m01',outcome='YES',
            price='.4',quantity='20',entry_order_id='partial-buy',owned_order_ids=['partial-buy'],
            order_history={'partial-buy':partial},buy_filled_quantity='1',residual_quantity='1'))
        evidence=dict(full_packets=[],result_packets=[],plans=[],states=[],publications=[])
        def full(*,max_age_seconds=0,trade_generation_provider=None):
            started=pool.NOW
            pool.NOW += timedelta(microseconds=1)
            packet=pool._fresh_registration_bundle(exchange,lp)
            packet.update(read_started_at=started,read_ended_at=pool.NOW,checked_at=pool.NOW,
                open_orders=deepcopy([o for o in exchange.orders if o['status']=='LIVE']),
                positions=deepcopy(exchange.positions),raw_trades=deepcopy(exchange.trades))
            if trade_generation_provider is not None: packet['trade_generation']=trade_generation_provider()
            evidence['full_packets'].append(deepcopy(packet))
            return packet
        def results(request):
            evidence['plans'].append(deepcopy(engine.lp_auto_state()['active_plan']))
            packet=result_reader(request)
            if mismatched_original_size:
                for row in packet['orders']+packet['open_orders']:
                    if row['order_id']=='partial-buy': row['original_size']='21'
            evidence['result_packets'].append(deepcopy(dict(request=request,packet=packet)))
            return packet
        monkeypatch.setattr(exchange,'lp_account_snapshot_shared',full,raising=False)
        monkeypatch.setattr(exchange,'lp_order_result_snapshot',results)
        def capture(label):
            state=engine.lp_auto_state();evidence['states'].append(deepcopy(dict(label=label,state=state)));return state
        try:
            pool.NOW += timedelta(microseconds=1)
            packet=full(trade_generation_provider=store.lp_trade_generation)
            publication=lp.register_account_snapshot(packet);evidence['publications'].append(deepcopy(publication))
            assert publication['state']=='registered',publication
            before=capture('truthful-first-public-authority')
            assert before['slots']['occupied']==2 and before['target_buy_count']==1
            assert Decimal(before['funds']['buy_reserved_usd'])==Decimal('15.60')
            assert Decimal(before['funds']['inventory_cost_usd'])==Decimal('.40')
            partial_fact,=[o for o in before['account_buys'] if o['order_id']=='partial-buy']
            assert Decimal(partial_fact['original_quantity'])==20 and Decimal(partial_fact['filled_quantity'])==1
            assert Decimal(partial_fact['quantity'])==19 and Decimal(partial_fact['reserved_usd'])==Decimal('7.60')
            assert len(exchange.posts)==1 and store.lp_actions(original_sid)==entry_audits
            advance_auto_wait(engine,monkeypatch)
            state=engine.lp_auto_run_once();capture('overcapacity-original-attempt-complete')
            assert state['slots']['occupied']==2 and Decimal(state['funds']['inventory_cost_usd'])==Decimal('.40')
            assert len(exchange.posts)==1 and not store.lp_session('partial-session').get('entry_cancel_requested')
            assert state['active_plan'] is None and state['last_round']['completed_at']
            assert state['plan_wait'] is None
            if mismatched_original_size:
                assert exchange.cancels==[] and state['last_round']['reason']=='target_filled'
                assert Decimal(state['funds']['buy_reserved_usd'])==Decimal('15.60')
                assert state['last_round']['independent_cancel_error']=='owned_order_identity_mismatch'
                assert all(a['kind']=='buy' for a in state['last_round']['actions'])
                retained,=[t for t in evidence['plans'][0]['victims'] if t['token_id']=='m01']
                assert Decimal(retained['original_quantity'])==20 and Decimal(retained['quantity'])==19
                assert any(Decimal(o['original_size'])==21 for x in evidence['result_packets'] for o in x['packet']['orders'] if o['order_id']=='partial-buy')
                assert store.lp_actions(original_sid)==entry_audits
            else:
                from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
                requests = _wait_round_cancels(store, state)
                assert exchange.cancels==['partial-buy']
                cancel, = requests
                assert cancel['state']=='accepted' and cancel['order_id']=='partial-buy'
                cancel_audits = deepcopy(store.lp_actions('partial-session'))
                partial.update(status='CANCELED')
                advance_auto_wait(engine,monkeypatch)
                later=full(trade_generation_provider=store.lp_trade_generation)
                assert later['read_started_at']>packet['read_ended_at']
                publication=lp.register_account_snapshot(later);evidence['publications'].append(deepcopy(publication))
                assert publication['state']=='registered',publication
                settled=engine.lp_auto_run_once();capture('later-current-terminal-authority')
                assert settled['slots']['occupied']==1
                assert Decimal(settled['funds']['buy_reserved_usd'])==8
                assert Decimal(settled['funds']['inventory_cost_usd'])==Decimal('.40')
                assert exchange.cancels==['partial-buy'] and len(exchange.posts)==1
                assert store.lp_actions(original_sid)==entry_audits
                assert store.lp_actions('partial-session')==cancel_audits
                assert not store.lp_session('partial-session').get('entry_cancel_requested')
                assert [o['order_id'] for o in settled['account_buys']]==['o1']
                assert partial['size_matched']=='1' and partial['original_size']=='20'
                assert Decimal(exchange.positions[0]['size'])==1
                evidence['historical_order_history_after_current_proof']=deepcopy(store.lp_session('partial-session')['order_history'])
            assert not any(p['side']=='SELL' for p in exchange.posts)
        finally:
            evidence.update(current=engine.lp_auto_state(),original_order=original_order,entry_audits=entry_audits,
                sessions=store.lp_sessions(),audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
                posts=exchange.posts,cancels=exchange.cancels,orders=exchange.orders)
            (tmp_path/'CI-G-partial-native-proof.json').write_text(json.dumps(evidence,default=str,indent=2))
        return
    engine, exchange, lp, store, _ = rotation_pool(tmp_path, monkeypatch)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    partial = dict(order_id='partial-buy', token_id='m01', condition_id='m01', side='BUY',
        status='LIVE', price='.4', original_size='20', size_matched='1')
    exchange.orders.append(partial)
    exchange.positions = [dict(token_id='m01', condition_id='m01', size='1')]
    store.lp_create_session('partial-session', 'manual-partial', state='entry_open', payload=dict(
        account_id='test-wallet', condition_id='m01', token_id='m01', market_id='m01', outcome='YES',
        price='.4', quantity='20', entry_order_id='partial-buy', owned_order_ids=['partial-buy'],
        order_history={'partial-buy': partial}, buy_filled_quantity='1', residual_quantity='1'))
    partial_fact = {**buy('partial-buy', token='m01', reserved='7.6'), 'session_id': 'partial-session',
        'quantity': '19', 'filled_quantity': '1', 'financial_status': partial_status}
    def include_partial(d):
        facts = d['account_financial_facts']
        facts['buys'].append(partial_fact)
        facts.update(inventory_cost_usd='.4', trade_generation=store.lp_trade_generation(),
                     financial_status=partial_status, reason_codes=[] if partial_status == 'known' else ['trade_fee_unknown'])
    engine._auto_pool._update(include_partial)
    if mismatched_original_size:
        def actual_result(request):
            packet = result_reader(request)
            for row in packet['orders'] + packet['open_orders']:
                if row['order_id'] == 'partial-buy':
                    row['original_size'] = '21'
            return packet
        exchange.lp_order_result_snapshot = actual_result
    before = engine.lp_auto_state()
    assert before['slots']['occupied'] == 2 and before['target_buy_count'] == 1
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert state['slots']['occupied'] == 2
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('.4')
    assert len(exchange.posts) == 1
    assert not store.lp_session('partial-session').get('entry_cancel_requested')
    if mismatched_original_size:
        assert exchange.cancels == []
        assert state['active_plan'] and state['plan_wait']['kind'] == 'api'
        assert state['last_round']['reason'] == 'owned_order_identity_mismatch'
        retained = state['active_plan']['targets'][0]
        assert Decimal(retained['original_quantity']) == 20 and Decimal(retained['quantity']) == 19
        return
    if partial_status == 'unknown':
        assert exchange.cancels == []
        assert state['funds']['spendable_usd'] is None
    else:
        assert exchange.cancels == ['o1']
        assert state['slots']['canceling'] == 1
        assert state['slots']['active'] == 1
        exchange.orders[0]['status'] = 'CANCELED'
        def terminal(d):
            d['account_financial_facts'].update(buys=[partial_fact], order_fills={'o1': '0', 'partial-buy': '1'},
                trade_generation=store.lp_trade_generation())
        engine._auto_pool._update(terminal)
        advance_auto_wait(engine, monkeypatch)
        settled = engine.lp_auto_run_once()
        assert settled['slots']['occupied'] == 1
        assert Decimal(settled['funds']['buy_reserved_usd']) == Decimal('7.6')
        assert Decimal(settled['funds']['inventory_cost_usd']) == Decimal('.4')
        assert exchange.cancels == ['o1'] and len(exchange.posts) == 1


def test_older_captured_document_cannot_supersede_newer_wait(tmp_path, monkeypatch):
    from open_trader.polymarket_lp import LpObservationWait
    engine, exchange, lp, _ = pool.setup(tmp_path)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    auto = engine._auto_pool
    older = auto._read()
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    def invalid(**kwargs):
        raise LpObservationWait('account_round_invalid')
    exchange.lp_account_snapshot_shared = invalid
    assert auto._refresh_account_facts() is False
    assert 'account_round_invalid' in auto._projection(older)['admission_block_reasons']
    assert auto._projection(older)['funds']['spendable_usd'] is None
