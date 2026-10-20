"""Automatic BUYs converge to the highest-yield eligible markets, offline."""

from tests.test_lp_auto_pool import advance_auto_wait
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import threading
from types import SimpleNamespace

import pytest

from tests import test_lp_auto_pool as pool


class RotationExchange(pool.Exchange):
    def __init__(self):
        super().__init__()
        self.rewards = {}
        self.cancels = []

    def direction(self, token):
        direction = super().direction(token)
        direction['daily_pool_usd'] = self.rewards.get(token, Decimal('24'))
        # Public depth includes the account's live orders.
        own = sum((Decimal(str(o['original_size'])) - Decimal(str(o['size_matched']))
                   for o in self.orders if o['token_id'] == token
                   and o['side'] == 'BUY' and o['status'] == 'LIVE'), Decimal(0))
        direction['book']['bids'][0]['size'] = str(Decimal('1000') + own)
        return direction

    def lp_reward_catalog(self, **kwargs):
        result = super().lp_reward_catalog(**kwargs)
        for row in result['markets']:
            row['daily_pool_usd'] = self.rewards.get(row['condition_id'], Decimal('24'))
        return result

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        # A cancel acknowledgment deliberately does not manufacture a terminal receipt.
        return {'canceled': [order_id]}


def setup(tmp_path, monkeypatch, count=10, target=5, budget='100'):
    monkeypatch.setattr(pool, 'Exchange', RotationExchange)
    engine, exchange, lp, store = pool.setup(tmp_path, count)
    engine.lp_auto_configure(dict(budget_usd=budget, target_buy_count=target))
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once()
    return engine, exchange, lp, store


def refresh(lp, exchange, count=10):
    for index in range(count):
        token = f'm{index:02}'
        lp._candidate_pool_record_success(token, dict(condition_id=token), judged_at=pool.NOW,
            facts=dict(directions=[exchange.direction(token)], account=exchange.lp_account_snapshot()))


def test_full_pool_replaces_every_market_outside_top_five_without_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = setup(tmp_path, monkeypatch)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    for index in range(5, 10):
        exchange.rewards[f'm{index:02}'] = Decimal('24.000001')
    refresh(lp, exchange)

    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from copy import deepcopy
    from collections import Counter
    original = deepcopy(_wait_round_cancels(store, state))
    assert len(original) == 5
    assert set(exchange.cancels) == {'o1', 'o2', 'o3', 'o4', 'o5'}
    assert len(exchange.posts) == 5
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    fresh = engine.lp_auto_run_once()
    requests = _wait_round_cancels(store, fresh)
    assert len(requests) == 5
    assert {a['order_id'] for a in requests} == {'o1', 'o2', 'o3', 'o4', 'o5'}
    assert {a['action_key'] for a in requests}.isdisjoint(a['action_key'] for a in original)
    assert all(next(a for a in store.lp_actions(old['session_id']) if a['action_key'] == old['action_key']) == old for old in original)
    assert Counter(exchange.cancels) == Counter({oid: 2 for oid in ('o1','o2','o3','o4','o5')})
    assert fresh['slots']['occupied'] == 5 and Decimal(fresh['funds']['buy_reserved_usd']) == 40
    assert len(exchange.posts) == 5

    for order in exchange.orders:
        order['status'] = 'CANCELED'
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert {o['token_id'] for o in exchange.orders if o['status'] == 'LIVE'} == {
        'm05', 'm06', 'm07', 'm08', 'm09'}
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40


def test_rotation_waits_for_first_real_shared_read_and_recovers_without_overbuying(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    from copy import deepcopy
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    engine.lp_auto_reconcile_unknown()

    slow_entered, fast_entered = threading.Event(), threading.Event()
    slow_release, fast_release = threading.Event(), threading.Event()
    slow_completed, fast_completed = threading.Event(), threading.Event()
    slow_returned, fast_returned = threading.Event(), threading.Event()
    read_errors = {}
    metadata = exchange.lp_market_metadata_fresh
    def blocked_metadata(ids, **kwargs):
        values = {str(value) for value in ids}
        for token, entered, release, completed in (
                ('m10', slow_entered, slow_release, slow_completed),
                ('m11', fast_entered, fast_release, fast_completed)):
            if token in values:
                try:
                    entered.set()
                    assert release.wait(5)
                    return metadata(ids, **kwargs)
                finally:
                    completed.set()
        return metadata(ids, **kwargs)
    exchange.lp_market_metadata_fresh = blocked_metadata

    def read(identity, returned):
        try:
            lp._read_candidate_facts(identity)
        except ValueError as error:
            read_errors[identity['token_id']] = str(error)
        finally:
            returned.set()
    slow = threading.Thread(target=read, args=(dict(condition_id='m10', token_id='m10', outcome='YES'), slow_returned))
    fast = threading.Thread(target=read, args=(dict(condition_id='m11', token_id='m11', outcome='YES'), fast_returned))

    from open_trader import polymarket_lp as lp_module
    original_wait = lp_module.wait
    wait_entered = threading.Event()
    def observed_wait(futures, **kwargs):
        wait_entered.set()
        return original_wait(futures, **kwargs)
    monkeypatch.setattr(lp_module, 'wait', observed_wait)

    round_thread = None
    try:
        lp._market_read_timeout = 1
        slow.start()
        assert slow_entered.wait(2)
        fast.start()
        assert fast_entered.wait(2)
        with lp._market_reads_lock:
            slow_job, fast_job = lp._market_reads['m10'], lp._market_reads['m11']
        state = engine.lp_auto_scheduled_check()
        assert state['last_round']['reason'] == 'market_read_capacity'
        assert [row['condition_id'] for row in state['last_round']['blocked']] == ['m00']
        assert exchange.cancels == []
        assert len(exchange.posts) == 1
        assert slow_returned.wait(2) and fast_returned.wait(2)
        assert read_errors == {'m10': 'market_read_timeout', 'm11': 'market_read_timeout'}
        # Caller timeout does not finish either real external metadata read.
        assert not slow_completed.is_set() and not fast_completed.is_set()
        assert not slow_release.is_set() and not fast_release.is_set()

        advance_auto_wait(engine, monkeypatch)
        engine.lp_auto_reconcile_unknown()
        published = lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))
        assert published['state'] == 'registered', published
        account_reads = []
        def complete_account_round(*, max_age_seconds=0, trade_generation_provider=None):
            del max_age_seconds
            pool.NOW += timedelta(microseconds=1)
            snapshot = pool._fresh_registration_bundle(exchange, lp)
            if trade_generation_provider is not None:
                snapshot['trade_generation'] = trade_generation_provider()
            assert snapshot['trade_generation'] == lp.store.lp_trade_generation()
            account_reads.append(deepcopy(snapshot))
            return snapshot
        exchange.lp_account_snapshot_shared = complete_account_round
        wait_entered.clear()
        checked = []
        def check():
            checked.append(engine.lp_auto_scheduled_check())
        round_thread = threading.Thread(target=check)
        round_thread.start()
        assert wait_entered.wait(2), str([x['last_round'] for x in checked])
        assert not slow_completed.is_set() and not fast_completed.is_set()
        assert not slow_release.is_set() and not fast_release.is_set()
        assert exchange.cancels == [] and len(exchange.posts) == 1
        fast_release.set()
        assert fast_completed.wait(2)
        round_thread.join(5)
        assert checked
        assert not slow_completed.is_set() and not slow_release.is_set()
        deferred = checked[0]
        assert not deferred['last_round'].get('cancel_registration')
        assert exchange.cancels == [] and len(exchange.posts) == 1
        assert deferred['slots']['occupied'] == 1 and Decimal(deferred['funds']['buy_reserved_usd']) == 8
        # SDK-finally is earlier than the actual read Future completion.
        fast_job.result(timeout=2)
        assert fast_job.done() and not slow_job.done()
        state = engine.lp_auto_scheduled_check()
        request, = _wait_round_cancels(store, state)
        assert request['order_id']=='o1' and request['state']=='accepted'
        assert exchange.cancels == ['o1']
        assert len(exchange.posts) == 1
        assert state['slots']['occupied'] == 1
        assert state['funds']['status'] == 'unknown'
        assert state['funds']['available_usd'] is None
        assert state['active_plan'] is None and state['last_round']['completed_at']
        assert all(a['kind']=='buy' for a in state['last_round']['actions'])
    finally:
        lp._market_read_timeout = 10
        fast_release.set()
        slow_release.set()
        for thread, completed in ((slow, slow_completed), (fast, fast_completed)):
            if thread.ident is not None:
                assert completed.wait(2)
                thread.join(2)
                assert not thread.is_alive()
        if round_thread is not None:
            round_thread.join(2)
            assert not round_thread.is_alive()

    for order in exchange.orders:
        order['status'] = 'CANCELED'
    advance_auto_wait(engine, monkeypatch)
    reads_before_refill = len(account_reads)
    result_reads, result_requests = [], []
    def observed_result(request):
        result_requests.append(deepcopy(request))
        packet = result_reader(request)
        result_reads.append(packet)
        return packet
    exchange.lp_order_result_snapshot = observed_result
    state = engine.lp_auto_scheduled_check()
    assert {o['token_id'] for o in exchange.orders if o['status'] == 'LIVE'} == {'m01'}
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    refill_reads = account_reads[reads_before_refill:]
    assert len(refill_reads) == 1, refill_reads
    assert refill_reads[0]['balance_complete'] is True
    assert refill_reads[0]['open_orders_complete'] is True
    assert refill_reads[0]['positions_complete'] is True
    result,=result_reads
    assert result_requests==[{'owned_order_ids':['o1'],'condition_ids':['m00']}]
    assert result['authenticated'] is True and result['wallet_address']=='test-wallet'
    assert all(result[key] is True for key in ('open_orders_complete','pagination_complete','positions_complete','trades_complete'))
    assert result['read_started_at']<=result['read_ended_at']<=pool.NOW
    terminal,=[row for row in result['orders'] if row['order_id']=='o1']
    assert terminal['status']=='CANCELED' and terminal['side']=='BUY' and terminal['token_id']==terminal['condition_id']=='m00'
    assert terminal.get('fill_quantity_known') in (None,True) and Decimal(terminal['size_matched'])==0
    assert Decimal(terminal['price'])==Decimal('.40') and Decimal(terminal['original_size'])==20
    assert not [row for row in refill_reads[0]['open_orders'] if row['status']=='LIVE']
    assert refill_reads[0]['raw_trades']==[]
    exact, = refill_reads[0]['open_orders']
    assert exact['order_id']=='o1' and exact['status']=='CANCELED' and Decimal(exact['size_matched'])==0
    original_session = next(s for s in store.lp_sessions() if s.get('entry_order_id')=='o1')
    lp.reconcile_facts(original_session['session_id'])
    exact=store.lp_session(original_session['session_id'])['order_history']['o1']
    assert exact['status']=='CANCELED' and Decimal(exact['size_matched'])==0
    assert exact['side']=='BUY' and exact['token_id']=='m00' and Decimal(exact['price'])==Decimal('.40')
    assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01']
    assert exchange.posts[-1]['price'] == Decimal('.40') and exchange.posts[-1]['quantity'] == 20
    assert exchange.cancels == ['o1']


def test_rotation_reports_exact_block_when_financial_facts_expire(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    intent = engine.lp_auto_state()['intents'][0]
    intent_id = intent['intent_id']
    engine._auto_pool._update(lambda d: d['intents'][intent_id].update(
        checked_at=(pool.NOW - timedelta(seconds=61)).isoformat()))
    engine._auto_pool._reconcile_unknown = lambda **kwargs: engine._auto_pool.state()

    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert exchange.cancels == []
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert state['last_round']['reason'] == 'financial_facts_stale'
    assert state['last_round']['blocked'] == [
        {'condition_id': 'm00', 'token_id': 'm00', 'reason': 'financial_facts_stale'}]


def test_rotation_keeps_unknown_occupied_slots_blocked_and_ranks_known_actives(tmp_path, monkeypatch):
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=3, target=2)
    engine.lp_auto_set_desired_running(False)
    observed = engine.lp_auto_run_once(round_id='observe-known-resting-yields')
    assert len(exchange.posts) == 2 and exchange.cancels == []
    engine.lp_auto_set_desired_running(True)
    exchange.rewards['m02'] = Decimal('25')
    refresh(lp, exchange, 3)
    intents = engine.lp_auto_state()['intents']
    engine._auto_pool._update(lambda d: d['intents'][intents[0]['intent_id']].update(
        state='unknown', financial_status='unknown', reconcile_reason='missing_reliable_order_id'))
    targets, _, _, blocked = engine._auto_pool._ranked_buys(engine._auto_pool.state())
    assert blocked == [{'condition_id': 'm00', 'token_id': 'm00', 'reason': 'missing_reliable_order_id'}]
    assert [t['condition_id'] for t in targets] == ['m00', 'm02']
    assert targets[0]['retained_constraint'] is True

    def all_unknown(document):
        for intent in document['intents'].values():
            intent.update(state='unknown', financial_status='unknown',
                          reconcile_reason='missing_reliable_order_id')
    engine._auto_pool._update(all_unknown)
    engine._auto_pool._reconcile_unknown = lambda **kwargs: engine._auto_pool.state()
    advance_auto_wait(engine, monkeypatch, refresh=False)
    state = engine.lp_auto_run_once()
    assert len(exchange.posts) == 2
    assert state['slots']['occupied'] == 2
    assert state['last_round']['reason'] == 'missing_reliable_order_id'
    assert [row['reason'] for row in state['last_round']['blocked']] == [
        'missing_reliable_order_id', 'missing_reliable_order_id']


def test_rotation_registers_all_victims_and_isolates_unresolved_terminal(tmp_path, monkeypatch):
    import json
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    import sqlite3
    from copy import deepcopy
    from collections import Counter
    monkeypatch.setattr(pool,'NOW',pool.NOW)
    engine,exchange,lp,store=setup(tmp_path,monkeypatch)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader=install_order_result_reader(exchange,monkeypatch)
    originals={o['token_id']:deepcopy(o) for o in exchange.orders}
    assert set(originals)=={f'm{i:02}' for i in range(5)}
    assert Counter(p['token_id'] for p in exchange.posts)==Counter(originals.keys())
    intents=deepcopy(engine.lp_auto_state()['intents'])
    original_entries={i['session_id']:deepcopy(store.lp_actions(i['session_id'])) for i in intents}
    revisions={i['session_id']:store.lp_session_revision(i['session_id'],trading=True) for i in intents}
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
    for index in range(5,10):exchange.rewards[f'm{index:02}']=Decimal('25')
    advance_auto_wait(engine,monkeypatch)
    refresh(lp,exchange)
    observed=[]; observation_lock=threading.Lock();cancel=exchange.cancel_order
    def unlocked():
        lock=engine._acquire_global_lock()
        if lock is None:return False
        acquired=lp._mutex.acquire(blocking=False)
        if acquired:lp._mutex.release()
        engine._release_global_lock(lock)
        return acquired
    def cancel_after_registration(order_id):
        with observation_lock:
            if not observed:
                state=deepcopy(engine.lp_auto_state())
                audits={i['session_id']:deepcopy(store.lp_actions(i['session_id'])) for i in intents}
                pending=[a for rows in audits.values() for a in rows if a.get('role')=='reconciliation_cancel']
                revisions_changed=all(store.lp_session_revision(sid,trading=True)>rev for sid,rev in revisions.items())
                with ThreadPoolExecutor(1) as workers:locks_free=workers.submit(unlocked).result(timeout=2)
                observed.append(dict(state=state,audits=audits,pending=pending,revisions_changed=revisions_changed,locks_free=locks_free,first_order_id=order_id))
        return cancel(order_id)
    monkeypatch.setattr(exchange,'cancel_order',cancel_after_registration)
    try:
        phase[0]='original_five_cancel_registration'
        state=engine.lp_auto_run_once();closed(state);_wait_round_cancels(store,state);first=capture('original_five_locally_complete_no_buy')
        evidence['registration_before_first_venue_call']=observed
        assert len(observed)==1
        native=observed[0]
        assert len(native['pending'])==5 and {a['order_id'] for a in native['pending']}=={o['order_id'] for o in originals.values()}
        assert all(a['state']=='pending' for a in native['pending'])
        assert native['revisions_changed'] is True and native['locks_free'] is True
        assert native['state']['funds']['status']=='unknown' and native['state']['slots']['canceling']==0 and native['state']['slots']['occupied']==5
        assert Decimal(native['state']['funds']['buy_reserved_usd'])==40
        assert Counter(exchange.cancels)==Counter(o['order_id'] for o in originals.values()) and len(exchange.posts)==5
        original_cancels={i['session_id']:deepcopy(store.lp_actions(i['session_id'])) for i in intents}
        for sid,rows in original_entries.items():
            for row in rows:assert next(a for a in original_cancels[sid] if a['action_key']==row['action_key'])==row
        for row in exchange.orders:
            if row['token_id'] in {f'm{i:02}' for i in range(4)}:row['status']='CANCELED'
        last_id=originals['m04']['order_id']
        phase[0]='current_four_terminal_one_live'
        advance_auto_wait(engine,monkeypatch)
        state=engine.lp_auto_run_once();closed(state);second=capture('new_current_four_buys')
        assert second['last_round']['round_id']!=first['last_round']['round_id']
        assert len(exchange.posts)==9 and state['slots']['occupied']==5 and Decimal(state['funds']['buy_reserved_usd'])==40
        assert next(o for o in exchange.orders if o['order_id']==last_id)['status']=='LIVE'
        newposts=exchange.posts[5:]
        assert len(newposts)==4 and len({p['token_id'] for p in newposts})==4 and {p['token_id'] for p in newposts}<={f'm{i:02}' for i in range(5,10)}
        for sid,rows in original_cancels.items():
            for row in rows:assert next(a for a in store.lp_actions(sid) if a['action_key']==row['action_key'])==row
        fresh=_wait_round_cancels(store,second)
        assert all(a['order_id']==last_id for a in fresh)
        if fresh:
            action,=fresh
            old_audits=[a for rows in original_cancels.values() for a in rows if a.get('role')=='reconciliation_cancel' and a['order_id']==last_id]
            new,=[a for a in store.lp_actions(action['session_id']) if a.get('rotation_round_id')==second['last_round']['round_id']]
            assert new['action_id']!=old_audits[0]['action_id'] and new['action_key']!=old_audits[0]['action_key']
            assert new['order_id']==last_id and new['rotation_round_id']!=old_audits[0]['rotation_round_id']
        before_final=deepcopy({s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()})
        next(o for o in exchange.orders if o['order_id']==last_id)['status']='CANCELED'
        phase[0]='following_current_final_terminal'
        advance_auto_wait(engine,monkeypatch)
        state=engine.lp_auto_run_once();closed(state);third=capture('new_current_fifth_buy')
        assert third['last_round']['round_id'] not in (first['last_round']['round_id'],second['last_round']['round_id'])
        assert len(exchange.posts)==10 and Counter(p['token_id'] for p in exchange.posts)==Counter(f'm{i:02}' for i in range(10))
        assert {o['token_id'] for o in exchange.orders if o['status']=='LIVE'}=={f'm{i:02}' for i in range(5,10)}
        assert state['slots']['occupied']==5 and Decimal(state['funds']['buy_reserved_usd'])==40
        assert all(p['side']=='BUY' and Decimal(p['price'])==Decimal('.40') and Decimal(p['quantity'])==20 and p['post_only'] is True for p in exchange.posts)
        for sid,rows in before_final.items():
            for row in rows:assert next(a for a in store.lp_actions(sid) if a['action_key']==row['action_key'])==row
        phase[0]='explicit_final_complete_publication'
        pool.NOW+=timedelta(microseconds=1)
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        published=capture('complete_final_known_40')
        assert published['funds']['status']=='known' and Decimal(published['funds']['buy_reserved_usd'])==40 and published['slots']['occupied']==5
        assert not any(x['active_plan'] for x in evidence['financial_reads'])
        evidence['completed']='full'
    finally:save('KF-registration-diagnostics.json')

def test_second_cancel_registration_failure_sends_nothing_and_recovers_safely(tmp_path, monkeypatch):
    import sqlite3
    import json
    import uuid
    from pathlib import Path
    from collections import Counter
    from copy import deepcopy
    from datetime import datetime
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_auto_plan_execution import install_order_result_reader

    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = setup(tmp_path, monkeypatch)
    result_reader = install_order_result_reader(exchange, monkeypatch)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    originals = deepcopy(exchange.orders)
    assert {o['order_id'] for o in originals} == {'o1', 'o2', 'o3', 'o4', 'o5'}
    assert len(originals) == 5 and len(exchange.posts) == 5
    assert all(o['side'] == 'BUY' and o['status'] == 'LIVE' and Decimal(o['price']) == Decimal('.40')
        and Decimal(o['original_size']) == 20 and Decimal(o['size_matched']) == 0 for o in originals)
    original_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    targets = {'o1', 'o2', 'o3', 'o4', 'o5'}
    ordered = []
    for session in store.lp_sessions():
        owned = targets & {session.get('entry_order_id'), *session.get('augment_order_ids', [])}
        if owned:
            assert len(owned) == 1, 'This original fixture must have one owned target per session'
            ordered.append((session['session_id'], next(iter(owned))))
    assert len(ordered) == 5 and {oid for _, oid in ordered} == targets
    first_sid, first_oid = ordered[0]
    second_sid, second_oid = ordered[1]
    assert len(first_sid) == len(second_sid) == 32 and all(c in '0123456789abcdef' for c in first_sid + second_sid)
    fault_message = 'C5 exact second cancel registration'
    with sqlite3.connect(store.path) as connection:
        connection.execute(f"""CREATE TRIGGER c5_exact_second_cancel BEFORE INSERT ON lp_actions
            WHEN NEW.session_id='{second_sid}' AND json_extract(NEW.payload,'$.order_id')='{second_oid}'
            AND json_extract(NEW.payload,'$.role')='reconciliation_cancel'
            AND json_extract(NEW.payload,'$.rotation_round_id')=(SELECT json_extract(payload,'$.active_plan.round_id') FROM lp_auto_pool WHERE singleton=1)
            AND (SELECT json_extract(payload,'$.active_plan.cancel_registration.completed') FROM lp_auto_pool WHERE singleton=1)=0
            AND EXISTS (SELECT 1 FROM lp_actions a WHERE a.session_id='{first_sid}' AND a.state='pending'
                AND json_extract(a.payload,'$.order_id')='{first_oid}' AND json_extract(a.payload,'$.role')='reconciliation_cancel'
                AND json_extract(a.payload,'$.rotation_round_id')=json_extract(NEW.payload,'$.rotation_round_id'))
            BEGIN SELECT RAISE(ABORT, '{fault_message}'); END""")
    errors, before_failure, packets, financial_reads, financial_packets = [], [], [], [], []
    connect = sqlite3.connect
    class FaultObservationConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            try:
                return super().execute(sql, parameters)
            except sqlite3.IntegrityError as error:
                if str(error) == fault_message:
                    errors.append(dict(type=type(error).__name__, code=error.sqlite_errorname))
                    cursor = super().execute("SELECT * FROM lp_actions WHERE session_id=? AND state='pending' AND json_extract(payload,'$.role')='reconciliation_cancel'", (first_sid,))
                    names = [column[0] for column in cursor.description]
                    before_failure.extend(dict(zip(names, row)) for row in cursor.fetchall())
                    (tmp_path / 'C5-native-registration-failure.json').write_text(json.dumps(
                        dict(errors=errors, first_pending_rows=before_failure, first_target=[first_sid,first_oid], second_target=[second_sid,second_oid]), indent=2) + '\n')
                raise
    def observed_connect(database, *args, **kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs.setdefault('factory', FaultObservationConnection)
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', observed_connect)
    def results(request):
        packet = result_reader(request)
        packets.append((deepcopy(request), deepcopy(packet)))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
    account_read = exchange.lp_account_snapshot
    def observed_account():
        financial_reads.append(pool.NOW)
        packet = account_read()
        financial_packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_account_snapshot', observed_account)
    for index in range(5, 10):
        exchange.rewards[f'm{index:02}'] = Decimal('25')
    refresh(lp, exchange)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    first = deepcopy(state['last_round'])
    assert errors == [dict(type='IntegrityError', code='SQLITE_CONSTRAINT_TRIGGER')]
    assert len(before_failure) == 1
    first_native = before_failure[0]
    first_payload = json.loads(first_native['payload'])
    assert first_payload['order_id'] == first_oid and first_payload['rotation_round_id'] == first['round_id']
    assert first_native['state'] == 'pending'
    assert first['completed_at'] and state['active_plan'] is None
    cancel_actions = [a for a in first['actions'] if a['kind'] == 'cancel']
    assert cancel_actions == [], 'Cancel registration belongs to its own path'
    assert fault_message in first['independent_cancel_error']
    assert all(Decimal(t['price']) == Decimal('.40') and Decimal(t['quantity']) == 20 for t in first['targets'])
    assert exchange.cancels == [] and len(exchange.posts) == 5 and exchange.orders == originals
    assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 40
    original_requests = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])
        if a.get('role') == 'reconciliation_cancel']
    first_audit, = original_requests
    assert first_audit['state'] == 'rejected' and first_audit['post_started'] is False and first_audit['submit_stage'] == 'pre_send_rejected'
    assert first_audit['action_id'] == first_native['action_id'] and first_audit['action_key'] == first_native['action_key']
    assert first_audit['created_at'] == first_native['created_at'] and first_audit['session_id'] == first_sid
    assert all(first_audit[k] == v for k, v in first_payload.items())
    completed = datetime.fromisoformat(first['completed_at'])

    ended = deepcopy(first_audit)
    before = (len(financial_reads), len(packets), deepcopy(exchange.posts))
    engine.lp_auto_set_desired_running(False)
    scheduler.request_check()
    assert scheduler.run_due()
    current = engine.lp_auto_state()
    assert current['last_round']['round_id'] != first['round_id']
    assert current['active_plan'] is None and current['last_round']['actions'] == []
    assert len(financial_reads) > before[0] and exchange.posts == before[2] and exchange.cancels == []
    assert next(a for a in store.lp_actions(first_sid) if a['action_key'] == ended['action_key']) == ended
    engine.lp_auto_set_desired_running(True)
    with sqlite3.connect(store.path) as connection:
        connection.execute('DROP TRIGGER c5_exact_second_cancel')
    cancel = exchange.cancel_order
    observed = []
    from threading import Lock
    observation_lock = Lock()
    def unlocked():
        lock = engine._acquire_global_lock()
        if lock is None:
            return False
        try:
            acquired = lp._mutex.acquire(blocking=False)
            if acquired:
                lp._mutex.release()
            return acquired
        finally:
            engine._release_global_lock(lock)
    def all_registered_before_send(order_id):
        with observation_lock:
            if not observed:
                current_state = engine.lp_auto_state()
                current = deepcopy(current_state['active_plan'] or current_state['last_round'])
                assert current and current['round_id'] != first['round_id']
                assert current['cancel_registration']['completed'] is True
                actions = [a for a in current['actions'] if a['kind'] == 'cancel']
                assert actions == []
                assert set(current['cancel_registration']['sessions']) == targets
                assert {a['action_id'] for a in actions}.isdisjoint({a['action_id'] for a in cancel_actions})
                audits = [a for sid in original_audits for a in store.lp_actions(sid)
                    if a.get('role') == 'reconciliation_cancel' and a.get('rotation_round_id') == current['round_id']]
                assert len(audits) == 5 and {a['order_id'] for a in audits} == targets
                assert all(a['state'] in ('pending', 'accepted') for a in audits)
                assert len({a['action_id'] for a in audits}) == len({a['action_key'] for a in audits}) == 5
                for audit in audits:
                    uuid.UUID(audit['action_id'])
                    assert audit['action_id'] != ended['action_id'] and audit['action_key'] != ended['action_key']
                    assert audit['session_id'] == dict((oid, sid) for sid, oid in ordered)[audit['order_id']]
                assert next(a for a in store.lp_actions(first_sid) if a['action_key'] == ended['action_key']) == ended
                with ThreadPoolExecutor(1) as workers:
                    assert workers.submit(unlocked).result(timeout=5)
                observed.append(dict(round_id=current['round_id'], actions=actions, audits=deepcopy(audits)))
        return cancel(order_id)
    monkeypatch.setattr(exchange, 'cancel_order', all_registered_before_send)
    refresh(lp, exchange)
    assert scheduler.run_due()
    from tests.test_lp_auto_independent_cancel_contract import _wait_audits
    _wait_audits(store, lambda rows: len(rows) == 6 and all(a['state'] != 'pending' for a in rows))
    second = deepcopy(engine.lp_auto_state()['last_round'])
    assert len(observed) == 1 and second['round_id'] == observed[0]['round_id'] and second['completed_at']
    assert Counter(exchange.cancels) == Counter(targets) and len(exchange.posts) == 5
    assert all(o['status'] == 'LIVE' for o in exchange.orders)
    assert engine.lp_auto_state()['slots']['occupied'] == 5 and Decimal(engine.lp_auto_state()['funds']['buy_reserved_usd']) == 40
    assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity'
        for a in second['actions'] if a['kind'] == 'buy')
    for order in exchange.orders:
        order['status'] = 'CANCELED'
    completed2 = datetime.fromisoformat(second['completed_at'])
    refresh(lp, exchange)
    assert scheduler.run_due()
    final = engine.lp_auto_state()
    assert final['last_round']['round_id'] not in (first['round_id'], second['round_id']) and final['last_round']['completed_at']
    assert Counter(exchange.cancels) == Counter(targets) and len(exchange.posts) == 10
    assert {o['token_id'] for o in exchange.orders if o['status'] == 'LIVE'} == {f'm{i:02}' for i in range(5, 10)}
    assert final['slots']['occupied'] == 5 and Decimal(final['funds']['buy_reserved_usd']) == 40
    terminal_packets = [packet for _, packet in packets if all(any(o['order_id'] == oid and o['status'] == 'CANCELED'
        and Decimal(o['size_matched']) == 0 and Decimal(o['price']) == Decimal('.40') and Decimal(o['original_size']) == 20
        for o in packet['orders']) for oid in targets)]
    assert any(packet.get('open_orders_complete') is True
        and not any(o['order_id'] in targets and o.get('status') == 'LIVE' for o in packet['open_orders'])
        for packet in financial_packets), 'Current complete account facts prove released BUY capacity'
    assert all(o['status'] == 'CANCELED' and Decimal(o['size_matched']) == 0
        for o in exchange.orders if o['order_id'] in targets)
    assert next(a for a in store.lp_actions(first_sid) if a['action_key'] == ended['action_key']) == ended
    for sid, audits in original_audits.items():
        current = store.lp_actions(sid)
        for audit in audits:
            assert next(a for a in current if a['action_key'] == audit['action_key']) == audit


def test_partial_fill_during_cancel_aborts_replacement_and_keeps_inventory_cost(tmp_path, monkeypatch):
    import json
    import sqlite3
    from copy import deepcopy
    from collections import Counter
    monkeypatch.setattr(pool,'NOW',pool.NOW)
    engine,exchange,lp,store=setup(tmp_path,monkeypatch,count=2,target=1)
    entry_round=deepcopy(engine.lp_auto_state()['last_round'])
    original_order=deepcopy(exchange.orders[0]);sid=entry_round['actions'][0]['session_id']
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader=install_order_result_reader(exchange,monkeypatch)
    sell=dict(order_id='self-managed-sell',token_id='legacy',condition_id='legacy',market_id='legacy',outcome='YES',
        side='SELL',status='LIVE',price='.30',original_size='13.31',size_matched='0')
    exchange.orders.append(sell)
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
    try:
        phase[0]='new_current_original_cancel'
        advance_auto_wait(engine,monkeypatch)
        refresh(lp,exchange,2)
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
        assert next(o for o in exchange.orders if o['order_id']=='self-managed-sell')==sell
        engine.lp_auto_set_desired_running(False)
        counts=(len(evidence['financial_reads']),len(evidence['result_reads']),len(evidence['full_reads']))
        engine.lp_auto_run_once()
        assert len(evidence['financial_reads']) > counts[0] and len(evidence['full_reads']) > counts[2]
        current = engine.lp_auto_state()
        assert current['active_plan'] is None and current['last_round']['actions'] == []
        assert Decimal(current['funds']['buy_reserved_usd']) == 8 and Decimal(current['funds']['inventory_cost_usd']) == Decimal('3.20')
        assert len(exchange.posts)==2 and exchange.cancels==[original_order['order_id']] and store.lp_actions(sid)==old_audits
        assert not any(x['active_plan'] for x in evidence['financial_reads'])
        evidence['publication_results']=publication_results;evidence['completed']='full'
    finally:save('KF-partial-terminal-diagnostics.json')

@pytest.mark.parametrize('guard', ['stale_reward', 'stale_book', 'incomplete_account', 'protection', 'pause', 'breaker', 'review', 'unknown_order'])
def test_rotation_keeps_existing_buy_when_guard_blocks(tmp_path, monkeypatch, guard):
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    install_order_result_reader(exchange, monkeypatch)
    session_id = engine.lp_auto_state()['intents'][0]['session_id']
    if guard == 'stale_reward':
        read = exchange.lp_reward_catalog
        def stale(**kwargs):
            result = read(**kwargs)
            for row in result['markets']:
                row['reward_checked_at'] = pool.NOW - timedelta(seconds=61)
            return result
        exchange.lp_reward_catalog = stale
    elif guard == 'stale_book':
        read = exchange.lp_order_books
        def stale(ids, **kwargs):
            result = read(ids, **kwargs)
            for book in result.values():
                book['received_at'] = pool.NOW - timedelta(seconds=61)
            return result
        exchange.lp_order_books = stale
    elif guard == 'incomplete_account':
        read = exchange.lp_account_snapshot
        exchange.lp_account_snapshot = lambda: {**read(), 'open_orders_complete': False}
    elif guard == 'protection':
        session = store.lp_session(session_id)
        protection = session['queue_protection']
        for bucket in protection.get('levels', {'entry': protection}).values():
            bucket['state'] = 'triggered'
        store.lp_update_session(session_id, patch={'queue_protection': protection})
    elif guard in ('pause', 'breaker'):
        read = exchange.lp_reward_catalog
        def interrupt(**kwargs):
            if guard == 'pause':
                engine.lp_auto_set_desired_running(False)
            else:
                engine._breaker_open = True
            return read(**kwargs)
        exchange.lp_reward_catalog = interrupt
    elif guard == 'review':
        store.lp_update_session(session_id, patch={'review_at': pool.NOW.isoformat()})
    else:
        exchange.orders[0]['status'] = 'UNKNOWN'

    state = engine.lp_auto_run_once()
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    _wait_round_cancels(store, state)
    assert exchange.cancels == (['o1'] if guard in ('protection', 'review') else [])
    assert len(exchange.posts) == 1
    if guard == 'stale_reward':
        observation = store.lp_session(session_id)['buy_yield_observations']['o1']
        assert observation['state'] == 'unknown' and observation['error'] == 'ValueError'
        assert state['last_round']['reason'] == 'rotation_yield_unknown'
        assert state['last_round']['blocked'] == [
            {'condition_id': 'm00', 'token_id': 'm00', 'reason': 'rotation_yield_unknown'}]


def test_equal_yields_keep_resting_orders_without_counting_them_as_competition(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch)
    refresh(lp, exchange)
    for _ in range(2):
        engine.lp_auto_run_once()
    assert exchange.cancels == []
    assert len(exchange.posts) == 5


def test_cached_leader_is_reestimated_before_any_cancel(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    exchange.rewards['m01'] = Decimal('23')
    engine.lp_auto_run_once()
    assert exchange.cancels == []
    assert len(exchange.posts) == 1


def test_cancel_timeout_survives_restart_and_reselects_latest_leader_after_terminal(tmp_path, monkeypatch):
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from copy import deepcopy
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=3, target=1)
    entry = deepcopy(engine.lp_auto_state()['last_round'])
    original_sid = entry['actions'][0]['session_id']
    original_order = exchange.orders[0]
    assert original_order['order_id'] == 'o1' and original_order['token_id'] == 'm00'
    assert original_order['side'] == 'BUY' and Decimal(original_order['price']) == Decimal('.40')
    assert Decimal(original_order['original_size']) == 20 and Decimal(original_order['size_matched']) == 0
    original_order['fill_quantity_known'] = True
    result_reader = install_order_result_reader(exchange, monkeypatch)
    packets, financial_reads = [], []
    def result(request):
        packet = result_reader(request)
        packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', result)
    account_packets = []
    def current_account(*, max_age_seconds=0, trade_generation_provider=None):
        del max_age_seconds
        started=pool.NOW
        packet=pool._fresh_registration_bundle(exchange, engine._lp)
        pool.NOW+=timedelta(microseconds=1)
        packet.update(read_started_at=started,read_ended_at=pool.NOW,checked_at=pool.NOW,
            open_orders=deepcopy([o for o in exchange.orders if o['status']=='LIVE']),
            positions=deepcopy(exchange.positions),raw_trades=deepcopy(exchange.trades))
        if trade_generation_provider is not None:packet['trade_generation']=trade_generation_provider()
        account_packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', current_account, raising=False)
    account = exchange.lp_account_snapshot
    def financial():
        financial_reads.append(dict(at=pool.NOW, active=engine.lp_auto_state()['active_plan'] is not None))
        return account()
    monkeypatch.setattr(exchange, 'lp_account_snapshot', financial)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 3)
    def timeout(order_id):
        exchange.cancels.append(order_id)
        raise TimeoutError()
    exchange.cancel_order = timeout
    first = engine.lp_auto_run_once()
    original = deepcopy(first['last_round'])
    old_action, = _wait_round_cancels(store, first, expected_states={'o1': 'unknown'})
    assert all(a['kind']=='buy' and a['state']=='not_sent' and a['reason']=='next_round_capacity' for a in original['actions'])
    assert old_action['order_id'] == 'o1' and old_action['session_id'] == original_sid
    assert old_action['state'] == 'unknown'
    assert first['active_plan'] is None and original['completed_at']
    audits = deepcopy(store.lp_actions(original_sid))
    unknown, = [a for a in audits if a['role'] == 'reconciliation_cancel']
    assert unknown['state'] == 'unknown' and unknown['post_started'] is True
    assert unknown['order_id'] == 'o1' and unknown['rotation_round_id'] == original['round_id']
    assert unknown['submit_post_started_at'] and unknown['action_id']
    assert unknown['action_key'].startswith(original_sid + ':reconciliation-cancel:')
    completed = pool.datetime.fromisoformat(original['completed_at'])
    deadline = completed
    assert first['plan_wait'] is None
    restarted = pool.PredictionExecutionService(store=store, monitor=SimpleNamespace(), trading=exchange,
        notifier=SimpleNamespace(), lock_path=tmp_path/'execution.lock', lp=lp)
    restarted._breaker_open = False
    engine = restarted
    before = (len(financial_reads), len(packets))
    restarted.lp_auto_set_desired_running(False)
    for elapsed in (1, 29):
        state = restarted.lp_auto_run_once()
        assert state['last_round']['round_id'] != original['round_id']
        assert len(financial_reads) > before[0]
        assert exchange.cancels == ['o1'] and len(exchange.posts) == 1
        assert state['slots']['occupied'] == 1 and Decimal(state['funds']['buy_reserved_usd']) == 8
        assert store.lp_actions(original_sid) == audits
    restarted.lp_auto_set_desired_running(True)
    original_order['status'] = 'CANCELED'
    exchange.rewards['m02'] = Decimal('26')
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
    refresh(lp, exchange, 3)
    state = restarted.lp_auto_run_once()
    assert len(financial_reads) > before[0] and not any(r['active'] for r in financial_reads)
    packet = account_packets[-1]
    assert all(packet[k] is True for k in ('authenticated','balance_complete','open_orders_complete',
        'pagination_complete','positions_complete','trades_complete'))
    assert packet['read_started_at'] >= deadline
    assert not any(r['order_id']=='o1' for r in packet['open_orders'])
    assert original_order['status']=='CANCELED' and original_order['fill_quantity_known'] is True
    assert original_order['side']=='BUY' and original_order['token_id']==original_order['condition_id']=='m00'
    assert Decimal(original_order['price'])==Decimal('.40') and Decimal(original_order['original_size'])==20
    assert Decimal(original_order['size_matched'])==0
    assert [o['token_id'] for o in exchange.orders if o['status'] == 'LIVE'] == ['m02'], state['last_round']
    assert [p['token_id'] for p in exchange.posts] == ['m00', 'm02']
    assert exchange.cancels == ['o1']
    confirmed_audits = deepcopy(store.lp_actions(original_sid))
    assert len(confirmed_audits) == len(audits)
    confirmed, = [a for a in confirmed_audits if a['action_key'] == unknown['action_key']]
    assert unknown['state'] == confirmed['state'] == 'unknown'
    assert confirmed == unknown, 'Current account facts preserve the original ambiguous cancel audit'
    assert {k: v for k, v in confirmed.items() if k not in ('state', 'updated_at')} == {
        k: v for k, v in unknown.items() if k not in ('state', 'updated_at')}
    for original_audit in audits:
        if original_audit['action_key'] != unknown['action_key']:
            assert next(a for a in confirmed_audits if a['action_key'] == original_audit['action_key']) == original_audit
    assert state['last_round']['round_id'] != original['round_id'] and state['active_plan'] is None
    assert state['last_round']['completed_at']
    new_buy, = [a for a in state['last_round']['actions'] if a['kind'] == 'buy']
    assert new_buy['token_id'] == 'm02' and new_buy['state'] == 'success'
    assert new_buy['session_id'] != original_sid and new_buy['action_id'] not in (old_action['action_id'], entry['actions'][0]['action_id'])
    assert new_buy['order_id'] != 'o1'
    new_audit, = store.lp_actions(new_buy['session_id'])
    assert new_audit['state'] == 'accepted' and new_audit['post_started'] is True
    assert new_audit['action_id'] not in {a['action_id'] for a in audits}
    assert state['slots']['occupied'] == 1 and Decimal(state['funds']['buy_reserved_usd']) == 8
    assert state['funds']['as_of'] and pool.datetime.fromisoformat(state['funds']['as_of']) >= deadline
    assert all(p['side'] == 'BUY' and Decimal(p['price']) == Decimal('.40') and Decimal(p['quantity']) == 20
        for p in exchange.posts)
    assert state['plan_wait'] is None
    before = (len(financial_reads), len(packets))
    restarted.lp_auto_run_once()
    again = pool.PredictionExecutionService(store=store, monitor=SimpleNamespace(), trading=exchange,
        notifier=SimpleNamespace(), lock_path=tmp_path/'execution.lock', lp=lp)
    again._breaker_open = False
    again.lp_auto_run_once()
    assert len(financial_reads) > before[0]
    assert exchange.cancels == ['o1'] and [p['token_id'] for p in exchange.posts] == ['m00', 'm02']
    assert store.lp_actions(original_sid) == confirmed_audits and store.lp_actions(new_buy['session_id']) == [new_audit]


def test_unaffordable_best_candidate_keeps_affordable_resting_buy(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1, budget='8')
    advance_auto_wait(engine, monkeypatch)
    direction = exchange.direction
    def larger(token):
        result = direction(token)
        if token == 'm01':
            result['market']['reward_min_size'] = Decimal('40')
            result['market']['minimum_order_size'] = Decimal('40')
        return result
    exchange.direction = larger
    exchange.rewards['m01'] = Decimal('100')
    refresh(lp, exchange, 2)
    resting_buy = exchange.orders[0].copy()
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert state['last_round']['reason'] == 'target_filled'
    assert [row['condition_id'] for row in state['last_round']['targets']] == ['m00']
    assert state['last_round']['blocked'] == [
        {'condition_id': 'm01', 'token_id': 'm01', 'reason': 'rotation_budget_insufficient'}]
    assert exchange.cancels == []
    assert len(exchange.posts) == 1
    assert exchange.orders == [resting_buy]
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    assert Decimal(state['funds']['available_usd']) == 0


@pytest.mark.parametrize('change', ['observation', 'fill', 'stop', 'protection'])
def test_concurrent_session_changes_are_checked_by_trading_facts(tmp_path, monkeypatch, change):
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from copy import deepcopy
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    session_id = engine.lp_auto_state()['intents'][0]['session_id']
    read = exchange.lp_reward_catalog
    def update_during_read(**kwargs):
        if change == 'observation':
            patch = {'reward_status': 'known'}
        elif change == 'fill':
            exchange.orders[0]['size_matched'] = '8'
            exchange.positions = [dict(token_id='m00',condition_id='m00',size='8',average_price='.40')]
            exchange.trades = [dict(trade_id='actual-concurrent-o1',status='CONFIRMED',trader_side='MAKER',
                taker_order_id='external-taker',token_id='m00',condition_id='m00',side='BUY',size='8',price='.40',
                fee='0',timestamp=pool.NOW,maker_orders=[dict(order_id='o1',token_id='m00',side='BUY',
                    price='.40',matched_amount='8',fee='0',maker_address='test-wallet')])]
            return read(**kwargs)
        elif change == 'stop':
            patch = {'stop_requested': True}
        else:
            protection = store.lp_session(session_id)['queue_protection']
            for bucket in protection.get('levels', {'entry': protection}).values():
                bucket['state'] = 'triggered'
            patch = {'queue_protection': protection}
        store.lp_update_session(session_id, patch=patch)
        return read(**kwargs)
    exchange.lp_reward_catalog = update_during_read
    state = engine.lp_auto_run_once()
    if change == 'fill':
        assert _wait_round_cancels(store, state)==[] and exchange.cancels==[]
        assert state['last_round']['reason']=='rotation_order_changed'
        assert state['slots']['occupied']==1 and Decimal(state['funds']['buy_reserved_usd'])==8
        assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state']=='registered'
        lp.reconcile_facts(session_id)
        advance_auto_wait(engine, monkeypatch)
        state = engine.lp_auto_run_once()
    request, = _wait_round_cancels(store, state)
    assert request['order_id']=='o1' and request['state']=='accepted'
    assert exchange.cancels == ['o1'] and len(exchange.posts) == 1
    assert state['active_plan'] is None and state['last_round']['completed_at']
    assert all(a['kind']=='buy' for a in state['last_round']['actions'])
    current = store.lp_session(session_id)
    if change == 'stop':
        assert current['stop_requested'] is True
    elif change == 'protection':
        assert all(b['state']=='triggered' for b in current['queue_protection'].get('levels',{'entry':current['queue_protection']}).values())
    elif change == 'fill':
        assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state']=='registered'
        lp.reconcile_facts(session_id)
        current = store.lp_session(session_id)
        assert Decimal(current['buy_filled_quantity'])==Decimal(current['residual_quantity'])==8
        assert Decimal(current['buy_cost'])==Decimal('3.20') and Decimal(current['buy_fees'])==0
        assert exchange.positions[0]['size']=='8'
    assert all(p['side']=='BUY' for p in exchange.posts)


def test_rotation_waits_when_replacement_cannot_meet_existing_gtd_minimum(tmp_path, monkeypatch):
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    session = store.lp_session(engine.lp_auto_state()['intents'][0]['session_id'])
    deadline = pool.datetime.fromisoformat(session['review_at'])
    monkeypatch.setattr(pool, 'NOW', deadline - timedelta(seconds=60))
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    engine.lp_auto_run_once()
    assert exchange.cancels == []
    assert len(exchange.posts) == 1


@pytest.mark.parametrize('change', ['size', 'price'])
def test_resting_buy_that_no_longer_scores_has_zero_estimated_yield(tmp_path, monkeypatch, change):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    direction = exchange.direction
    def changed(token):
        result = direction(token)
        if token == 'm00':
            if change == 'size':
                result['market']['reward_min_size'] = Decimal('40')
            else:
                result['book']['bids'] = [dict(price='.52', size='1000'),
                    dict(price='.51', size='1000'), dict(price='.40', size='20')]
                result['book']['asks'] = [dict(price='.53', size='1000')]
        return result
    exchange.direction = changed
    catalog = exchange.lp_reward_catalog
    def rewards(**kwargs):
        result = catalog(**kwargs)
        if change == 'size':
            for row in result['markets']:
                if row['condition_id'] == 'm00':
                    row['rewards_min_size'] = '40'
        return result
    exchange.lp_reward_catalog = rewards
    refresh(lp, exchange, 2)
    state = engine.lp_auto_run_once()
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    _wait_round_cancels(lp.store, state)
    assert exchange.cancels == ['o1']


@pytest.mark.parametrize('failure', ['timeout', 'denied'])
def test_failed_cancel_retries_only_after_a_new_known_live_receipt(tmp_path, monkeypatch, failure):
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from copy import deepcopy
    from threading import Event
    from time import monotonic
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    from tests.test_lp_auto_plan_scheduler import restart_engine
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    original_entry = deepcopy(engine.lp_auto_state()['last_round'])
    sid = original_entry['actions'][0]['session_id']
    original_entry_audit = deepcopy(store.lp_actions(sid))
    original_order = exchange.orders[0]
    assert original_order['order_id'] == 'o1' and original_order['token_id'] == 'm00'
    assert original_order['side'] == 'BUY' and original_order['status'] == 'LIVE'
    assert Decimal(original_order['price']) == Decimal('.40')
    assert Decimal(original_order['original_size']) == 20 and Decimal(original_order['size_matched']) == 0
    # The simulated venue explicitly reports the full original quantity and known zero fill.
    original_order['fill_quantity_known'] = True
    result_reader = install_order_result_reader(exchange, monkeypatch)
    packets, financial_reads, planning_packets = [], [], []
    def result(request):
        packet = result_reader(request)
        packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', result)
    account = exchange.lp_account_snapshot
    def financial():
        financial_reads.append(dict(at=pool.NOW, active=engine.lp_auto_state()['active_plan'] is not None))
        return account()
    monkeypatch.setattr(exchange, 'lp_account_snapshot', financial)
    def complete_account(*, max_age_seconds=0, trade_generation_provider=None):
        del max_age_seconds
        started = pool.NOW
        packet = pool._fresh_registration_bundle(exchange, engine._lp)
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
        packet.update(read_started_at=started, read_ended_at=pool.NOW, checked_at=pool.NOW,
            open_orders=deepcopy([o for o in exchange.orders if o['status'] == 'LIVE']))
        if trade_generation_provider is not None:
            acquired_generation = trade_generation_provider()
            packet['trade_generation'] = acquired_generation
            assert packet['trade_generation'] == acquired_generation == engine._store.lp_trade_generation()
        planning_packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', complete_account, raising=False)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    native_commits, receipt_commits = [], []
    if failure == 'timeout':
        import json
        import sqlite3
        from pathlib import Path
        connect = sqlite3.connect
        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                result = super().execute(sql, parameters)
                command = sql.strip().upper()
                if command in ('BEGIN', 'BEGIN IMMEDIATE'):
                    self.pending_cancel = None
                    self.pending_receipt = None
                elif command.startswith('INSERT INTO LP_ACTIONS') and len(parameters) >= 5:
                    if parameters[1] == sid and parameters[3] == 'accepted':
                        row = super().execute('SELECT * FROM lp_actions WHERE action_key=?', (parameters[2],)).fetchone()
                        payload = json.loads(row['payload'])
                        if payload.get('role')=='reconciliation_cancel' and payload.get('order_id')=='o1':
                            self.pending_receipt = dict(action_id=row['action_id'],action_key=row['action_key'],payload=payload)
                    if parameters[1] != sid or parameters[3] != 'pending':
                        return result
                    row = super().execute('SELECT * FROM lp_actions WHERE action_key=?', (parameters[2],)).fetchone()
                    payload = json.loads(row['payload'])
                    owner = payload.get('rotation_round_id')
                    if (payload.get('role') != 'reconciliation_cancel' or payload.get('order_id') != 'o1'
                            or row['state'] != 'pending' or row['action_id'] != parameters[0]
                            or not row['action_key'].startswith(sid + ':reconciliation-cancel:')
                            or any(c['action_id'] == row['action_id'] for c in native_commits)):
                        return result
                    document = json.loads(super().execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
                    registration = document['active_plan'].get('cancel_registration', {})
                    if (not owner or (document.get('active_plan') or {}).get('round_id') != owner
                            or owner not in document['rounds'] or registration.get('round_id') != owner
                            or registration.get('sessions', {}).get('o1') != sid):
                        return result
                    self.pending_cancel = dict(action_id=row['action_id'], action_key=row['action_key'],
                        session_id=sid, order_id='o1', round_id=owner, pending_row=dict(row),
                        registered_at=document['rounds'][owner]['started_at'],
                        rotation_requested_at=payload['submit_requested_at'],
                        submit_requested_at=payload['submit_requested_at'])
                elif command.startswith('UPDATE LP_ACTIONS SET STATE=') and len(parameters)>=6 and parameters[0]=='accepted' and parameters[3]==sid:
                    row=super().execute('SELECT * FROM lp_actions WHERE action_key=?',(parameters[4],)).fetchone()
                    payload=json.loads(row['payload'])
                    if payload.get('role')=='reconciliation_cancel' and payload.get('order_id')=='o1':
                        self.pending_receipt=dict(action_id=row['action_id'],action_key=row['action_key'],payload=payload)
                elif command == 'COMMIT' and getattr(self, 'pending_cancel', None) is not None:
                    pending = self.pending_cancel
                    self.pending_cancel = None
                    assert not self.in_transaction, 'Successful real pending cancel COMMIT must precede clock advance'
                    actual = super().execute('SELECT * FROM lp_actions WHERE action_key=?', (pending['action_key'],)).fetchone()
                    assert dict(actual) == pending['pending_row'], 'Observer leaves the native pending audit row untouched'
                    before = pool.NOW
                    monkeypatch.setattr(pool, 'NOW', before + timedelta(microseconds=1))
                    native_commits.append({**pending, 'commit': command, 'native_commit_returned': True,
                        'in_transaction': self.in_transaction, 'before': before, 'after': pool.NOW})
                elif command == 'COMMIT' and getattr(self, 'pending_receipt', None) is not None:
                    assert not self.in_transaction
                    receipt_commits.append({**self.pending_receipt, 'observed_business_at': pool.NOW, 'native_commit_returned':True})
                    self.pending_receipt = None
                return result
        def observed_connect(database, *args, **kwargs):
            if Path(database).resolve() == store.path.resolve():
                kwargs.setdefault('factory', ObservedConnection)
            return connect(database, *args, **kwargs)
        monkeypatch.setattr(sqlite3, 'connect', observed_connect)
    def failed(order_id):
        exchange.cancels.append(order_id)
        if failure == 'timeout':
            raise TimeoutError()
        return {'not_canceled': {order_id: 'temporary failure'}}
    exchange.cancel_order = failed
    state = engine.lp_auto_run_once()
    original = deepcopy(state['last_round'])
    expected_state = 'unknown' if failure == 'timeout' else 'rejected'
    receipt_deadline = monotonic() + 5
    while True:
        cancel, = _wait_round_cancels(store, state, expected_states={'o1': expected_state})
        if cancel['state'] == expected_state:
            break
        assert monotonic() < receipt_deadline, 'Exact final typed cancel receipt watchdog'
        Event().wait(.01)
    assert all(a['kind']=='buy' and a['state']=='not_sent' and a['reason']=='next_round_capacity' for a in original['actions'])
    assert cancel['order_id'] == 'o1' and cancel['session_id'] == sid
    assert cancel['state'] == ('unknown' if failure == 'timeout' else 'rejected')
    assert state['active_plan'] is None and original['completed_at']
    original_audits = deepcopy(store.lp_actions(sid))
    old_cancel, = [a for a in original_audits if a['role'] == 'reconciliation_cancel']
    assert old_cancel['state'] == ('unknown' if failure == 'timeout' else 'rejected')
    assert old_cancel['order_id'] == 'o1' and old_cancel['rotation_round_id'] == original['round_id']
    assert old_cancel['post_started'] is True and old_cancel['submit_post_started_at']
    assert old_cancel['action_id'] and old_cancel['action_key'].startswith(sid + ':reconciliation-cancel:')
    if failure == 'timeout':
        assert len(native_commits) == 1, 'Exact original pending typed cancel committed once'
        committed = native_commits[0]
        assert committed['action_id'] == old_cancel['action_id'] and committed['action_key'] == old_cancel['action_key']
        assert committed['round_id'] == old_cancel['rotation_round_id'] == original['round_id']
        assert committed['native_commit_returned'] is True and committed['in_transaction'] is False
        assert committed['after'] == committed['before'] + timedelta(microseconds=1)
        assert pool.datetime.fromisoformat(committed['registered_at']) <= pool.datetime.fromisoformat(
            committed['rotation_requested_at']) <= pool.datetime.fromisoformat(old_cancel['submit_requested_at'])
        assert committed['submit_requested_at'] == old_cancel['submit_requested_at']
        assert pool.datetime.fromisoformat(old_cancel['submit_post_started_at']) == committed['after']
        assert pool.datetime.fromisoformat(old_cancel['submit_requested_at']) == committed['before']
    assert original_audits[:len(original_entry_audit)] == original_entry_audit
    assert exchange.cancels == ['o1'] and len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1 and Decimal(state['funds']['buy_reserved_usd']) == 8
    completed = pool.datetime.fromisoformat(original['completed_at'])
    deadline = completed
    assert state['plan_wait'] is None
    before = (len(financial_reads), len(packets))
    engine.lp_auto_set_desired_running(False)
    for elapsed in (1, 29):
        engine.lp_auto_run_once()
        engine = restart_engine(engine, exchange)
        engine.lp_auto_run_once()
        assert len(financial_reads) > before[0]
        assert exchange.cancels == ['o1'] and len(exchange.posts) == 1
        assert engine._store.lp_actions(sid) == original_audits
        held = engine.lp_auto_state()
        assert held['slots']['occupied'] == 1 and Decimal(held['funds']['buy_reserved_usd']) == 8
    engine.lp_auto_set_desired_running(True)
    exchange.cancel_order = lambda oid: (exchange.cancels.append(oid) or {'canceled': [oid]})
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
    refresh(engine._lp, exchange, 2)
    state = engine.lp_auto_run_once()
    assert len(financial_reads) > before[0] and not any(r['active'] for r in financial_reads)
    assert state['funds']['as_of']
    current_account = planning_packets[-1]
    assert all(current_account[k] is True for k in ('authenticated', 'balance_complete',
        'open_orders_complete', 'positions_complete', 'trades_complete', 'pagination_complete'))
    assert current_account['wallet_address'] == current_account['account_id'] == 'test-wallet'
    assert current_account['read_started_at'] >= deadline
    assert current_account['read_started_at'] <= current_account['read_ended_at'] == current_account['checked_at']
    assert Decimal(current_account['balance']) == Decimal(current_account['allowance']) == 1000
    current_order, = current_account['open_orders']
    assert current_order['order_id'] == 'o1' and current_order['token_id'] == 'm00'
    assert current_order['status'] == 'LIVE' and Decimal(current_order['price']) == Decimal('.40')
    assert Decimal(current_order['original_size']) == 20 and Decimal(current_order['size_matched']) == 0
    assert current_account['positions'] == current_account['raw_trades'] == []
    # A public current result carries exact identity/terms and strictly newer known LIVE0.
    assert packets, state['last_round']
    packet = packets[-1]
    assert all(packet[k] is True for k in ('authenticated', 'open_orders_complete', 'pagination_complete'))
    exact, = [r for r in packet['orders'] if r['order_id'] == 'o1']
    assert exact['status'] == 'LIVE' and exact['fill_quantity_known'] is True
    assert exact['side'] == 'BUY' and exact['token_id'] == exact['condition_id'] == 'm00'
    assert Decimal(exact['price']) == Decimal('.40') and Decimal(exact['original_size']) == 20
    assert Decimal(exact['size_matched']) == 0
    assert packet['read_started_at'] > pool.datetime.fromisoformat(old_cancel['submit_post_started_at'])
    _wait_round_cancels(store,state)
    assert exchange.cancels == ['o1', 'o1'], state['last_round']
    current = deepcopy(state['last_round'])
    assert current['round_id'] != original['round_id'] and current['completed_at'] and state['active_plan'] is None
    new_cancel, = _wait_round_cancels(store,state)
    assert all(a['kind']=='buy' and a['state']=='not_sent' and a['reason']=='next_round_capacity' for a in current['actions'])
    assert new_cancel['action_id'] != cancel['action_id'] and new_cancel['session_id'] == sid
    assert new_cancel['order_id'] == 'o1' and new_cancel['state'] == 'accepted'
    audits = deepcopy(engine._store.lp_actions(sid))
    assert audits[:len(original_audits)] == original_audits
    newer, = [a for a in audits if a['role'] == 'reconciliation_cancel' and a['action_key'] != old_cancel['action_key']]
    assert newer['action_id'] != old_cancel['action_id'] and newer['rotation_round_id'] == current['round_id']
    assert newer['state'] == 'accepted' and newer['post_started'] is True
    assert len(exchange.posts) == 1 and exchange.orders == [original_order]
    assert state['slots']['occupied'] == 1 and Decimal(state['funds']['buy_reserved_usd']) == 8
    second_deadline = pool.datetime.fromisoformat(current['completed_at'])
    assert state['plan_wait'] is None
    if failure == 'timeout':
        before = (len(financial_reads), len(packets))
        engine.lp_auto_set_desired_running(False)
        engine.lp_auto_run_once()
        assert len(financial_reads) > before[0]
        assert exchange.cancels == ['o1', 'o1'] and engine._store.lp_actions(sid) == audits
        # Close the second ACK responsibility in a genuine current round
        # whose best yield is the original resting BUY, requiring no new send.
        engine.lp_auto_set_desired_running(True)
        exchange.rewards['m01'] = Decimal('23')
        refresh(engine._lp, exchange, 2)
        idle = engine.lp_auto_run_once()
        assert idle['active_plan'] is None and idle['last_round']['completed_at']
        assert idle['last_round']['round_id'] not in (original['round_id'], current['round_id'])
        assert idle['last_round']['actions']==[]
        assert [t['token_id'] for t in idle['last_round']['targets']]==['m00']
        assert idle['slots']['occupied']==1 and Decimal(idle['funds']['buy_reserved_usd'])==8
        assert idle['funds']['status']=='known'
        assert exchange.cancels==['o1','o1'] and len(exchange.posts)==1 and exchange.orders==[original_order]
        assert engine._store.lp_actions(sid)==audits and len(native_commits)==2
        closed_session = deepcopy(engine._store.lp_session(sid))
        assert not closed_session.get('yield_rotation_cancel_owners')
        assert not closed_session.get('yield_rotation_protection_before')
        assert closed_session['entry_cancel_requested'] is False
        assert all(b['state']=='registered' for b in closed_session['queue_protection']['levels'].values())
        closed_order = closed_session['order_history']['o1']
        assert closed_order['status']=='LIVE' and closed_order['side']=='BUY' and closed_order['token_id']=='m00'
        assert Decimal(closed_order['price'])==Decimal('.40')
        assert Decimal(closed_order.get('original_size',closed_order.get('quantity')))==20
        assert Decimal(closed_order['size_matched'])==0
        # Observe the actual committed closure; never manufacture its age.
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
            closed_document = json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
        assert not closed_document.get('account_rotations')
        assert current['round_id'] in closed_document['rounds']
        receipt_commit, = [r for r in receipt_commits if r['action_key']==newer['action_key']]
        assert receipt_commit['action_id']==newer['action_id'] and receipt_commit['native_commit_returned'] is True
        settled = receipt_commit['observed_business_at']
        assert pool.datetime.fromisoformat(newer['submit_post_started_at'])<=settled<=pool.NOW
        idle_deadline = pool.datetime.fromisoformat(idle['last_round']['completed_at'])
        assert idle['plan_wait'] is None
        before = (len(financial_reads),len(packets),len(planning_packets))
        engine.lp_auto_run_once()
        assert len(financial_reads) > before[0] and len(planning_packets) > before[2]
        assert exchange.cancels==['o1','o1'] and engine._store.lp_actions(sid)==audits
        late_at = settled+timedelta(seconds=90)
        assert late_at>=idle_deadline and (late_at-settled).total_seconds()>60
        monkeypatch.setattr(pool,'NOW',late_at)
        exchange.rewards['m01'] = Decimal('25')
        refresh(engine._lp, exchange, 2)
        third = engine.lp_auto_run_once()
        late_account = planning_packets[-1]
        assert all(late_account[k] is True for k in ('authenticated','balance_complete',
            'open_orders_complete','positions_complete','trades_complete','pagination_complete'))
        assert late_account['wallet_address']==late_account['account_id']=='test-wallet'
        assert late_account['read_started_at']>=late_at
        assert late_account['read_started_at']<=late_account['checked_at']==late_account['read_ended_at']
        assert Decimal(late_account['balance'])==Decimal(late_account['allowance'])==1000
        late_order, = late_account['open_orders']
        assert late_order['order_id']=='o1' and late_order['side']=='BUY'
        assert late_order['token_id']==late_order['condition_id']=='m00' and late_order['status']=='LIVE'
        assert late_order['fill_quantity_known'] is True and Decimal(late_order['price'])==Decimal('.40')
        assert Decimal(late_order['original_size'])==20 and Decimal(late_order['size_matched'])==0
        assert late_account['positions']==late_account['raw_trades']==[]
        assert third['slots']['occupied']==1
        assert Decimal(third['funds']['buy_reserved_usd'])==8
        assert packets[-1]['read_started_at'] > pool.datetime.fromisoformat(newer['submit_post_started_at'])
        _wait_round_cancels(store,third)
        assert exchange.cancels == ['o1', 'o1', 'o1'], third['last_round']
        assert third['last_round']['round_id'] not in (original['round_id'], current['round_id'])
        assert third['active_plan'] is None and third['last_round']['completed_at']
        third_action, = _wait_round_cancels(store,third)
        assert all(a['kind']=='buy' and a['state']=='not_sent' and a['reason']=='next_round_capacity' for a in third['last_round']['actions'])
        assert third_action['action_id'] not in (cancel['action_id'], new_cancel['action_id'])
        all_audits = engine._store.lp_actions(sid)
        assert all_audits[:len(audits)] == audits
        cancels = [a for a in all_audits if a['role'] == 'reconciliation_cancel']
        assert len(cancels) == len({a['action_id'] for a in cancels}) == len({a['action_key'] for a in cancels}) == 3
        assert len(native_commits) == 3 and {c['action_id'] for c in native_commits} == {a['action_id'] for a in cancels}
        for committed in native_commits:
            actual, = [a for a in cancels if a['action_id'] == committed['action_id']]
            assert actual['action_key'] == committed['action_key'] and actual['rotation_round_id'] == committed['round_id']
            assert committed['native_commit_returned'] is True and not committed['in_transaction']
            assert pool.datetime.fromisoformat(committed['rotation_requested_at']) <= pool.datetime.fromisoformat(
                actual['submit_requested_at']) < pool.datetime.fromisoformat(actual['submit_post_started_at'])
            assert pool.datetime.fromisoformat(actual['submit_post_started_at']) == committed['after']
            assert committed['after'] == committed['before'] + timedelta(microseconds=1)
        assert len(exchange.posts) == 1 and exchange.orders == [original_order]
        assert third['slots']['occupied'] == 1 and Decimal(third['funds']['buy_reserved_usd']) == 8

    assert not any(r['active'] for r in financial_reads)
    assert all(p['side'] == 'BUY' and Decimal(p['price']) == Decimal('.40') and Decimal(p['quantity']) == 20
        for p in exchange.posts)


@pytest.mark.parametrize('receipt', ['unknown', 'incomplete', 'stale', 'terminal', 'partial'])
def test_cancel_recovery_uses_known_owned_receipts_and_preserves_fills(tmp_path, monkeypatch, receipt):
    from copy import deepcopy
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    entry = deepcopy(engine.lp_auto_state()['last_round'])
    sid = entry['actions'][0]['session_id']
    order = exchange.orders[0]
    assert order['order_id'] == 'o1' and order['token_id'] == 'm00' and order['status'] == 'LIVE'
    assert order['side'] == 'BUY' and Decimal(order['price']) == Decimal('.40')
    assert Decimal(order['original_size']) == 20 and Decimal(order['size_matched']) == 0
    order['fill_quantity_known'] = True
    result_reader = install_order_result_reader(exchange, monkeypatch)
    unavailable_result = [False]
    packets, financial_reads, accounts, source_accounts = [], [], [], []
    account = exchange.lp_account_snapshot
    def financial():
        financial_reads.append(dict(at=pool.NOW, active=engine.lp_auto_state()['active_plan'] is not None))
        packet = account()
        if unavailable_result[0] and receipt == 'incomplete':
            packet['open_orders_complete'] = False
        elif unavailable_result[0] and receipt == 'stale':
            packet['checked_at'] = pool.NOW - timedelta(seconds=61)
        return packet
    monkeypatch.setattr(exchange, 'lp_account_snapshot', financial)
    def complete_account(*, max_age_seconds=0, trade_generation_provider=None):
        del max_age_seconds
        started = pool.NOW
        packet = pool._fresh_registration_bundle(exchange, lp)
        source_accounts.append(deepcopy(packet))
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
        packet.update(read_started_at=started, read_ended_at=pool.NOW, checked_at=pool.NOW,
            open_orders=deepcopy([o for o in exchange.orders
                if o['status'] not in ('CANCELED','CANCELLED','EXPIRED','FILLED','MATCHED','REJECTED')]))
        if unavailable_result[0] and receipt == 'stale':
            packet['checked_at'] = started - timedelta(seconds=61)
        if trade_generation_provider is not None:
            generation = trade_generation_provider()
            packet['trade_generation'] = generation
            assert packet['trade_generation'] == generation == store.lp_trade_generation()
        accounts.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', complete_account, raising=False)
    def confirmation(request):
        packet = result_reader(request)
        if unavailable_result[0] and receipt == 'incomplete':
            packet['open_orders_complete'] = False
        elif unavailable_result[0] and receipt == 'stale':
            packet.update(read_started_at=pool.NOW - timedelta(seconds=61),
                read_ended_at=pool.NOW - timedelta(seconds=61), checked_at=pool.NOW - timedelta(seconds=61))
        packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', confirmation)
    owned_snapshots = []
    snapshot = exchange.lp_snapshot
    def owned_snapshot(request):
        packet = snapshot(request)
        packet['account'].update(trades_complete=True, pagination_complete=True)
        owned_snapshots.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_snapshot', owned_snapshot)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    exchange.cancel_order = lambda oid: (exchange.cancels.append(oid) or {})
    first = engine.lp_auto_run_once()
    original = deepcopy(first['last_round'])
    original_cancel, = _wait_round_cancels(store, first, expected_states={'o1': 'unknown'})
    assert all(a['kind'] == 'buy' for a in original['actions'])
    assert original_cancel['order_id'] == 'o1' and original_cancel['session_id'] == sid
    assert first['active_plan'] is None and original['completed_at']
    original_audits = deepcopy(store.lp_actions(sid))
    unknown, = [a for a in original_audits if a['role'] == 'reconciliation_cancel']
    assert unknown['state'] == 'unknown' and unknown['post_started'] is True
    assert unknown['order_id'] == 'o1' and unknown['rotation_round_id'] == original['round_id']
    assert exchange.cancels == ['o1'] and len(exchange.posts) == 1
    assert first['slots']['occupied'] == 1 and Decimal(first['funds']['buy_reserved_usd']) == 8
    deadline = pool.datetime.fromisoformat(original['completed_at'])
    assert first['plan_wait'] is None
    before = (len(financial_reads), len(packets))
    engine.lp_auto_set_desired_running(False)
    paused = engine.lp_auto_run_once()
    assert paused['active_plan'] is None and paused['last_round']['actions'] == []
    assert paused['last_round']['round_id'] != original['round_id']
    assert len(financial_reads) > before[0] and exchange.cancels == ['o1'] and len(exchange.posts) == 1
    assert store.lp_actions(sid) == original_audits
    engine.lp_auto_set_desired_running(True)
    unavailable_result[0] = True
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
    if receipt == 'unknown':
        order['status'] = 'UNKNOWN'
    elif receipt == 'terminal':
        order['status'] = 'CANCELED'
    elif receipt == 'partial':
        order['size_matched'] = '8'
        exchange.positions = [dict(token_id='m00', condition_id='m00', size='8', average_price='.40')]
        exchange.trades = [dict(trade_id='actual-o1-fill', order_id='o1', condition_id='m00', token_id='m00', side='BUY',
            size='8', price='.40', fee='0', status='CONFIRMED', timestamp=pool.NOW, trader_side='MAKER',
            taker_order_id='external-taker', maker_orders=[dict(order_id='o1', token_id='m00', side='BUY',
                price='.40', matched_amount='8', fee='0', maker_address='test-wallet', owner='credential-owner-uuid')])]
    state = engine.lp_auto_run_once()
    assert not any(r['active'] for r in financial_reads)
    assert state['last_round']['round_id'] != original['round_id'] and state['last_round']['completed_at']
    assert state['active_plan'] is None and state['plan_wait'] is None
    fresh_requests = _wait_round_cancels(store, state, expected_states={'o1': 'unknown'} if receipt == 'partial' else {})
    expected_cancels = ['o1','o1'] if receipt == 'partial' else ['o1']
    assert exchange.cancels == expected_cancels
    if receipt == 'partial':
        lp.reconcile_facts(sid)
    if receipt == 'partial':
        assert len(fresh_requests)==1 and fresh_requests[0]['order_id']=='o1' and fresh_requests[0]['state']=='unknown'
        assert fresh_requests[0]['action_key']!=unknown['action_key']
        assert next(a for a in store.lp_actions(sid) if a['action_key']==unknown['action_key'])==unknown
    if receipt == 'terminal':
        exact, = [row for row in source_accounts[-1]['open_orders'] if row['order_id']=='o1']
        assert all(source_accounts[-1][key] is True for key in ('authenticated','open_orders_complete',
            'positions_complete','trades_complete','pagination_complete'))
        assert exact['status']=='CANCELED' and exact['fill_quantity_known'] is True
        assert exact['side']=='BUY' and exact['token_id']==exact['condition_id']=='m00'
        assert Decimal(exact['price'])==Decimal('.40') and Decimal(exact['original_size'])==20
        assert Decimal(exact['size_matched'])==0
        assert source_accounts[-1]['checked_at']>pool.datetime.fromisoformat(unknown['submit_post_started_at'])
        assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01']
        replacement, = [a for a in state['last_round']['actions'] if a['kind'] == 'buy']
        assert replacement['state'] == 'success' and replacement['session_id'] != sid
        assert replacement['action_id'] not in {a['action_id'] for a in original['actions'] + entry['actions']}
        assert replacement['order_id'] != 'o1'
        assert state['slots']['occupied'] == 1 and Decimal(state['funds']['buy_reserved_usd']) == 8
    else:
        assert len(exchange.posts) == 1 and state['slots']['occupied'] == 1
        assert Decimal(state['funds']['buy_reserved_usd']) == (Decimal('4.80') if receipt == 'partial' else Decimal('8'))
        if receipt == 'partial':
            assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.20')
            assert Decimal(state['funds']['buy_reserved_usd']) + Decimal(state['funds']['inventory_cost_usd']) == 8
        else:
            assert state['funds']['status'] == 'unknown' and state['funds']['available_usd'] is None
        if receipt != 'partial':
            assert store.lp_actions(sid) == original_audits
        else:
            assert all(next(a for a in store.lp_actions(sid) if a['action_key']==old['action_key'])==old for old in original_audits)
    partial_audits = deepcopy(store.lp_actions(sid)) if receipt=='partial' else original_audits
    def old_audits_after_terminal():
        current = deepcopy(store.lp_actions(sid))
        assert current == partial_audits, 'Original UNKNOWN requests remain immutable after terminal publication'
        return current
    if receipt == 'terminal':
        resolved = old_audits_after_terminal()
        before = (len(financial_reads), len(packets))
        engine.lp_auto_run_once()
        assert len(financial_reads) > before[0]
        assert exchange.cancels == ['o1'] and len(exchange.posts) == 2 and store.lp_actions(sid) == resolved
    elif receipt in ('unknown', 'incomplete', 'stale'):
        if receipt == 'unknown':
            assert any(r['order_id'] == 'o1' and r['status'] == 'UNKNOWN' for r in accounts[-1]['open_orders'])
        elif receipt == 'incomplete':
            assert accounts[-1]['open_orders_complete'] is False
            assert fresh_requests==[]
        else:
            assert accounts[-1]['checked_at'] < pool.NOW - timedelta(seconds=60)
            assert fresh_requests==[]
        old_round = state['last_round']['round_id']
        order['status'] = 'LIVE'
        unavailable_result[0] = False
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
        refresh(lp, exchange, 2)
        state = engine.lp_auto_run_once()
        assert packets[-1]['orders'][0]['status'] == 'LIVE' and packets[-1]['orders'][0]['fill_quantity_known'] is True
        assert packets[-1]['read_started_at'] > pool.datetime.fromisoformat(unknown['submit_post_started_at'])
        _wait_round_cancels(store, state, expected_states={'o1': 'unknown'})
        assert exchange.cancels == ['o1', 'o1'] and len(exchange.posts) == 1
        assert state['last_round']['round_id'] not in (original['round_id'], old_round)
        fresh_cancel, = _wait_round_cancels(store, state, expected_states={'o1': 'unknown'})
        assert all(a['kind'] == 'buy' for a in state['last_round']['actions'])
        assert fresh_cancel['action_id'] != original_cancel['action_id'] and fresh_cancel['session_id'] == sid
        audits = store.lp_actions(sid)
        assert audits[:len(original_audits)] == original_audits
        new_audit, = [a for a in audits if a['role']=='reconciliation_cancel' and a['action_key']!=unknown['action_key']]
        assert new_audit['state'] == 'unknown' and new_audit['action_id'] != unknown['action_id']
        assert new_audit['rotation_round_id'] == state['last_round']['round_id']
        assert state['slots']['occupied'] == 1 and Decimal(state['funds']['buy_reserved_usd']) == 8
    else:
        session = store.lp_session(sid)
        assert Decimal(session['buy_filled_quantity']) == Decimal(session['residual_quantity']) == 8
        assert Decimal(session['buy_cost']) == Decimal('3.20') and Decimal(session['buy_fees']) == 0
        partial_round = state['last_round']['round_id']
        old_ledger = {k:deepcopy(session.get(k)) for k in ('buy_filled_quantity','residual_quantity','buy_cost','buy_fees')}
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
        order['status'] = 'CANCELED'
        refresh(lp, exchange, 2)
        state = engine.lp_auto_run_once()
        assert state['last_round']['round_id'] not in (original['round_id'], partial_round)
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        assert state['slots']['occupied'] == 1 and Decimal(state['funds']['buy_reserved_usd']) == 8
        assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.20')
        assert [p['token_id'] for p in exchange.posts] == ['m00','m01'] and exchange.cancels == ['o1','o1']
        replacement, = [a for a in state['last_round']['actions'] if a['kind']=='buy']
        assert replacement['state']=='success' and replacement['session_id']!=sid and replacement['order_id']!='o1'
        assert replacement['action_id'] not in {a['action_id'] for a in original['actions']}
        resolved = old_audits_after_terminal()
        assert {k:store.lp_session(sid).get(k) for k in old_ledger} == old_ledger
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        current = engine.lp_auto_state()
        assert current['funds']['status'] == 'known'
        assert Decimal(current['funds']['buy_reserved_usd']) == 8
        assert Decimal(current['funds']['inventory_cost_usd']) == Decimal('3.20')
        assert Decimal(current['funds']['available_usd']) == Decimal('88.80')
        assert current['slots']['occupied'] == 1 and len(exchange.posts) == 2 and exchange.cancels == ['o1','o1']
        assert store.lp_actions(sid) == resolved
        assert {k:store.lp_session(sid).get(k) for k in old_ledger} == old_ledger

    assert all(p['side']=='BUY' and Decimal(p['price'])==Decimal('.40') and Decimal(p['quantity'])==20 for p in exchange.posts)


@pytest.mark.parametrize('balance,expected_cancels', [('20', ['o2']), ('24', ['o2'])])
def test_rotation_budget_counts_manual_and_retained_buys_once(tmp_path, monkeypatch, balance, expected_cancels):
    import json
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from collections import Counter
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=3, target=2)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    originals = {i['token_id']: i for i in engine.lp_auto_state()['intents']}
    assert {'m00', 'm01'} <= originals.keys()
    mapping = {}
    for token in ('m00', 'm01'):
        intent = originals[token]
        session = store.lp_session(intent['session_id'])
        audit, = [a for a in store.lp_actions(intent['session_id']) if a.get('role') == 'entry']
        assert session['entry_order_id'] == intent['order_id'] == audit['order_id']
        assert session['token_id'] == token and audit['state'] == 'accepted'
        mapping[token] = dict(intent=intent, session=session, audit=audit)
    (tmp_path / 'CI-B-native-binding.json').write_text(json.dumps(mapping, default=str, indent=2))
    manual = dict(order_id='manual-buy', token_id='manual', condition_id='manual',
                  side='BUY', status='LIVE', price='.40', original_size='20', size_matched='0')
    exchange.orders.append(manual.copy())
    account = exchange.lp_account_snapshot
    exchange.lp_account_snapshot = lambda: {**account(), 'balance': balance, 'allowance': balance}
    exchange.rewards['m02'] = Decimal('25')
    refresh(lp, exchange, 3)
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    expected_original_ids = [originals[{'o1': 'm00', 'o2': 'm01'}[oid]]['order_id'] for oid in expected_cancels]
    _wait_round_cancels(store, state)
    assert Counter(exchange.cancels) == Counter(expected_original_ids)
    assert exchange.orders[-1] == manual
    assert len(exchange.posts) == 2
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    expected_targets = ['m02', 'm00']
    assert [row['condition_id'] for row in state['last_round']['targets']] == expected_targets
    assert len(state['last_round']['targets']) == len(expected_targets)
    assert state['active_plan'] is None and state['last_round']['completed_at']
    assert all(a['kind'] == 'buy' for a in state['last_round']['actions'])
    assert state['last_round']['blocked'] == []
    retained, = [row for row in state['last_round']['targets'] if row['token_id']=='m00']
    assert retained['order_id']==originals['m00']['order_id']
    assert retained['session_id']==originals['m00']['session_id']
    assert Decimal(retained['price'])==Decimal('.40') and Decimal(retained['quantity'])==20
    prospective, = [row for row in state['last_round']['targets'] if row['token_id']=='m02']
    assert Decimal(prospective['price'])*Decimal(prospective['quantity'])==8
    assert Decimal(balance)-Decimal(manual['price'])*Decimal(manual['original_size'])>=8


def test_full_candidate_count_and_preview_are_not_truncated_to_target_count(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=18)
    advance_auto_wait(engine, monkeypatch)
    refresh(lp, exchange, 18)
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert state['last_round']['candidate_count'] == 13
    assert len(state['last_round']['candidates']) == 10
    assert {r['condition_id'] for r in state['last_round']['targets']} == {
        'm00', 'm01', 'm02', 'm03', 'm04'}


@pytest.mark.parametrize('raw_facts', ['complete', 'missing_quantity', 'invalid_position', 'read_failure', 'stale', 'unknown', 'partial'])
def test_rotation_retry_uses_real_adapter_account_completeness(tmp_path, monkeypatch, raw_facts):
    from copy import deepcopy
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    original_entry = deepcopy(engine.lp_auto_state()['last_round'])
    lp.drain_submissions(timeout=5)
    # A new account read must start after the real original sender ended.
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
    sid = original_entry['actions'][0]['session_id']
    order = exchange.orders[0]
    assert order['order_id']=='o1' and order['token_id']=='m00' and order['side']=='BUY'
    assert Decimal(order['price'])==Decimal('.40') and Decimal(order['original_size'])==20 and Decimal(order['size_matched'])==0
    order['fill_quantity_known'] = True
    install_order_result_reader(exchange, monkeypatch)
    accounts, financial_reads, raw_reads, packets, failed_accounts = [], [], [], [], []
    result_requests = []
    using_adapter = [False]
    account = exchange.lp_account_snapshot
    def financial():
        financial_reads.append(dict(at=pool.NOW, active=engine.lp_auto_state()['active_plan'] is not None))
        return adapter.lp_account_snapshot() if using_adapter[0] else account()
    monkeypatch.setattr(exchange, 'lp_account_snapshot', financial)
    def complete_account(*, max_age_seconds=0, trade_generation_provider=None):
        del max_age_seconds
        started = pool.NOW
        if using_adapter[0]:
            # Preserve the real adapter's completeness and malformed/stale rows.
            try:
                packet = adapter.lp_account_snapshot_shared(max_age_seconds=0,
                    trade_generation_provider=trade_generation_provider)
            except Exception as exc:
                failed_accounts.append(dict(type=type(exc).__name__,message=str(exc),started=started,ended=pool.NOW))
                raise
        else:
            packet = pool._fresh_registration_bundle(exchange, lp)
            packet['open_orders'] = deepcopy([o for o in exchange.orders if o['status']=='LIVE'])
            packet.update(read_started_at=started, read_ended_at=pool.NOW, checked_at=pool.NOW)
        if trade_generation_provider is not None:
            acquired = trade_generation_provider()
            if not using_adapter[0]:
                packet['trade_generation'] = acquired
            assert packet['trade_generation'] == acquired == store.lp_trade_generation()
        accounts.append(deepcopy(packet))
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
        return packet
    monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', complete_account, raising=False)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    exchange.cancel_order = lambda oid: (exchange.cancels.append(oid) or {})
    first = engine.lp_auto_run_once()
    original = deepcopy(first['last_round'])
    assert original['completed_at'] and first['active_plan'] is None
    old, = _wait_round_cancels(store, first, expected_states={'o1': 'unknown'})
    original_audits = deepcopy(store.lp_actions(sid))
    unknown, = [a for a in original_audits if a['role']=='reconciliation_cancel']
    assert unknown['state']=='unknown' and unknown['post_started'] is True and unknown['order_id']=='o1'
    assert unknown['rotation_round_id']==original['round_id']
    assert exchange.cancels==['o1'] and len(exchange.posts)==1
    assert first['slots']['occupied']==1 and Decimal(first['funds']['buy_reserved_usd'])==8
    deadline = pool.datetime.fromisoformat(original['completed_at'])
    assert first['plan_wait'] is None
    if raw_facts == 'partial':
        partial_before = deepcopy(store.lp_session(sid))
        ledger_fields = ('buy_filled_quantity', 'residual_quantity', 'buy_cost', 'buy_fees',
            'sold_quantity', 'sold_revenue', 'fees', 'position_reconciled', 'verified_order_fills')
        control_fields = ('entry_cancel_requested', 'yield_rotation_cancel_owners',
            'yield_rotation_protection_before', 'queue_protection')
    def read_account():
        raw_reads.append(dict(at=pool.NOW, variant=raw_facts))
        if raw_facts == 'read_failure':
            raise OSError('account read failed')
        orders = [dict(o) for o in exchange.orders]
        if raw_facts == 'missing_quantity':
            orders[0].pop('size_matched')
        positions = [dict(token_id='m00')] if raw_facts == 'invalid_position' else []
        if raw_facts == 'unknown':
            orders[0]['status'] = 'UNKNOWN'
        if raw_facts == 'partial':
            orders[0]['size_matched'] = '8'
            positions = [dict(token_id='m00', condition_id='m00', size='8')]
        checked_at = pool.NOW - timedelta(seconds=61) if raw_facts == 'stale' else pool.NOW
        # Keep the actual source value under the controlled adapter datetime type.
        checked_at = ResultClock.fromtimestamp(checked_at.timestamp(), tz=checked_at.tzinfo)
        return Decimal('1000'), Decimal('1000'), orders, positions, checked_at, (), True
    adapter = PolymarketTradingClient(TradingConfig('test-signer', 'test-wallet'), SimpleNamespace())
    def actual_financial_read():
        financial_reads.append(dict(at=pool.NOW, active=engine.lp_auto_state()['active_plan'] is not None))
        return read_account()
    adapter._account_read_facts = actual_financial_read
    adapter.lp_market_metadata = lambda ids: {}
    from open_trader import polymarket_trading
    from datetime import datetime
    class ResultClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return pool.NOW
    monkeypatch.setattr(polymarket_trading, 'datetime', ResultClock)
    adapter._client = SimpleNamespace(
        list_open_orders=lambda: read_account()[2],
        list_positions=lambda: read_account()[3],
        list_account_trades=lambda: (),
    )
    def confirmation(request):
        result_requests.append(deepcopy(request))
        packet = adapter.lp_order_result_snapshot(request)
        if raw_facts == 'stale':
            packet.update(read_started_at=pool.NOW - timedelta(seconds=61),
                read_ended_at=pool.NOW - timedelta(seconds=61), checked_at=pool.NOW - timedelta(seconds=61))
        packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', confirmation)
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
    using_adapter[0] = True
    before = (len(financial_reads),len(raw_reads),len(packets))
    assert exchange.cancels==['o1'] and len(exchange.posts)==1 and store.lp_actions(sid)==original_audits
    state = engine.lp_auto_run_once()
    assert raw_reads and accounts and not any(r['active'] for r in financial_reads)
    assert state['last_round']['round_id']!=original['round_id'] and state['last_round']['completed_at']
    import json
    (tmp_path / 'adapter-current-evidence.json').write_text(json.dumps(dict(variant=raw_facts,
        state=state,accounts=accounts,packets=packets,raw_reads=raw_reads,
        original_audits=original_audits,session=store.lp_session(sid)),default=str,indent=2)+'\n')
    assert state['active_plan'] is None and state['slots']['occupied']==1
    packet = accounts[-1]
    if raw_facts == 'complete':
        fresh, = _wait_round_cancels(store, state, expected_states={'o1': 'unknown'})
        packet = packets[-1]
    if raw_facts == 'complete':
        assert all(packet[k] is True for k in ('authenticated','open_orders_complete','positions_complete','trades_complete','pagination_complete'))
        current = accounts[-1]
        assert all(current[k] is True for k in ('authenticated','balance_complete','open_orders_complete','positions_complete','trades_complete','pagination_complete'))
        assert current['wallet_address']==current['account_id']=='test-wallet'
        assert current['checked_at']>=deadline and current['read_started_at']<=current['checked_at']<=current['read_ended_at']
        exact, = [r for r in packet['open_orders'] if r['order_id']=='o1']
        assert exact['status']=='LIVE' and exact['fill_quantity_known'] is True and Decimal(exact['size_matched'])==0
        assert exact['token_id']==exact['condition_id']=='m00' and exact['side']=='BUY'
        assert Decimal(exact['price'])==Decimal('.40') and Decimal(exact['original_size'])==20
        assert packet['read_started_at']>pool.datetime.fromisoformat(unknown['submit_post_started_at'])
        assert exchange.cancels==['o1','o1']
        assert all(a['kind']=='buy' for a in original['actions'] + state['last_round']['actions'])
        assert fresh['action_id']!=old['action_id'] and fresh['session_id']==sid and fresh['order_id']=='o1'
        audits = store.lp_actions(sid)
        assert audits[:len(original_audits)]==original_audits
        new, = [a for a in audits if a['role']=='reconciliation_cancel' and a['action_key']!=unknown['action_key']]
        assert new['action_id']!=unknown['action_id'] and new['rotation_round_id']==state['last_round']['round_id']
        assert new['post_started'] is True and new['state']=='unknown'
    else:
        assert exchange.cancels==['o1'] and store.lp_actions(sid)==original_audits
        assert state['last_round']['reason']
        assert state['funds']['status']=='unknown' and state['funds']['available_usd'] is None
        if raw_facts=='missing_quantity':
            assert accounts[-1]['open_orders_complete'] is False and packet['open_orders_complete'] is False
            assert any(r.get('fill_quantity_known') is False for r in packet['open_orders'])
        elif raw_facts=='invalid_position':
            assert accounts[-1]['positions_complete'] is False and packet['positions_complete'] is False
        elif raw_facts=='read_failure':
            assert failed_accounts and failed_accounts[-1]['type']=='OSError'
            assert failed_accounts[-1]['message']=='account read failed'
            assert len(accounts)==1, 'Failed current source has no new complete account publication'
            assert raw_reads[-1]['variant']=='read_failure'
            result,=packets
            assert result_requests==[{'owned_order_ids':['o1'],'condition_ids':['m00']}]
            assert result['result_only'] is True and result['wallet_address']=='test-wallet'
            assert result['authenticated'] is False and result['open_orders_complete'] is False
            assert result['pagination_complete'] is False and result['positions_complete'] is False
            assert result['orders']==result['open_orders']==[]
            assert result['order_read_errors']=={'o1':'order_lookup_unavailable'}
            assert result['read_started_at']<=result['read_ended_at']<=pool.NOW
        elif raw_facts=='stale':
            assert accounts[-1]['checked_at']<pool.NOW-timedelta(seconds=60)
            assert packet['checked_at']<pool.NOW-timedelta(seconds=60)
        elif raw_facts=='unknown':
            assert any(r['status']=='UNKNOWN' for r in packet['open_orders'])
        else:
            exact, = [r for r in packet['open_orders'] if r['order_id']=='o1']
            assert exact['status']=='LIVE' and Decimal(exact['size_matched'])==8
            assert packet['raw_trades']==() and packet['positions'][0]['size']==8
    assert len(exchange.posts)==1 and exchange.posts[0]['token_id']=='m00'
    if raw_facts == 'partial':
        assert Decimal(state['funds']['buy_reserved_usd'])==Decimal('4.80')
        assert state['funds']['inventory_cost_usd'] is None
        assert state['funds']['available_usd'] is None and state['funds']['spendable_usd'] is None
        assert state['funds']['status']=='unknown'
        assert state['last_round']['reason']=='account_position_cost_unknown'
        assert 'account_position_cost_unknown' in state['admission_block_reasons']
        assert state['last_round']['actions']==[] and state['slots']['occupied']==1
        current = accounts[-1]
        assert all(current[k] is True for k in ('authenticated','open_orders_complete',
            'positions_complete','trades_complete','pagination_complete'))
        exact, = [o for o in current['open_orders'] if o['order_id']=='o1']
        assert exact['token_id']==exact['condition_id']=='m00' and exact['side']=='BUY'
        assert exact['status']=='LIVE' and exact['fill_quantity_known'] is True
        assert Decimal(exact['price'])==Decimal('.40') and Decimal(exact['original_size'])==20
        assert Decimal(exact['size_matched'])==8
        buy, = state['account_buys']
        assert buy['order_id']=='o1' and buy['session_id']==sid
        assert buy['token_id']==buy['condition_id']=='m00' and buy['side']=='BUY'
        assert Decimal(buy['price'])==Decimal('.40') and Decimal(buy['original_quantity'])==20
        assert Decimal(buy['quantity'])==12 and Decimal(buy['filled_quantity'])==8
        assert Decimal(buy['reserved_usd'])==Decimal('4.80')
        position, = current['positions']
        assert position['token_id']==position['condition_id']=='m00' and Decimal(position['size'])==8
        assert position.get('average_price') is None and position.get('initial_value') is None
        assert current['raw_trades']==() and current['trades']==() and current['account_trades']==()
        session = store.lp_session(sid)
        assert {k:session.get(k) for k in ledger_fields}=={k:partial_before.get(k) for k in ledger_fields}
        assert {k:session.get(k) for k in control_fields}=={k:partial_before.get(k) for k in control_fields}
        assert not session.get('yield_rotation_cancel_owners')
        assert store.lp_actions(sid)==original_audits and exchange.cancels==['o1']
    else:
        assert Decimal(state['funds']['buy_reserved_usd'])==8


def test_cancel_recovery_waits_when_session_is_missing(tmp_path, monkeypatch):
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    install_order_result_reader(exchange, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    exchange.cancel_order = lambda oid: (exchange.cancels.append(oid) or {})
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    original = engine.lp_auto_run_once()
    _wait_round_cancels(store, original, expected_states={'o1': 'unknown'})
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
    monkeypatch.setattr(store, 'lp_session', lambda session_id: None)
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert 'financial_facts_unknown' in state['block_reasons']
    assert state['funds']['available_usd'] is None
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    assert state['slots']['occupied'] == 1
    assert state['intents'][0]['session_id'] and store.lp_session(state['intents'][0]['session_id']) is None
    assert state['last_round']['actions'] == [] and state['active_plan'] is None
    assert exchange.cancels == ['o1']
    assert len(exchange.posts) == 1


def test_failed_market_does_not_block_healthy_market_rotation(tmp_path, monkeypatch):
    from collections import Counter
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from copy import deepcopy
    import json
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=3, target=2)
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
    initial = engine.lp_auto_state()
    assert len(exchange.posts) == 2
    assert Counter(post['token_id'] for post in exchange.posts) == Counter({'m00': 1, 'm01': 1})
    assert initial['slots']['occupied'] == 2
    assert Decimal(initial['funds']['buy_reserved_usd']) == 16
    originals = {}
    for intent in initial['intents']:
        token = intent['token_id']
        assert token in {'m00', 'm01'} and token not in originals
        session = store.lp_session(intent['session_id'])
        audit, = [a for a in store.lp_actions(session['session_id']) if a['role'] == 'entry']
        order_id = session['entry_order_id']
        order, = [o for o in exchange.orders if o['order_id'] == order_id]
        assert intent['order_id'] == audit['order_id'] == order_id
        assert session['token_id'] == audit['token_id'] == order['token_id'] == token
        assert audit['session_id'] == intent['session_id'] == session['session_id']
        assert session['idempotency_key'] == 'lp-auto:' + intent['intent_id']
        assert audit['state'] == 'accepted' and order['side'] == 'BUY' and order['status'] == 'LIVE'
        assert Decimal(str(order['price'])) == Decimal('.40')
        assert Decimal(str(order['original_size'])) == 20 and Decimal(str(order['size_matched'])) == 0
        originals[token] = dict(intent=deepcopy(intent), session=deepcopy(session),
                                audit=deepcopy(audit), order=deepcopy(order))
    assert set(originals) == {'m00', 'm01'}
    assert originals['m00']['order']['order_id'] != originals['m01']['order']['order_id']
    for post in exchange.posts:
        assert post['side'] == 'BUY' and post['price'] == Decimal('.40') and post['quantity'] == 20
    failed_id = originals['m00']['order']['order_id']
    healthy_id = originals['m01']['order']['order_id']
    observations = []
    try:
        advance_auto_wait(engine, monkeypatch)
        exchange.rewards['m02'] = Decimal('30')
        refresh(lp, exchange, 3)
        read = exchange.lp_snapshot

        def unavailable(request):
            if request['token_id'] == 'm00':
                raise ValueError('order_read_failed')
            return read(request)

        exchange.lp_snapshot = unavailable
        advance_auto_wait(engine, monkeypatch)
        state = engine.lp_auto_run_once()
        observations.append(deepcopy(state))
        cancel, = _wait_round_cancels(store, state)
        assert cancel['order_id'] == healthy_id and cancel['session_id'] == originals['m01']['session']['session_id']
        assert cancel['rotation_round_id'] == state['last_round']['round_id'] and cancel['state'] == 'accepted'
        assert cancel['post_started'] is True and cancel['action_id'] and cancel['action_key']
        assert next(a for a in store.lp_actions(cancel['session_id']) if a['action_key'] == cancel['action_key']) == cancel
        assert exchange.cancels == [healthy_id], 'Only the exact original healthy m01 may rotate'
        assert failed_id not in exchange.cancels
        assert len(exchange.posts) == 2
        assert Decimal(state['funds']['buy_reserved_usd']) == 16
        failed, = [o for o in exchange.orders if o['order_id'] == failed_id]
        assert failed == originals['m00']['order'] and failed['status'] == 'LIVE'
        healthy, = [o for o in exchange.orders if o['order_id'] == healthy_id]
        assert healthy['token_id'] == 'm01'
        healthy['status'] = 'CANCELED'
        advance_auto_wait(engine, monkeypatch)
        state = engine.lp_auto_run_once()
        observations.append(deepcopy(state))
        assert len(exchange.posts) == 3
        assert Counter(post['token_id'] for post in exchange.posts) == Counter({'m00': 1, 'm01': 1, 'm02': 1})
        assert exchange.cancels == [healthy_id] and failed_id not in exchange.cancels
        failed, = [o for o in exchange.orders if o['order_id'] == failed_id]
        assert failed == originals['m00']['order'] and failed['status'] == 'LIVE'
        new, = [o for o in exchange.orders if o['token_id'] == 'm02']
        assert new['order_id'] not in {failed_id, healthy_id} and new['status'] == 'LIVE'
        assert new['side'] == 'BUY' and Decimal(str(new['price'])) == Decimal('.40')
        assert Decimal(str(new['original_size'])) == 20
        assert state['slots']['occupied'] == 2
        assert Decimal(state['funds']['buy_reserved_usd']) == 16
        for token, original in originals.items():
            session = store.lp_session(original['session']['session_id'])
            assert session['session_id'] == original['session']['session_id']
            assert session['idempotency_key'] == original['session']['idempotency_key']
            entry, = [a for a in store.lp_actions(session['session_id']) if a['role'] == 'entry']
            assert entry == original['audit']
        new_intent, = [i for i in state['intents'] if i['token_id'] == 'm02']
        assert new_intent['intent_id'] not in {v['intent']['intent_id'] for v in originals.values()}
        assert new_intent['session_id'] not in {v['session']['session_id'] for v in originals.values()}
        assert new_intent['order_id'] == new['order_id']
    finally:
        (tmp_path / 'CI02-rotation-proof.json').write_text(json.dumps(dict(
            originals=originals, observations=observations, posts=exchange.posts,
            cancels=exchange.cancels, orders=exchange.orders), default=str, indent=2) + '\n')
