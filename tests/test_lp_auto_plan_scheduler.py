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
    """#329 retains the acquired financial source through a later ranking generation."""
    from collections import Counter
    from copy import deepcopy
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
    active_financial_reads = []
    for name, counter in [('lp_account_snapshot', 'account'),
                          ('lp_account_snapshot_shared', 'shared'),
                          ('lp_market_metadata_fresh', 'metadata'),
                          ('lp_reward_catalog', 'reward'), ('lp_order_books', 'books')]:
        external = getattr(exchange, name)
        def observed(*args, _read=external, _counter=counter, **kwargs):
            reads[_counter] += 1
            if _counter in ('account', 'shared') and engine.lp_auto_state()['active_plan']:
                active_financial_reads.append(_counter)
            trace.append((_counter, args, kwargs))
            result = _read(*args, **kwargs)
            if _counter == 'reward' and race_enabled[0] and not invalidated_at:
                assert reads['shared'] == 1, 'the complete account read precedes ranking'
                assert store.lp_advance_trade_generation(original_generation)
                assert store.lp_trade_generation() == original_generation + 1
                invalidated_at.append(pool.NOW)
            return result
        monkeypatch.setattr(exchange, name, observed)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    first_reward = next(row for row in trace if row[0] == 'reward')
    assert first_reward[2]['condition_ids'] == (('A',) if initial else ('B',))
    assert len(invalidated_at) == 1
    assert [r['token_id'] for r in state['last_round']['targets']] == ['B', 'C', 'E', 'F', 'G']
    original = deepcopy(state['last_round'])
    original_round = original['round_id']
    original_actions = [a['action_id'] for a in original['actions']]
    assert state['active_plan'] is None and original['completed_at']
    assert state['plan_wait']['kind'] == 'round'
    completed = datetime.fromisoformat(original['completed_at'])
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=1)
    assert reads['shared'] == reads['account'] == 1
    original_audits = {a['session_id']: deepcopy(store.lp_actions(a['session_id']))
        for a in original['actions'] if a.get('session_id')}
    if initial:
        assert exchange.posts == [] and exchange.cancels == ['original-A', 'original-D', 'original-I']
        assert live_orders(exchange) == {k: v for k, v in original_orders.items() if k in {'B', 'C'}}
        dependent = [a for a in original['actions'] if a['kind'] == 'buy']
        assert {a['token_id'] for a in dependent} == {'E', 'F', 'G'}
        assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
        first_reads = dict(reads)
        monkeypatch.setattr(pool, 'NOW', completed + timedelta(milliseconds=500))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert reads == first_reads and exchange.posts == []
        assert exchange.cancels == ['original-A', 'original-D', 'original-I']
        assert engine.lp_auto_state()['last_round'] == original
        for sid, audits in original_audits.items():
            assert store.lp_actions(sid) == audits
        monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=1))
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        assert state['last_round']['round_id'] != original_round
        assert {a['action_id'] for a in state['last_round']['actions']}.isdisjoint(original_actions)
        assert reads['shared'] == reads['account'] == 2
        current_buys = [a for a in state['last_round']['actions'] if a['kind'] == 'buy']
        assert {a['token_id'] for a in current_buys} == {'E', 'F', 'G'}
        assert all(a['state'] == 'success' and a.get('session_id') for a in current_buys)
        for sid, audits in original_audits.items():
            assert store.lp_actions(sid) == audits
    else:
        assert state['last_round']['round_id'] == original_round
        assert [a['action_id'] for a in state['last_round']['actions']] == original_actions
        assert all(a['state'] == 'success' for a in original['actions'])
    assert Counter(post['token_id'] for post in exchange.posts) == Counter(
        ['E', 'F', 'G'] if initial else ['B', 'C', 'E', 'F', 'G'])
    assert [r['token_id'] for r in state['last_round']['targets']] == ['B', 'C', 'E', 'F', 'G']
    assert state['active_plan'] is None and not active_financial_reads
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
    assert all(p['price'] == Decimal('.39') and p['quantity'] == 20 for p in exchange.posts)
    completed = datetime.fromisoformat(state['last_round']['completed_at'])
    assert state['plan_wait']['kind'] == 'round'
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=1)
    stopped_reads = dict(reads)
    stopped_posts, stopped_cancels = list(exchange.posts), list(exchange.cancels)
    monkeypatch.setattr(pool, 'NOW', completed + timedelta(milliseconds=500))
    scheduler.request_check()
    assert not scheduler.run_due()
    assert reads == stopped_reads and exchange.posts == stopped_posts and exchange.cancels == stopped_cancels


@pytest.mark.parametrize('window', ['first_cancel_result', 'unknown_POST_result'])
def test_current_read_failure_ends_check_without_replaying_prior_requests(tmp_path, monkeypatch, window):
    import json
    import sqlite3
    from copy import deepcopy
    from collections import Counter
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    monkeypatch.setattr(pool,'NOW',pool.NOW)
    initial=('A','B','C','D','I') if window=='first_cancel_result' else ('B','C')
    engine,exchange,lp,store=plan_setup(tmp_path,monkeypatch,initial)
    result_reader=install_order_result_reader(exchange,monkeypatch)
    phase,evidence=['original_local_cycle'],dict(states=[],full_reads=[],narrow_reads=[],financial_reads=[],market_reads=[],preparations=[],holds=[])
    full_mode,narrow_mode=['normal'],['normal']
    blockers=dict(full=None,narrow=None)
    def document():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state=deepcopy(engine.lp_auto_state())
        evidence['states'].append(dict(label=label,state=state,document=document(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=deepcopy(exchange.orders),trades=deepcopy(exchange.trades),positions=deepcopy(exchange.positions),
            posts=deepcopy(exchange.posts),cancels=list(exchange.cancels)))
        return state
    def known(rows):
        for row in rows:
            if (row.get('side')=='BUY' and row.get('token_id')==row.get('condition_id')
                    and row.get('status') in ('LIVE','CANCELED') and row.get('fill_quantity_known') in (None,True)
                    and Decimal(str(row['price']))==Decimal('.39') and Decimal(str(row['original_size']))==20
                    and Decimal(str(row['size_matched'])) in (Decimal(0),Decimal(8))):row['fill_quantity_known']=True
        return rows
    def hold(endpoint):
        value=blockers[endpoint]
        if value is not None:
            entered,release=value
            evidence['holds'].append(dict(endpoint=endpoint,phase=phase[0],started=pool.NOW))
            entered.set();assert release.wait(5),'Independent external current-read watchdog'
    for method in ('lp_account_snapshot','lp_market_metadata_fresh','lp_reward_catalog','lp_order_books'):
        original_read=getattr(exchange,method)
        def read(*args,_method=method,_read=original_read,**kwargs):
            target=evidence['financial_reads'] if _method=='lp_account_snapshot' else evidence['market_reads']
            target.append(dict(method=_method,phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),at=pool.NOW))
            return _read(*args,**kwargs)
        monkeypatch.setattr(exchange,method,read)
    full_reader=exchange.lp_account_snapshot_shared
    def full(*args,**kwargs):
        hold('full')
        record=dict(phase=phase[0],mode=full_mode[0],started=pool.NOW,active_plan=bool(engine.lp_auto_state()['active_plan']))
        evidence['full_reads'].append(record)
        if full_mode[0]=='timeout':
            pool.NOW+=timedelta(seconds=3)
            try:raise TimeoutError('required current account read unavailable')
            except TimeoutError as exc:
                record.update(ended=pool.NOW,error=dict(type=type(exc).__name__,message=str(exc)))
                raise
        packet=deepcopy(full_reader(*args,**kwargs))
        packet['open_orders']=known(deepcopy([o for o in exchange.orders if o['status']=='LIVE']))
        packet['positions']=deepcopy(exchange.positions);packet['raw_trades']=deepcopy(exchange.trades)
        if full_mode[0]=='incomplete':packet.update(open_orders_complete=False,pagination_complete=False)
        record.update(ended=pool.NOW,packet=deepcopy(packet));return packet
    monkeypatch.setattr(exchange,'lp_account_snapshot_shared',full)
    def narrow(request):
        hold('narrow')
        record=dict(phase=phase[0],mode=narrow_mode[0],request=deepcopy(request),started=pool.NOW)
        evidence['narrow_reads'].append(record)
        packet=result_reader(request)
        for field in ('orders','open_orders'):known(packet[field])
        if narrow_mode[0] in ('incomplete','incomplete_delay'):
            if narrow_mode[0]=='incomplete_delay':pool.NOW+=timedelta(seconds=3)
            packet.update(authenticated=False,open_orders_complete=False,pagination_complete=False,
                read_ended_at=pool.NOW,checked_at=pool.NOW)
        elif narrow_mode[0]=='generation':
            generation=store.lp_trade_generation();assert store.lp_advance_trade_generation(generation)
            record.update(generation_before=generation,generation_after=store.lp_trade_generation())
        record.update(ended=pool.NOW,packet=deepcopy(packet));return packet
    monkeypatch.setattr(exchange,'lp_order_result_snapshot',narrow)
    catalog_read=exchange.lp_reward_catalog
    def prepare(service):
        def catalog(*,condition_ids=None,**kwargs):
            value=catalog_read(condition_ids=tuple(exchange.rewards) if condition_ids is None else condition_ids,**kwargs)
            return {**value,'state':'known','complete':True}
        def history(token_ids,*,start_ts,end_ts,**kwargs):
            return dict(state='known',history={t:[dict(t=start_ts,p='.390'),dict(t=end_ts,p='.391')] for t in token_ids})
        def competition(**kwargs):
            return dict(state='known',complete=True,checked_at=pool.NOW,round_checked_at=pool.NOW,
                competitiveness={t:(Decimal('2.5'),pool.NOW) for t in exchange.rewards})
        monkeypatch.setattr(exchange,'lp_reward_catalog',catalog)
        monkeypatch.setattr(exchange,'lp_market_metadata',exchange.lp_market_metadata_fresh,raising=False)
        monkeypatch.setattr(exchange,'lp_price_history',history,raising=False)
        monkeypatch.setattr(exchange,'lp_market_competitiveness',competition,raising=False)
        competition_state=service.refresh_competition_cache();prepared=service.refresh_price_history();qualified=service.refresh_candidates(force=True)
        evidence['preparations'].append(deepcopy(dict(phase=phase[0],competition=competition_state,prepared=prepared,qualified=qualified)))
        assert competition_state['state']=='known' and prepared['preparation_outcome']=='success'
    def interval(state,dispatched=False):
        assert state['active_plan'] is None and state['plan_wait']['kind']=='round'
        start=state['last_round']['completed_at'] if dispatched else state['plan_wait']['started_at']
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(start)+timedelta(seconds=30)
    def original_audits():return {s['session_id']:deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    def immutable(original):
        for sid,rows in original.items():
            for row in rows:assert next(a for a in store.lp_actions(sid) if a['action_key']==row['action_key'])==row
    def counts():return tuple(len(evidence[k]) for k in ('full_reads','narrow_reads','financial_reads','market_reads'))
    def quiet(state,elapsed):
        previous=counts();posts=deepcopy(exchange.posts);cancels=list(exchange.cancels)
        latest=deepcopy(engine.lp_auto_state()['last_round']);wait=deepcopy(state['plan_wait'])
        pool.NOW=datetime.fromisoformat(wait['started_at'])+timedelta(seconds=elapsed)
        assert pool.NOW<datetime.fromisoformat(wait['deadline'])
        scheduler.request_check();scheduler.run_due();engine.lp_auto_run_once()
        assert counts()==previous and exchange.posts==posts and exchange.cancels==cancels
        assert engine.lp_auto_state()['last_round']==latest and engine.lp_auto_state()['plan_wait']==wait
    def fixed_posts(expected):
        assert Counter(p['token_id'] for p in exchange.posts)==Counter(expected)
        assert all(p['side']=='BUY' and Decimal(p['price'])==Decimal('.39') and Decimal(p['quantity'])==20 and p['post_only'] is True for p in exchange.posts)
    def unknown_request(first):
        action,=[a for a in first['last_round']['actions'] if a['kind']=='buy' and a['token_id']=='E']
        sid=action['session_id'];request_id=action.get('request_id',action['action_id'])
        audit=deepcopy(store.lp_actions(sid));assert len(audit)==1 and audit[0]['state']=='unknown' and audit[0]['post_started'] is True and not audit[0].get('order_id')
        intent=deepcopy(document()['intents'][request_id]);assert intent['state']=='unknown' and not intent.get('order_id') and intent['settled'] is False and Decimal(intent['reserved_usd'])==Decimal('7.80')
        return dict(action=action,sid=sid,request_id=request_id,audit=audit,intent=intent)
    def preserve_unknown(value):
        intent=document()['intents'][value['request_id']]
        assert intent['state']=='unknown' and intent['settled'] is False and not intent.get('order_id')
        assert intent['session_id']==value['sid'] and Decimal(intent['reserved_usd'])==Decimal('7.80')
        assert store.lp_actions(value['sid'])==value['audit'] and not store.lp_session(value['sid']).get('entry_order_id')
    def coverage(value,state):
        preserve_unknown(value)
        marker=document()['intents'][value['request_id']]['reservation_coverage'];facts=document()['account_financial_facts']
        assert marker['state']=='covered' and marker['session_id']==value['sid'] and marker['intent_id']==value['request_id']
        assert marker['account_id']==facts['account_id']=='test-wallet' and marker['snapshot_id']==facts['snapshot_id']
        assert state['slots']['occupied']==4 and Decimal(state['funds']['buy_reserved_usd'])==Decimal('31.20')
        assert any(x.get('packet',{}).get('authenticated') is True and all(x['packet'].get(k) is True
            for k in ('balance_complete','open_orders_complete','pagination_complete','positions_complete','trades_complete'))
            and {o['token_id'] for o in x['packet']['open_orders']}=={'B','C','F','G'} for x in evidence['full_reads'] if x['phase']==phase[0])
    def save(filename):
        evidence.update(phase=phase[0],clock=pool.NOW,state=engine.lp_auto_state(),document=document(),sessions=store.lp_sessions(),
            audits=original_audits(),orders=exchange.orders,positions=exchange.positions,trades=exchange.trades,posts=exchange.posts,cancels=exchange.cancels)
        (tmp_path/filename).write_text(json.dumps(evidence,default=str,indent=2)+'\n')
    if window=='unknown_POST_result':
        post=exchange.lp_post_order
        def unknown(signed):
            if signed['token_id']=='E':
                exchange.posts.append(signed);raise TimeoutError('original E POST receipt unknown')
            return post(signed)
        monkeypatch.setattr(exchange,'lp_post_order',unknown)
    scheduler=LPAutoScheduler(engine,clock=lambda:pool.NOW)
    try:
        assert scheduler.run_due();first=capture('original_ended_cycle');interval(first,True)
        original=deepcopy(first['last_round']);audits=original_audits();source_asof=first['funds']['as_of']
        expected=[] if window=='first_cancel_result' else ['E','F','G'];fixed_posts(expected)
        assert first['slots']['occupied']==5 and Decimal(first['funds']['buy_reserved_usd'])==39
        if window=='first_cancel_result':
            assert Counter(exchange.cancels)==Counter(['original-A','original-D','original-I'])
            assert all(a['state']=='not_sent' and a['reason']=='next_round_capacity' for a in original['actions'] if a['kind']=='buy')
        value=unknown_request(first) if window=='unknown_POST_result' else None
        full_mode[0]='timeout';narrow_mode[0]='incomplete' if value is None else 'normal'
        for attempt in range(2):
            phase[0]='required_bad_current_'+str(attempt)
            at_deadline(monkeypatch,engine);fs,ns=len(evidence['full_reads']),len(evidence['narrow_reads'])
            assert scheduler.run_due();failed=capture('failed_current_'+str(attempt));interval(failed)
            assert failed['last_round']['round_id']!=original['round_id'] and failed['last_round']['actions']==[]
            assert failed['last_round']['reason']=='account_order_sync_unknown'
            assert len(evidence['full_reads'])==fs+1 and evidence['full_reads'][-1]['error']['type']=='TimeoutError'
            assert evidence['full_reads'][-1]['ended']-evidence['full_reads'][-1]['started']==timedelta(seconds=3)
            assert len(evidence['narrow_reads'])==ns+(1 if value is None else 0)
            if value is None:
                packet=evidence['narrow_reads'][-1]['packet'];assert packet['authenticated'] is False and packet['open_orders_complete'] is False and packet['pagination_complete'] is False
            assert datetime.fromisoformat(failed['plan_wait']['started_at'])==pool.NOW
            assert failed['funds']['as_of']==source_asof and failed['slots']['occupied']==5 and Decimal(failed['funds']['buy_reserved_usd'])==39
            fixed_posts(expected);immutable(audits)
            if value is not None:preserve_unknown(value)
            for elapsed in (1,29):quiet(failed,elapsed)
        full_mode[0]=narrow_mode[0]='normal';phase[0]='healthy_current_after_two_failures'
        if value is not None:engine.lp_auto_set_desired_running(False)
        at_deadline(monkeypatch,engine)
        if value is None:prepare(engine._lp)
        assert scheduler.run_due();state=capture('healthy_new_current_observation');interval(state)
        assert state['last_round']['round_id']!=original['round_id'];immutable(audits);fixed_posts(['E','F','G'])
        if value is None:
            assert state['last_round']['completed_at'] and set(live_orders(exchange))=={'B','C','E','F','G'}
            assert live_orders(exchange)['B']=='original-B' and live_orders(exchange)['C']=='original-C'
            assert state['slots']['occupied']==5 and Decimal(state['funds']['buy_reserved_usd'])==39
            assert Counter(exchange.cancels)==Counter(['original-A','original-D','original-I'])
            assert {a['action_id'] for a in state['last_round']['actions']}.isdisjoint({a['action_id'] for a in original['actions']})
        else:
            assert state['desired_running'] is False and state['last_round']['actions']==[] and state['last_round']['checked_at']
            assert set(live_orders(exchange))=={'B','C','F','G'} and exchange.cancels==[]
            coverage(value,state)
        assert not any(x['active_plan'] for x in evidence['financial_reads']+evidence['market_reads'])
        evidence.update(completed='full',parameter=window,original_round=original,original_audits=audits,
            seam_mapping='required full transport TimeoutError + separate explicit incomplete narrow external protocol; no native SDK normalization claim/no monotonic change')
    finally:save('SC-api-diagnostics.json')

@pytest.mark.parametrize('receipt', ['timeout', 'rejected', 'ACK', 'ACK-generation-race', 'ACK-retry-api-failure'])
def test_live_after_cancel_automatically_reconciles_and_retries(tmp_path, monkeypatch, receipt):
    from tests.test_lp_auto_independent_cancel_contract import _wait_audits
    from time import monotonic
    import json
    import sqlite3
    from copy import deepcopy
    from collections import Counter
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    monkeypatch.setattr(pool,'NOW',pool.NOW)
    engine,exchange,lp,store=plan_setup(tmp_path,monkeypatch,('A','B','C','D','I'))
    result_reader=install_order_result_reader(exchange,monkeypatch)
    phase,evidence=['original_local_cycle'],dict(states=[],full_reads=[],narrow_reads=[],financial_reads=[],market_reads=[],preparations=[],holds=[])
    full_mode,narrow_mode=['normal'],['normal']
    blockers=dict(full=None,narrow=None)
    def document():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state=deepcopy(engine.lp_auto_state())
        evidence['states'].append(dict(label=label,state=state,document=document(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=deepcopy(exchange.orders),trades=deepcopy(exchange.trades),positions=deepcopy(exchange.positions),
            posts=deepcopy(exchange.posts),cancels=list(exchange.cancels)))
        return state
    def known(rows):
        for row in rows:
            if (row.get('side')=='BUY' and row.get('token_id')==row.get('condition_id')
                    and row.get('status') in ('LIVE','CANCELED') and row.get('fill_quantity_known') in (None,True)
                    and Decimal(str(row['price']))==Decimal('.39') and Decimal(str(row['original_size']))==20
                    and Decimal(str(row['size_matched'])) in (Decimal(0),Decimal(8))):row['fill_quantity_known']=True
        return rows
    def hold(endpoint):
        value=blockers[endpoint]
        if value is not None:
            entered,release=value
            evidence['holds'].append(dict(endpoint=endpoint,phase=phase[0],started=pool.NOW))
            entered.set();assert release.wait(5),'Independent external current-read watchdog'
    for method in ('lp_account_snapshot','lp_market_metadata_fresh','lp_reward_catalog','lp_order_books'):
        original_read=getattr(exchange,method)
        def read(*args,_method=method,_read=original_read,**kwargs):
            target=evidence['financial_reads'] if _method=='lp_account_snapshot' else evidence['market_reads']
            target.append(dict(method=_method,phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),at=pool.NOW))
            return _read(*args,**kwargs)
        monkeypatch.setattr(exchange,method,read)
    full_reader=exchange.lp_account_snapshot_shared
    def full(*args,**kwargs):
        hold('full')
        record=dict(phase=phase[0],mode=full_mode[0],started=pool.NOW,active_plan=bool(engine.lp_auto_state()['active_plan']))
        evidence['full_reads'].append(record)
        if full_mode[0]=='timeout':
            pool.NOW+=timedelta(seconds=3)
            try:raise TimeoutError('required current account read unavailable')
            except TimeoutError as exc:
                record.update(ended=pool.NOW,error=dict(type=type(exc).__name__,message=str(exc)))
                raise
        packet=deepcopy(full_reader(*args,**kwargs))
        packet['open_orders']=known(deepcopy([o for o in exchange.orders if o['status']=='LIVE']))
        packet['positions']=deepcopy(exchange.positions);packet['raw_trades']=deepcopy(exchange.trades)
        if full_mode[0]=='incomplete':packet.update(open_orders_complete=False,pagination_complete=False)
        record.update(ended=pool.NOW,packet=deepcopy(packet));return packet
    monkeypatch.setattr(exchange,'lp_account_snapshot_shared',full)
    def narrow(request):
        hold('narrow')
        record=dict(phase=phase[0],mode=narrow_mode[0],request=deepcopy(request),started=pool.NOW)
        evidence['narrow_reads'].append(record)
        packet=result_reader(request)
        for field in ('orders','open_orders'):known(packet[field])
        if narrow_mode[0] in ('incomplete','incomplete_delay'):
            if narrow_mode[0]=='incomplete_delay':pool.NOW+=timedelta(seconds=3)
            packet.update(authenticated=False,open_orders_complete=False,pagination_complete=False,
                read_ended_at=pool.NOW,checked_at=pool.NOW)
        elif narrow_mode[0]=='generation':
            generation=store.lp_trade_generation();assert store.lp_advance_trade_generation(generation)
            record.update(generation_before=generation,generation_after=store.lp_trade_generation())
        record.update(ended=pool.NOW,packet=deepcopy(packet));return packet
    monkeypatch.setattr(exchange,'lp_order_result_snapshot',narrow)
    catalog_read=exchange.lp_reward_catalog
    def prepare(service):
        def catalog(*,condition_ids=None,**kwargs):
            value=catalog_read(condition_ids=tuple(exchange.rewards) if condition_ids is None else condition_ids,**kwargs)
            return {**value,'state':'known','complete':True}
        def history(token_ids,*,start_ts,end_ts,**kwargs):
            return dict(state='known',history={t:[dict(t=start_ts,p='.390'),dict(t=end_ts,p='.391')] for t in token_ids})
        def competition(**kwargs):
            return dict(state='known',complete=True,checked_at=pool.NOW,round_checked_at=pool.NOW,
                competitiveness={t:(Decimal('2.5'),pool.NOW) for t in exchange.rewards})
        monkeypatch.setattr(exchange,'lp_reward_catalog',catalog)
        monkeypatch.setattr(exchange,'lp_market_metadata',exchange.lp_market_metadata_fresh,raising=False)
        monkeypatch.setattr(exchange,'lp_price_history',history,raising=False)
        monkeypatch.setattr(exchange,'lp_market_competitiveness',competition,raising=False)
        competition_state=service.refresh_competition_cache();prepared=service.refresh_price_history();qualified=service.refresh_candidates(force=True)
        evidence['preparations'].append(deepcopy(dict(phase=phase[0],competition=competition_state,prepared=prepared,qualified=qualified)))
        assert competition_state['state']=='known' and prepared['preparation_outcome']=='success'
    def interval(state,dispatched=False):
        assert state['active_plan'] is None and state['plan_wait']['kind']=='round'
        start=state['last_round']['completed_at'] if dispatched else state['plan_wait']['started_at']
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(start)+timedelta(seconds=30)
    def original_audits():return {s['session_id']:deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    def immutable(original):
        for sid,rows in original.items():
            for row in rows:assert next(a for a in store.lp_actions(sid) if a['action_key']==row['action_key'])==row
    def counts():return tuple(len(evidence[k]) for k in ('full_reads','narrow_reads','financial_reads','market_reads'))
    def quiet(state,elapsed):
        previous=counts();posts=deepcopy(exchange.posts);cancels=list(exchange.cancels)
        latest=deepcopy(engine.lp_auto_state()['last_round']);wait=deepcopy(state['plan_wait'])
        pool.NOW=datetime.fromisoformat(wait['started_at'])+timedelta(seconds=elapsed)
        assert pool.NOW<datetime.fromisoformat(wait['deadline'])
        scheduler.request_check();scheduler.run_due();engine.lp_auto_run_once()
        assert counts()==previous and exchange.posts==posts and exchange.cancels==cancels
        assert engine.lp_auto_state()['last_round']==latest and engine.lp_auto_state()['plan_wait']==wait
    def fixed_posts(expected):
        assert Counter(p['token_id'] for p in exchange.posts)==Counter(expected)
        assert all(p['side']=='BUY' and Decimal(p['price'])==Decimal('.39') and Decimal(p['quantity'])==20 and p['post_only'] is True for p in exchange.posts)
    def unknown_request(first):
        action,=[a for a in first['last_round']['actions'] if a['kind']=='buy' and a['token_id']=='E']
        sid=action['session_id'];request_id=action.get('request_id',action['action_id'])
        audit=deepcopy(store.lp_actions(sid));assert len(audit)==1 and audit[0]['state']=='unknown' and audit[0]['post_started'] is True and not audit[0].get('order_id')
        intent=deepcopy(document()['intents'][request_id]);assert intent['state']=='unknown' and not intent.get('order_id') and intent['settled'] is False and Decimal(intent['reserved_usd'])==Decimal('7.80')
        return dict(action=action,sid=sid,request_id=request_id,audit=audit,intent=intent)
    def preserve_unknown(value):
        intent=document()['intents'][value['request_id']]
        assert intent['state']=='unknown' and intent['settled'] is False and not intent.get('order_id')
        assert intent['session_id']==value['sid'] and Decimal(intent['reserved_usd'])==Decimal('7.80')
        assert store.lp_actions(value['sid'])==value['audit'] and not store.lp_session(value['sid']).get('entry_order_id')
    def coverage(value,state):
        preserve_unknown(value)
        marker=document()['intents'][value['request_id']]['reservation_coverage'];facts=document()['account_financial_facts']
        assert marker['state']=='covered' and marker['session_id']==value['sid'] and marker['intent_id']==value['request_id']
        assert marker['account_id']==facts['account_id']=='test-wallet' and marker['snapshot_id']==facts['snapshot_id']
        assert state['slots']['occupied']==4 and Decimal(state['funds']['buy_reserved_usd'])==Decimal('31.20')
        assert any(x.get('packet',{}).get('authenticated') is True and all(x['packet'].get(k) is True
            for k in ('balance_complete','open_orders_complete','pagination_complete','positions_complete','trades_complete'))
            and {o['token_id'] for o in x['packet']['open_orders']}=={'B','C','F','G'} for x in evidence['full_reads'] if x['phase']==phase[0])
    def save(filename):
        evidence.update(phase=phase[0],clock=pool.NOW,state=engine.lp_auto_state(),document=document(),sessions=store.lp_sessions(),
            audits=original_audits(),orders=exchange.orders,positions=exchange.positions,trades=exchange.trades,posts=exchange.posts,cancels=exchange.cancels)
        (tmp_path/filename).write_text(json.dumps(evidence,default=str,indent=2)+'\n')
    exchange.cancel_terminal=False;cancel=exchange.cancel_order
    def response(order_id):
        if receipt.startswith('ACK'):return cancel(order_id)
        exchange.cancels.append(order_id)
        if receipt=='timeout':raise TimeoutError('simulated cancel timeout')
        return dict(canceled=[],not_canceled={order_id:'temporarily unavailable'})
    monkeypatch.setattr(exchange,'cancel_order',response)
    def completed_cancel_receipts(state):
        round_id=state['last_round']['round_id']
        expected='unknown' if receipt=='timeout' else 'rejected' if receipt=='rejected' else 'accepted'
        return _wait_audits(store,lambda rows:{a['order_id']:a['state'] for a in rows
            if a.get('rotation_round_id')==round_id}=={oid:expected for oid in ('original-A','original-D','original-I')})
    scheduler=LPAutoScheduler(engine,clock=lambda:pool.NOW)
    try:
        assert scheduler.run_due();first=capture('original_three_live_requests_closed');interval(first,True)
        completed_cancel_receipts(first)
        original=deepcopy(first['last_round']);audits=original_audits();victims=['original-A','original-D','original-I']
        assert Counter(exchange.cancels)==Counter(victims) and exchange.posts==[]
        assert first['slots']['occupied']==5 and Decimal(first['funds']['buy_reserved_usd'])==39
        quiet(first,29);immutable(audits)
        if receipt in ('ACK-generation-race','ACK-retry-api-failure'):
            phase[0]='current_missing_or_stale_original_proof'
            narrow_mode[0]='generation' if receipt=='ACK-generation-race' else 'incomplete_delay'
            owned={s['session_id']:deepcopy(s.get('yield_rotation_cancel_owners')) for s in store.lp_sessions()}
            at_deadline(monkeypatch,engine);ns=len(evidence['narrow_reads'])
            assert scheduler.run_due();failed=capture('fresh_check_no_current_cancel_authority');interval(failed)
            assert len(evidence['narrow_reads'])==ns+1 and Counter(exchange.cancels)==Counter(victims) and exchange.posts==[]
            assert failed['slots']['occupied']==5 and Decimal(failed['funds']['buy_reserved_usd'])==39
            assert {s['session_id']:s.get('yield_rotation_cancel_owners') for s in store.lp_sessions()}==owned
            if receipt=='ACK-retry-api-failure':
                packet=evidence['narrow_reads'][-1]['packet'];assert packet['authenticated'] is False and packet['open_orders_complete'] is False
                assert datetime.fromisoformat(failed['plan_wait']['started_at'])==evidence['narrow_reads'][-1]['ended']
            else:assert evidence['narrow_reads'][-1]['generation_after']==evidence['narrow_reads'][-1]['generation_before']+1
            quiet(failed,29);immutable(audits);narrow_mode[0]='normal'
        phase[0]='new_current_three_separate_requests'
        at_deadline(monkeypatch,engine);prepare(engine._lp)
        assert scheduler.run_due();second=capture('new_round_three_live_cancel_attempts');interval(second,True)
        completed_cancel_receipts(second)
        assert second['last_round']['round_id']!=original['round_id']
        assert Counter(exchange.cancels)==Counter(victims*2) and exchange.posts==[]
        assert second['slots']['occupied']==5 and Decimal(second['funds']['buy_reserved_usd'])==39
        immutable(audits)
        attempts=[a for s in store.lp_sessions() for a in store.lp_actions(s['session_id']) if a.get('role')=='reconciliation_cancel']
        assert len(attempts)==6 and len({a['action_id'] for a in attempts})==len({a['action_key'] for a in attempts})==6
        assert {a['rotation_round_id'] for a in attempts}=={original['round_id'],second['last_round']['round_id']}
        assert all(a['state']==('unknown' if receipt=='timeout' else 'rejected' if receipt=='rejected' else 'accepted') for a in attempts)
        all_audits=original_audits()
        for order in exchange.orders:
            if order['order_id'] in victims:order['status']='CANCELED'
        phase[0]='next_current_actual_terminal_three_free'
        at_deadline(monkeypatch,engine);prepare(engine._lp)
        assert scheduler.run_due();final=capture('new_current_EFG_after_terminal');interval(final,True)
        fixed_posts(['E','F','G']);assert set(live_orders(exchange))=={'B','C','E','F','G'}
        assert final['slots']['occupied']==5 and Decimal(final['funds']['buy_reserved_usd'])==39
        assert Counter(exchange.cancels)==Counter(victims*2);immutable(all_audits)
        assert not any(x['active_plan'] for x in evidence['financial_reads']+evidence['market_reads'])
        evidence.update(completed='full',parameter=receipt,original_round=original,original_audits=audits,second_audits=all_audits)
    finally:
        sender_deadline=monotonic()+5
        while True:
            try:
                lp.drain_submissions(timeout=.05)
                break
            except RuntimeError as exc:
                assert monotonic()<sender_deadline,str(exc)
                Event().wait(.01)
        save('SC-live-diagnostics.json')

@pytest.mark.parametrize('fill,window', [('0', 'registered'), ('8', 'registered'),
    ('8', 'before_registration'), ('8', 'live_partial_before_registration')])
def test_current_absence_supports_new_round_without_phantom_hold(tmp_path, monkeypatch, fill, window):
    import json
    import sqlite3
    from copy import deepcopy
    from collections import Counter
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    monkeypatch.setattr(pool,'NOW',pool.NOW)
    engine,exchange,lp,store=plan_setup(tmp_path,monkeypatch,('A','B','C','D','I'))
    result_reader=install_order_result_reader(exchange,monkeypatch)
    phase,evidence=['original_local_cycle'],dict(states=[],full_reads=[],narrow_reads=[],financial_reads=[],market_reads=[],preparations=[],holds=[])
    full_mode,narrow_mode=['normal'],['normal']
    blockers=dict(full=None,narrow=None)
    def document():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state=deepcopy(engine.lp_auto_state())
        evidence['states'].append(dict(label=label,state=state,document=document(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=deepcopy(exchange.orders),trades=deepcopy(exchange.trades),positions=deepcopy(exchange.positions),
            posts=deepcopy(exchange.posts),cancels=list(exchange.cancels)))
        return state
    def known(rows):
        for row in rows:
            if (row.get('side')=='BUY' and row.get('token_id')==row.get('condition_id')
                    and row.get('status') in ('LIVE','CANCELED') and row.get('fill_quantity_known') in (None,True)
                    and Decimal(str(row['price']))==Decimal('.39') and Decimal(str(row['original_size']))==20
                    and Decimal(str(row['size_matched'])) in (Decimal(0),Decimal(8))):row['fill_quantity_known']=True
        return rows
    def hold(endpoint):
        value=blockers[endpoint]
        if value is not None:
            entered,release=value
            evidence['holds'].append(dict(endpoint=endpoint,phase=phase[0],started=pool.NOW))
            entered.set();assert release.wait(5),'Independent external current-read watchdog'
    for method in ('lp_account_snapshot','lp_market_metadata_fresh','lp_reward_catalog','lp_order_books'):
        original_read=getattr(exchange,method)
        def read(*args,_method=method,_read=original_read,**kwargs):
            target=evidence['financial_reads'] if _method=='lp_account_snapshot' else evidence['market_reads']
            target.append(dict(method=_method,phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),at=pool.NOW))
            return _read(*args,**kwargs)
        monkeypatch.setattr(exchange,method,read)
    full_reader=exchange.lp_account_snapshot_shared
    def full(*args,**kwargs):
        hold('full')
        record=dict(phase=phase[0],mode=full_mode[0],started=pool.NOW,active_plan=bool(engine.lp_auto_state()['active_plan']))
        evidence['full_reads'].append(record)
        if full_mode[0]=='timeout':
            pool.NOW+=timedelta(seconds=3)
            try:raise TimeoutError('required current account read unavailable')
            except TimeoutError as exc:
                record.update(ended=pool.NOW,error=dict(type=type(exc).__name__,message=str(exc)))
                raise
        packet=deepcopy(full_reader(*args,**kwargs))
        packet['open_orders']=known(deepcopy([o for o in exchange.orders if o['status']=='LIVE']))
        packet['positions']=deepcopy(exchange.positions);packet['raw_trades']=deepcopy(exchange.trades)
        if full_mode[0]=='incomplete':packet.update(open_orders_complete=False,pagination_complete=False)
        record.update(ended=pool.NOW,packet=deepcopy(packet));return packet
    monkeypatch.setattr(exchange,'lp_account_snapshot_shared',full)
    def narrow(request):
        hold('narrow')
        record=dict(phase=phase[0],mode=narrow_mode[0],request=deepcopy(request),started=pool.NOW)
        evidence['narrow_reads'].append(record)
        packet=result_reader(request)
        for field in ('orders','open_orders'):known(packet[field])
        if narrow_mode[0] in ('incomplete','incomplete_delay'):
            if narrow_mode[0]=='incomplete_delay':pool.NOW+=timedelta(seconds=3)
            packet.update(authenticated=False,open_orders_complete=False,pagination_complete=False,
                read_ended_at=pool.NOW,checked_at=pool.NOW)
        elif narrow_mode[0]=='generation':
            generation=store.lp_trade_generation();assert store.lp_advance_trade_generation(generation)
            record.update(generation_before=generation,generation_after=store.lp_trade_generation())
        record.update(ended=pool.NOW,packet=deepcopy(packet));return packet
    monkeypatch.setattr(exchange,'lp_order_result_snapshot',narrow)
    catalog_read=exchange.lp_reward_catalog
    def prepare(service):
        def catalog(*,condition_ids=None,**kwargs):
            value=catalog_read(condition_ids=tuple(exchange.rewards) if condition_ids is None else condition_ids,**kwargs)
            return {**value,'state':'known','complete':True}
        def history(token_ids,*,start_ts,end_ts,**kwargs):
            return dict(state='known',history={t:[dict(t=start_ts,p='.390'),dict(t=end_ts,p='.391')] for t in token_ids})
        def competition(**kwargs):
            return dict(state='known',complete=True,checked_at=pool.NOW,round_checked_at=pool.NOW,
                competitiveness={t:(Decimal('2.5'),pool.NOW) for t in exchange.rewards})
        monkeypatch.setattr(exchange,'lp_reward_catalog',catalog)
        monkeypatch.setattr(exchange,'lp_market_metadata',exchange.lp_market_metadata_fresh,raising=False)
        monkeypatch.setattr(exchange,'lp_price_history',history,raising=False)
        monkeypatch.setattr(exchange,'lp_market_competitiveness',competition,raising=False)
        competition_state=service.refresh_competition_cache();prepared=service.refresh_price_history();qualified=service.refresh_candidates(force=True)
        evidence['preparations'].append(deepcopy(dict(phase=phase[0],competition=competition_state,prepared=prepared,qualified=qualified)))
        assert competition_state['state']=='known' and prepared['preparation_outcome']=='success'
    def interval(state,dispatched=False):
        assert state['active_plan'] is None and state['plan_wait']['kind']=='round'
        start=state['last_round']['completed_at'] if dispatched else state['plan_wait']['started_at']
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(start)+timedelta(seconds=30)
    def original_audits():return {s['session_id']:deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    def immutable(original):
        for sid,rows in original.items():
            for row in rows:assert next(a for a in store.lp_actions(sid) if a['action_key']==row['action_key'])==row
    def counts():return tuple(len(evidence[k]) for k in ('full_reads','narrow_reads','financial_reads','market_reads'))
    def quiet(state,elapsed):
        previous=counts();posts=deepcopy(exchange.posts);cancels=list(exchange.cancels)
        latest=deepcopy(engine.lp_auto_state()['last_round']);wait=deepcopy(state['plan_wait'])
        pool.NOW=datetime.fromisoformat(wait['started_at'])+timedelta(seconds=elapsed)
        assert pool.NOW<datetime.fromisoformat(wait['deadline'])
        scheduler.request_check();scheduler.run_due();engine.lp_auto_run_once()
        assert counts()==previous and exchange.posts==posts and exchange.cancels==cancels
        assert engine.lp_auto_state()['last_round']==latest and engine.lp_auto_state()['plan_wait']==wait
    def fixed_posts(expected):
        assert Counter(p['token_id'] for p in exchange.posts)==Counter(expected)
        assert all(p['side']=='BUY' and Decimal(p['price'])==Decimal('.39') and Decimal(p['quantity'])==20 and p['post_only'] is True for p in exchange.posts)
    def unknown_request(first):
        action,=[a for a in first['last_round']['actions'] if a['kind']=='buy' and a['token_id']=='E']
        sid=action['session_id'];request_id=action.get('request_id',action['action_id'])
        audit=deepcopy(store.lp_actions(sid));assert len(audit)==1 and audit[0]['state']=='unknown' and audit[0]['post_started'] is True and not audit[0].get('order_id')
        intent=deepcopy(document()['intents'][request_id]);assert intent['state']=='unknown' and not intent.get('order_id') and intent['settled'] is False and Decimal(intent['reserved_usd'])==Decimal('7.80')
        return dict(action=action,sid=sid,request_id=request_id,audit=audit,intent=intent)
    def preserve_unknown(value):
        intent=document()['intents'][value['request_id']]
        assert intent['state']=='unknown' and intent['settled'] is False and not intent.get('order_id')
        assert intent['session_id']==value['sid'] and Decimal(intent['reserved_usd'])==Decimal('7.80')
        assert store.lp_actions(value['sid'])==value['audit'] and not store.lp_session(value['sid']).get('entry_order_id')
    def coverage(value,state):
        preserve_unknown(value)
        marker=document()['intents'][value['request_id']]['reservation_coverage'];facts=document()['account_financial_facts']
        assert marker['state']=='covered' and marker['session_id']==value['sid'] and marker['intent_id']==value['request_id']
        assert marker['account_id']==facts['account_id']=='test-wallet' and marker['snapshot_id']==facts['snapshot_id']
        assert state['slots']['occupied']==4 and Decimal(state['funds']['buy_reserved_usd'])==Decimal('31.20')
        assert any(x.get('packet',{}).get('authenticated') is True and all(x['packet'].get(k) is True
            for k in ('balance_complete','open_orders_complete','pagination_complete','positions_complete','trades_complete'))
            and {o['token_id'] for o in x['packet']['open_orders']}=={'B','C','F','G'} for x in evidence['full_reads'] if x['phase']==phase[0])
    def save(filename):
        evidence.update(phase=phase[0],clock=pool.NOW,state=engine.lp_auto_state(),document=document(),sessions=store.lp_sessions(),
            audits=original_audits(),orders=exchange.orders,positions=exchange.positions,trades=exchange.trades,posts=exchange.posts,cancels=exchange.cancels)
        (tmp_path/filename).write_text(json.dumps(evidence,default=str,indent=2)+'\n')
    exchange.cancel_terminal=False;scheduler=LPAutoScheduler(engine,clock=lambda:pool.NOW)
    handle=None
    if window!='registered':
        handle=engine._lock_path.open('a+');fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        try:assert scheduler.run_due()
        finally:
            if handle:fcntl.flock(handle.fileno(),fcntl.LOCK_UN);handle.close();handle=None
        first=capture('original_registration_or_native_refusal_closed');interval(first,True)
        original=deepcopy(first['last_round']);audits=original_audits()
        assert exchange.posts==[] and first['slots']['occupied']==5 and Decimal(first['funds']['buy_reserved_usd'])==39
        assert len(exchange.cancels)==(3 if window=='registered' else 0)
        for order in exchange.orders:
            if order['token_id'] in {'A','D','I'}:
                order.update(status='LIVE' if window=='live_partial_before_registration' and order['token_id']=='A' else 'CANCELED',
                    size_matched=fill if order['token_id']=='A' else '0')
        if fill=='8':
            exchange.positions=[dict(token_id='A',condition_id='A',size='8',average_price='.39')]
            exchange.trades=[dict(trade_id='actual-original-A',status='CONFIRMED',trader_side='MAKER',taker_order_id='external-taker',
                token_id='A',condition_id='A',side='BUY',size='8',price='.39',fee='0',timestamp=pool.NOW,
                maker_orders=[dict(order_id='original-A',token_id='A',side='BUY',price='.39',matched_amount='8',fee='0',maker_address='test-wallet')])]
        full_mode[0]=narrow_mode[0]='incomplete';phase[0]='both_required_current_proofs_incomplete'
        at_deadline(monkeypatch,engine)
        assert scheduler.run_due();failed=capture('invalid_current_cannot_release_capacity');interval(failed)
        assert failed['last_round']['reason']=='account_snapshot_incomplete' and failed['last_round']['actions']==[]
        assert exchange.posts==[] and Decimal(failed['funds']['buy_reserved_usd'])==39 and failed['slots']['occupied']==5
        assert failed['funds']['as_of']==first['funds']['as_of'];immutable(audits)
        assert evidence['full_reads'][-1]['packet']['open_orders_complete'] is False
        if window=='registered':assert evidence['narrow_reads'][-1]['packet']['open_orders_complete'] is False
        quiet(failed,29)
        full_mode[0]=narrow_mode[0]='normal';phase[0]='next_valid_actual_current_absence_or_partial'
        at_deadline(monkeypatch,engine);prepare(engine._lp)
        assert scheduler.run_due();final=capture('new_current_replacements_with_owned_fill');interval(final,True)
        assert final['last_round']['round_id']!=original['round_id']
        expected=['E','F'] if window=='live_partial_before_registration' else ['E','F','G'];fixed_posts(expected)
        assert set(live_orders(exchange))==({'A','B','C','E','F'} if len(expected)==2 else {'B','C','E','F','G'})
        assert live_orders(exchange)['B']=='original-B' and live_orders(exchange)['C']=='original-C'
        assert Decimal(final['funds']['inventory_cost_usd'])==(Decimal('3.12') if fill=='8' else Decimal(0))
        assert Decimal(final['funds']['buy_reserved_usd'])==(Decimal('35.88') if len(expected)==2 else Decimal('39')) and final['slots']['occupied']==5
        assert {a['action_id'] for a in final['last_round']['actions']}.isdisjoint({a['action_id'] for a in original['actions']})
        immutable(audits)
        cancel_audits=[a for s in store.lp_sessions() for a in store.lp_actions(s['session_id']) if a.get('role')=='reconciliation_cancel']
        assert len(cancel_audits)==(3 if window=='registered' else 0)
        assert len(exchange.cancels)==(3 if window=='registered' else 0)
        if len(expected)==2:
            a=next(o for o in exchange.orders if o['order_id']=='original-A')
            assert a['status']=='LIVE' and Decimal(a['size_matched'])==8 and Decimal(a['original_size'])==20
        assert not any(x['active_plan'] for x in evidence['financial_reads']+evidence['market_reads'])
        evidence.update(completed='full',parameter=fill+'-'+window,original_round=original,original_audits=audits)
    finally:
        if handle:fcntl.flock(handle.fileno(),fcntl.LOCK_UN);handle.close()
        save('SC-absence-diagnostics.json')

@pytest.mark.parametrize('intervals', [(30, 60, 10), (90, 30, 5)], ids=['defaults', 'custom'])
def test_three_waits_use_independent_fixed_intervals(tmp_path, monkeypatch, intervals):
    from collections import Counter
    from copy import deepcopy

    engine, exchange, lp, _ = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    install_order_result_reader(exchange, monkeypatch)
    current_read = exchange.lp_account_snapshot_shared
    reads, failed_reads = [], []
    unavailable = [False]
    def result(**kwargs):
        reads.append(pool.NOW)
        if unavailable[0]:
            failed_reads.append(pool.NOW)
            raise TimeoutError('necessary current account read unavailable')
        return current_read(**kwargs)
    exchange.lp_account_snapshot_shared = result
    fields = ('round_interval_seconds', 'api_retry_interval_seconds', 'order_check_interval_seconds')
    if intervals != (30, 60, 10):
        engine.lp_auto_configure(dict(zip(fields, intervals)))
    assert tuple(engine.lp_auto_state()[key] for key in fields) == intervals

    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    first = deepcopy(engine.lp_auto_state()['last_round'])
    assert datetime.fromisoformat(first['completed_at']) > datetime.fromisoformat(first['started_at'])
    assert Counter(exchange.cancels) == Counter({'original-A': 1, 'original-D': 1, 'original-I': 1})
    assert exchange.posts == []

    def assert_wait(seconds):
        state = engine.lp_auto_state()
        assert state['plan_wait']['kind'] == 'round'
        completed = datetime.fromisoformat(state['last_round']['completed_at'])
        assert datetime.fromisoformat(state['plan_wait']['started_at']) == completed
        assert state['active_plan'] is None
        assert datetime.fromisoformat(state['plan_wait']['deadline']) - datetime.fromisoformat(state['plan_wait']['started_at']) == timedelta(seconds=seconds)
        before_reads, before_posts, before_cancels = list(reads), list(exchange.posts), list(exchange.cancels)
        at_deadline(monkeypatch, engine, before=1)
        scheduler.request_check()
        assert not scheduler.run_due()
        assert reads == before_reads and exchange.posts == before_posts and exchange.cancels == before_cancels
    assert_wait(intervals[0])
    unavailable[0] = True
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    assert failed_reads and engine.lp_auto_state()['last_round']['actions'] == []
    assert_wait(intervals[0])
    unavailable[0] = False
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    completed = datetime.fromisoformat(engine.lp_auto_state()['last_round']['completed_at'])
    successful = deepcopy(engine.lp_auto_state()['last_round'])
    assert Counter(a['token_id'] for a in successful['actions']) == Counter({'E': 1, 'F': 1, 'G': 1})
    assert all(a['kind'] == 'buy' and a['state'] == 'success' for a in successful['actions'])
    assert Counter(p['token_id'] for p in exchange.posts) == Counter({'E': 1, 'F': 1, 'G': 1})
    assert all(p['price'] == Decimal('.39') and p['quantity'] == 20 for p in exchange.posts)
    assert live_orders(exchange)['B'] == 'original-B' and live_orders(exchange)['C'] == 'original-C'
    assert len(live_orders(exchange)) == engine.lp_auto_state()['slots']['occupied'] == 5
    assert Counter(exchange.cancels) == Counter({'original-A': 1, 'original-D': 1, 'original-I': 1})
    accepted_audits = {a['session_id']: deepcopy(engine._store.lp_actions(a['session_id'])) for a in successful['actions']}
    assert datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']) == completed + timedelta(seconds=intervals[0])
    assert_wait(intervals[0])
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['last_round']['round_id'] != successful['round_id'] and state['active_plan'] is None
    assert Counter(p['token_id'] for p in exchange.posts) == Counter({'E': 1, 'F': 1, 'G': 1})
    assert Counter(exchange.cancels) == Counter({'original-A': 1, 'original-D': 1, 'original-I': 1})
    assert {sid: engine._store.lp_actions(sid) for sid in accepted_audits} == accepted_audits
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(state['last_round']['completed_at']) + timedelta(seconds=intervals[0])


def test_running_config_changes_only_future_waits(tmp_path, monkeypatch):
    from collections import Counter

    engine, exchange, _, _ = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    install_order_result_reader(exchange, monkeypatch)
    current_read = exchange.lp_account_snapshot_shared
    failed_reads = []
    unavailable = [False]
    def result(**kwargs):
        if unavailable[0]:
            failed_reads.append(pool.NOW)
            raise TimeoutError('necessary current account read unavailable')
        return current_read(**kwargs)
    exchange.lp_account_snapshot_shared = result
    exchange.cancel_terminal = False
    def prepare_current_candidates(service):
        # Existing external exchange seams feed normal public preparation.
        catalog_read = exchange.lp_reward_catalog
        def catalog(*, condition_ids=None, **kwargs):
            result = catalog_read(condition_ids=tuple(exchange.rewards) if condition_ids is None else condition_ids, **kwargs)
            return {**result, 'state': 'known', 'complete': True}
        def history(token_ids, *, start_ts, end_ts, **kwargs):
            return dict(state='known', history={token: [dict(t=start_ts, p='.390'),
                dict(t=end_ts, p='.391')] for token in token_ids})
        def competition(**kwargs):
            return dict(state='known', complete=True, checked_at=pool.NOW,
                round_checked_at=pool.NOW, competitiveness={token: (Decimal('2.5'), pool.NOW)
                    for token in exchange.rewards})
        exchange.lp_reward_catalog = catalog
        exchange.lp_market_metadata = exchange.lp_market_metadata_fresh
        exchange.lp_price_history = history
        exchange.lp_market_competitiveness = competition
        competition_state = service.refresh_competition_cache()
        assert competition_state['state'] == 'known' and competition_state['complete']
        prepared = service.refresh_price_history()
        assert prepared['preparation_outcome'] == 'success', prepared
        qualified = service.refresh_candidates(force=True)
        rows = qualified['candidates']
        assert qualified['candidate_valid_count'] == 7
        assert Counter(row['condition_id'] for row in rows) == Counter({'A': 1, 'D': 1, 'E': 1, 'F': 1, 'G': 1, 'H': 1, 'I': 1})
        for token in ('E', 'F', 'G'):
            row, = [row for row in rows if row['condition_id'] == token]
            assert row['selected_direction']['token_id'] == token
            assert row['selected_direction']['eligible'] is True

    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    before = engine.lp_auto_state()
    original_round = before['last_round']['round_id']
    original_observation = dict(before['last_round'])
    assert before['active_plan'] is None and original_observation['completed_at']
    state = engine.lp_auto_configure(dict(order_check_interval_seconds=15, api_retry_interval_seconds=30,
        round_interval_seconds=90, expected_config_version=before['config_version']))
    assert state['plan_wait'] == before['plan_wait']
    assert state['active_plan'] is None and state['last_round'] == original_observation
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
    assert datetime.fromisoformat(state['plan_wait']['deadline']) - datetime.fromisoformat(state['plan_wait']['started_at']) == timedelta(seconds=90)
    unavailable[0] = True
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    failed = engine.lp_auto_state()
    assert failed_reads and failed['last_round']['actions'] == [] and failed['active_plan'] is None
    before = failed['plan_wait']
    engine.lp_auto_configure(dict(api_retry_interval_seconds=45))
    assert engine.lp_auto_state()['plan_wait'] == before
    assert datetime.fromisoformat(before['deadline']) - datetime.fromisoformat(before['started_at']) == timedelta(seconds=90)
    unavailable[0] = False
    for order in exchange.orders:
        if order['token_id'] in {'A', 'D', 'I'}:
            order['status'] = 'CANCELED'
    at_deadline(monkeypatch, engine, before=1)
    scheduler.request_check()
    before_reads = list(exchange.reads)
    assert not scheduler.run_due()
    assert exchange.reads == before_reads and exchange.posts == []
    assert live_orders(exchange) == {'B': 'original-B', 'C': 'original-C'}
    original_requests = [(session['session_id'], session['idempotency_key']) for session in engine._store.lp_sessions()]
    prepare_current_candidates(engine._lp)
    at_deadline(monkeypatch, engine)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert Counter(p['token_id'] for p in exchange.posts) == Counter({'E': 1, 'F': 1, 'G': 1}), state['last_round']
    assert all(p['price'] == Decimal('.39') and p['quantity'] == 20 for p in exchange.posts)
    assert live_orders(exchange)['B'] == 'original-B' and live_orders(exchange)['C'] == 'original-C'
    current_requests = [(session['session_id'], session['idempotency_key']) for session in engine._store.lp_sessions()]
    assert all(request in current_requests for request in original_requests)
    assert len(current_requests) == len(original_requests) + 3
    assert state['last_round']['round_id'] != original_round
    assert state['last_round']['completed_at']
    assert state['api_retry_interval_seconds'] == 45
    assert datetime.fromisoformat(state['plan_wait']['deadline']) - datetime.fromisoformat(state['plan_wait']['started_at']) == timedelta(seconds=90)


@pytest.mark.parametrize('stage', ['cancel', 'api_failure', 'unknown_POST'])
def test_restart_and_duplicate_wakes_preserve_unresolved_request_identity(tmp_path, monkeypatch, stage):
    import json
    import sqlite3
    from copy import deepcopy
    from collections import Counter
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    monkeypatch.setattr(pool,'NOW',pool.NOW)
    initial=('B','C') if stage=='unknown_POST' else ('A','B','C','D','I')
    engine,exchange,lp,store=plan_setup(tmp_path,monkeypatch,initial)
    result_reader=install_order_result_reader(exchange,monkeypatch)
    phase,evidence=['original_local_cycle'],dict(states=[],full_reads=[],narrow_reads=[],financial_reads=[],market_reads=[],preparations=[],holds=[])
    full_mode,narrow_mode=['normal'],['normal']
    blockers=dict(full=None,narrow=None)
    def document():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state=deepcopy(engine.lp_auto_state())
        evidence['states'].append(dict(label=label,state=state,document=document(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=deepcopy(exchange.orders),trades=deepcopy(exchange.trades),positions=deepcopy(exchange.positions),
            posts=deepcopy(exchange.posts),cancels=list(exchange.cancels)))
        return state
    def known(rows):
        for row in rows:
            if (row.get('side')=='BUY' and row.get('token_id')==row.get('condition_id')
                    and row.get('status') in ('LIVE','CANCELED') and row.get('fill_quantity_known') in (None,True)
                    and Decimal(str(row['price']))==Decimal('.39') and Decimal(str(row['original_size']))==20
                    and Decimal(str(row['size_matched'])) in (Decimal(0),Decimal(8))):row['fill_quantity_known']=True
        return rows
    def hold(endpoint):
        value=blockers[endpoint]
        if value is not None:
            entered,release=value
            evidence['holds'].append(dict(endpoint=endpoint,phase=phase[0],started=pool.NOW))
            entered.set();assert release.wait(5),'Independent external current-read watchdog'
    for method in ('lp_account_snapshot','lp_market_metadata_fresh','lp_reward_catalog','lp_order_books'):
        original_read=getattr(exchange,method)
        def read(*args,_method=method,_read=original_read,**kwargs):
            target=evidence['financial_reads'] if _method=='lp_account_snapshot' else evidence['market_reads']
            target.append(dict(method=_method,phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),at=pool.NOW))
            return _read(*args,**kwargs)
        monkeypatch.setattr(exchange,method,read)
    full_reader=exchange.lp_account_snapshot_shared
    def full(*args,**kwargs):
        hold('full')
        record=dict(phase=phase[0],mode=full_mode[0],started=pool.NOW,active_plan=bool(engine.lp_auto_state()['active_plan']))
        evidence['full_reads'].append(record)
        if full_mode[0]=='timeout':
            pool.NOW+=timedelta(seconds=3)
            try:raise TimeoutError('required current account read unavailable')
            except TimeoutError as exc:
                record.update(ended=pool.NOW,error=dict(type=type(exc).__name__,message=str(exc)))
                raise
        packet=deepcopy(full_reader(*args,**kwargs))
        packet['open_orders']=known(deepcopy([o for o in exchange.orders if o['status']=='LIVE']))
        packet['positions']=deepcopy(exchange.positions);packet['raw_trades']=deepcopy(exchange.trades)
        if full_mode[0]=='incomplete':packet.update(open_orders_complete=False,pagination_complete=False)
        record.update(ended=pool.NOW,packet=deepcopy(packet));return packet
    monkeypatch.setattr(exchange,'lp_account_snapshot_shared',full)
    def narrow(request):
        hold('narrow')
        record=dict(phase=phase[0],mode=narrow_mode[0],request=deepcopy(request),started=pool.NOW)
        evidence['narrow_reads'].append(record)
        packet=result_reader(request)
        for field in ('orders','open_orders'):known(packet[field])
        if narrow_mode[0] in ('incomplete','incomplete_delay'):
            if narrow_mode[0]=='incomplete_delay':pool.NOW+=timedelta(seconds=3)
            packet.update(authenticated=False,open_orders_complete=False,pagination_complete=False,
                read_ended_at=pool.NOW,checked_at=pool.NOW)
        elif narrow_mode[0]=='generation':
            generation=store.lp_trade_generation();assert store.lp_advance_trade_generation(generation)
            record.update(generation_before=generation,generation_after=store.lp_trade_generation())
        record.update(ended=pool.NOW,packet=deepcopy(packet));return packet
    monkeypatch.setattr(exchange,'lp_order_result_snapshot',narrow)
    catalog_read=exchange.lp_reward_catalog
    def prepare(service):
        def catalog(*,condition_ids=None,**kwargs):
            value=catalog_read(condition_ids=tuple(exchange.rewards) if condition_ids is None else condition_ids,**kwargs)
            return {**value,'state':'known','complete':True}
        def history(token_ids,*,start_ts,end_ts,**kwargs):
            return dict(state='known',history={t:[dict(t=start_ts,p='.390'),dict(t=end_ts,p='.391')] for t in token_ids})
        def competition(**kwargs):
            return dict(state='known',complete=True,checked_at=pool.NOW,round_checked_at=pool.NOW,
                competitiveness={t:(Decimal('2.5'),pool.NOW) for t in exchange.rewards})
        monkeypatch.setattr(exchange,'lp_reward_catalog',catalog)
        monkeypatch.setattr(exchange,'lp_market_metadata',exchange.lp_market_metadata_fresh,raising=False)
        monkeypatch.setattr(exchange,'lp_price_history',history,raising=False)
        monkeypatch.setattr(exchange,'lp_market_competitiveness',competition,raising=False)
        competition_state=service.refresh_competition_cache();prepared=service.refresh_price_history();qualified=service.refresh_candidates(force=True)
        evidence['preparations'].append(deepcopy(dict(phase=phase[0],competition=competition_state,prepared=prepared,qualified=qualified)))
        assert competition_state['state']=='known' and prepared['preparation_outcome']=='success'
    def interval(state,dispatched=False):
        assert state['active_plan'] is None and state['plan_wait']['kind']=='round'
        start=state['last_round']['completed_at'] if dispatched else state['plan_wait']['started_at']
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(start)+timedelta(seconds=30)
    def original_audits():return {s['session_id']:deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    def immutable(original):
        for sid,rows in original.items():
            for row in rows:assert next(a for a in store.lp_actions(sid) if a['action_key']==row['action_key'])==row
    def counts():return tuple(len(evidence[k]) for k in ('full_reads','narrow_reads','financial_reads','market_reads'))
    def quiet(state,elapsed):
        previous=counts();posts=deepcopy(exchange.posts);cancels=list(exchange.cancels)
        latest=deepcopy(engine.lp_auto_state()['last_round']);wait=deepcopy(state['plan_wait'])
        pool.NOW=datetime.fromisoformat(wait['started_at'])+timedelta(seconds=elapsed)
        assert pool.NOW<datetime.fromisoformat(wait['deadline'])
        scheduler.request_check();scheduler.run_due();engine.lp_auto_run_once()
        assert counts()==previous and exchange.posts==posts and exchange.cancels==cancels
        assert engine.lp_auto_state()['last_round']==latest and engine.lp_auto_state()['plan_wait']==wait
    def fixed_posts(expected):
        assert Counter(p['token_id'] for p in exchange.posts)==Counter(expected)
        assert all(p['side']=='BUY' and Decimal(p['price'])==Decimal('.39') and Decimal(p['quantity'])==20 and p['post_only'] is True for p in exchange.posts)
    def unknown_request(first):
        action,=[a for a in first['last_round']['actions'] if a['kind']=='buy' and a['token_id']=='E']
        sid=action['session_id'];request_id=action.get('request_id',action['action_id'])
        audit=deepcopy(store.lp_actions(sid));assert len(audit)==1 and audit[0]['state']=='unknown' and audit[0]['post_started'] is True and not audit[0].get('order_id')
        intent=deepcopy(document()['intents'][request_id]);assert intent['state']=='unknown' and not intent.get('order_id') and intent['settled'] is False and Decimal(intent['reserved_usd'])==Decimal('7.80')
        return dict(action=action,sid=sid,request_id=request_id,audit=audit,intent=intent)
    def preserve_unknown(value):
        intent=document()['intents'][value['request_id']]
        assert intent['state']=='unknown' and intent['settled'] is False and not intent.get('order_id')
        assert intent['session_id']==value['sid'] and Decimal(intent['reserved_usd'])==Decimal('7.80')
        assert store.lp_actions(value['sid'])==value['audit'] and not store.lp_session(value['sid']).get('entry_order_id')
    def coverage(value,state):
        preserve_unknown(value)
        marker=document()['intents'][value['request_id']]['reservation_coverage'];facts=document()['account_financial_facts']
        assert marker['state']=='covered' and marker['session_id']==value['sid'] and marker['intent_id']==value['request_id']
        assert marker['account_id']==facts['account_id']=='test-wallet' and marker['snapshot_id']==facts['snapshot_id']
        assert state['slots']['occupied']==4 and Decimal(state['funds']['buy_reserved_usd'])==Decimal('31.20')
        assert any(x.get('packet',{}).get('authenticated') is True and all(x['packet'].get(k) is True
            for k in ('balance_complete','open_orders_complete','pagination_complete','positions_complete','trades_complete'))
            and {o['token_id'] for o in x['packet']['open_orders']}=={'B','C','F','G'} for x in evidence['full_reads'] if x['phase']==phase[0])
    def save(filename):
        evidence.update(phase=phase[0],clock=pool.NOW,state=engine.lp_auto_state(),document=document(),sessions=store.lp_sessions(),
            audits=original_audits(),orders=exchange.orders,positions=exchange.positions,trades=exchange.trades,posts=exchange.posts,cancels=exchange.cancels)
        (tmp_path/filename).write_text(json.dumps(evidence,default=str,indent=2)+'\n')
    exchange.cancel_terminal=False
    if stage=='unknown_POST':
        post=exchange.lp_post_order
        def unknown_post(signed):
            if signed['token_id']=='E':exchange.posts.append(signed);raise TimeoutError('original E POST receipt unknown')
            return post(signed)
        monkeypatch.setattr(exchange,'lp_post_order',unknown_post)
    scheduler=LPAutoScheduler(engine,clock=lambda:pool.NOW)
    entered,release=Event(),Event()
    try:
        assert scheduler.run_due();first=capture('original_closed_before_restart');interval(first,True)
        from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
        _wait_round_cancels(store,first)
        original=deepcopy(first['last_round']);audits=original_audits()
        value=unknown_request(first) if stage=='unknown_POST' else None
        if stage=='api_failure':
            phase[0]='current_api_failure_before_cold';full_mode[0]=narrow_mode[0]='incomplete'
            at_deadline(monkeypatch,engine);assert scheduler.run_due();first=capture('failed_current_before_cold');interval(first)
            assert first['last_round']['actions']==[] and exchange.posts==[]
        counts_before=len(exchange.posts),len(exchange.cancels)
        phase[0]='full_cold_durable_deadline'
        engine=restart_engine(engine,exchange);scheduler=LPAutoScheduler(engine,clock=lambda:pool.NOW)
        assert engine.lp_auto_state()['active_plan'] is None and engine.lp_auto_state()['plan_wait']==first['plan_wait']
        for _ in range(3):quiet(first,29);immutable(audits)
        assert (len(exchange.posts),len(exchange.cancels))==counts_before
        for order in exchange.orders:
            if order['token_id'] in {'A','D','I'}:order['status']='CANCELED'
        full_mode[0]=narrow_mode[0]='normal';phase[0]='held_new_current_after_cold'
        if value is not None:engine.lp_auto_set_desired_running(False)
        at_deadline(monkeypatch,engine)
        if value is None:prepare(engine._lp)
        endpoint='full';blockers[endpoint]=(entered,release)
        with ThreadPoolExecutor(1) as workers:
            running=workers.submit(scheduler.run_due)
            try:
                assert entered.wait(2),'Actual reached external current-read entry watchdog'
                scheduler.request_check();assert not scheduler.run_due()
                assert (len(exchange.posts),len(exchange.cancels))==counts_before
            finally:release.set()
            assert running.result(timeout=5)
        blockers[endpoint]=None
        final=capture('new_current_after_real_nonoverlap');interval(final)
        assert final['last_round']['round_id']!=original['round_id'];immutable(audits);fixed_posts(['E','F','G'])
        assert len(exchange.cancels)==counts_before[1]
        if value is not None:
            assert final['desired_running'] is False and final['last_round']['actions']==[] and final['last_round']['checked_at']
            assert set(live_orders(exchange))=={'B','C','F','G'};coverage(value,final)
        else:
            assert final['last_round']['completed_at'] and set(live_orders(exchange))=={'B','C','E','F','G'}
            assert final['slots']['occupied']==5 and Decimal(final['funds']['buy_reserved_usd'])==39
            assert {a['action_id'] for a in final['last_round']['actions']}.isdisjoint({a['action_id'] for a in original['actions']})
        assert not any(x['active_plan'] for x in evidence['financial_reads']+evidence['market_reads'])
        evidence.update(completed='full',parameter=stage,original_round=original,original_audits=audits,held_endpoint=endpoint)
    finally:release.set();save('SC-restart-diagnostics.json')

@pytest.mark.parametrize('resource', ['execution_lock', 'execution_lock_before_cancel', 'read_capacity', 'same_market'])
def test_transient_resource_contention_resumes_without_replacing_plan(tmp_path, monkeypatch, resource):
    from collections import Counter
    from copy import deepcopy
    from pathlib import Path
    from threading import Barrier, Lock, current_thread, get_ident
    from time import monotonic
    import json
    import sqlite3
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    from tests.test_lp_auto_target_convergence import publish_candidates, REWARDS

    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    result_reader = install_order_result_reader(exchange, monkeypatch)
    result_packets, reads, signatures, approvals, preview_calls = [], [], [], [], []
    held, previews, preview_results = [], [], []
    network_jobs, network_results = {}, {}
    blockers = ('Y', 'Z') if resource == 'read_capacity' else ('E',) if resource == 'same_market' else ()
    entered = {token: Event() for token in blockers}
    completed = {token: Event() for token in blockers}
    release = Event()
    armed, approved_phase, preview_phase = [False], [False], [False]
    caller, deadline = get_ident(), [None]
    old_timeout = lp._market_read_timeout
    original_ids = live_orders(exchange)
    original_orders = deepcopy(exchange.orders)
    signature_lock = Lock()
    workers = ThreadPoolExecutor(max_workers=2)

    def remaining():
        return max(0, deadline[0] - monotonic())

    def hold():
        assert not held
        handle = engine._lock_path.open('a+')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        held.append(handle)

    def unlock():
        for handle in held:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        held.clear()

    def results(request):
        packet = result_reader(request)
        result_packets.append(deepcopy(packet))
        return packet

    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
    for name in ('lp_account_snapshot', 'lp_account_snapshot_shared', 'lp_reward_catalog', 'lp_order_books'):
        read = getattr(exchange, name)
        def observed(*args, _read=read, _name=name, **kwargs):
            preview = preview_phase[0] and current_thread().name == 'lp-market-read'
            reads.append(dict(method=_name, thread=get_ident(), thread_name=current_thread().name,
                phase='preview' if preview else 'automatic_approved' if approved_phase[0] else 'current_planning'))
            if approved_phase[0] and not preview and _name in ('lp_account_snapshot', 'lp_account_snapshot_shared'):
                raise TimeoutError('Financial transport unavailable for approved automatic dispatch')
            return _read(*args, **kwargs)
        monkeypatch.setattr(exchange, name, observed)
    metadata = exchange.lp_market_metadata_fresh
    def blocking_metadata(ids, **kwargs):
        preview = preview_phase[0] and current_thread().name == 'lp-market-read'
        reads.append(dict(method='lp_market_metadata_fresh', ids=list(ids), thread=get_ident(),
            thread_name=current_thread().name,
            phase='preview' if preview else 'automatic_approved' if approved_phase[0] else 'current_planning'))
        for token in blockers:
            if token in ids and preview:
                try:
                    entered[token].set()
                    assert release.wait(remaining()), 'Independent actual preview SDK watchdog'
                    return metadata(ids, **kwargs)
                finally:
                    completed[token].set()
        return metadata(ids, **kwargs)
    monkeypatch.setattr(exchange, 'lp_market_metadata_fresh', blocking_metadata)

    def preview(token):
        preview_calls.append(dict(token=token, caller=get_ident(), phase='public_preview_after_approval'))
        return engine.lp_candidate_preview(dict(market_id=token, condition_id=token, token_id=token, outcome='YES'))

    connect = sqlite3.connect
    class AuthorityConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            result = super().execute(sql, parameters)
            if sql.strip().upper() == 'COMMIT' and armed[0] and get_ident() == caller:
                row = super().execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
                document = json.loads(row[0]) if row else {}
                plan = document.get('active_plan')
                if plan:
                    armed[0] = False
                    assert not self.in_transaction
                    state = engine.lp_auto_state()
                    assert state['active_plan']['round_id'] == plan['round_id']
                    assert [t['token_id'] for t in plan['targets']] == ['B', 'C', 'E', 'F', 'G']
                    assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in plan['targets'])
                    assert {a['token_id'] for a in plan['actions'] if a['kind'] == 'buy'} == {'E', 'F', 'G'}
                    assert all(a['state'] == 'pending' for a in plan['actions'])
                    assert exchange.posts == []
                    approvals.append(deepcopy(plan))
                    approved_phase[0] = True
                    if blockers:
                        preview_phase[0] = True
                        previews.extend(workers.submit(preview, token) for token in blockers)
                        for token in blockers:
                            assert entered[token].wait(remaining()), 'Actual public preview reaches SDK metadata'
                        assert not any(event.is_set() for event in completed.values())
                        with lp._market_reads_lock:
                            network_jobs.update({token: lp._market_reads[token] for token in blockers})
                        assert all(not job.done() for job in network_jobs.values())
                    return result
            return result
    def observed_connect(database, *args, **kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs.setdefault('factory', AuthorityConnection)
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', observed_connect)

    sign = exchange.lp_create_limit_order
    barrier = Barrier(3, action=hold)
    def signer(**kwargs):
        signed = sign(**kwargs)
        with signature_lock:
            signatures.append(deepcopy(signed))
        if resource == 'execution_lock' and approved_phase[0]:
            barrier.wait(timeout=remaining())
        return signed
    monkeypatch.setattr(exchange, 'lp_create_limit_order', signer)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)

    def closed():
        state = engine.lp_auto_state()
        last = deepcopy(state['last_round'])
        assert last['completed_at'] and state['active_plan'] is None
        completed_at = datetime.fromisoformat(last['completed_at'])
        assert state['plan_wait']['kind'] == 'round'
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed_at + timedelta(seconds=30)
        return state, last, completed_at

    def silent(completed_at):
        before = (len(reads), len(result_packets), deepcopy(exchange.posts), list(exchange.cancels))
        audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        for elapsed in (1, 29):
            monkeypatch.setattr(pool, 'NOW', completed_at + timedelta(seconds=elapsed))
            scheduler.request_check()
            assert not scheduler.run_due()
            engine.lp_auto_run_once()
            assert (len(reads), len(result_packets), exchange.posts, exchange.cancels) == before
            assert {sid: store.lp_actions(sid) for sid in audits} == audits

    def due(completed_at):
        approved_phase[0] = False
        monkeypatch.setattr(pool, 'NOW', completed_at + timedelta(seconds=30))
        publish_candidates(exchange, lp, store, REWARDS)
        return scheduler.run_due()

    try:
        if resource == 'execution_lock_before_cancel':
            hold()
        assert scheduler.run_due()
        state, original, completed_at = closed()
        assert [t['token_id'] for t in original['targets']] == ['B', 'C', 'E', 'F', 'G']
        assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in original['targets'])
        assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity'
            for a in original['actions'] if a['kind'] == 'buy')
        assert exchange.posts == []
        if resource == 'execution_lock_before_cancel':
            assert all(a['state'] == 'not_sent' and a['reason'] == 'execution_lock'
                for a in original['actions'] if a['kind'] == 'cancel')
            assert exchange.cancels == [] and exchange.orders == original_orders
            assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
            failed_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
            assert all(not a.get('rotation_round_id') for values in failed_audits.values() for a in values)
            silent(completed_at)
            unlock()
            assert due(completed_at)
            state, cancel_round, completed_at = closed()
            assert cancel_round['round_id'] != original['round_id']
            assert {a['action_id'] for a in cancel_round['actions']}.isdisjoint(a['action_id'] for a in original['actions'])
            assert {sid: store.lp_actions(sid)[:len(audits)] for sid, audits in failed_audits.items()} == failed_audits
        else:
            cancel_round = original
        assert Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'])
        assert live_orders(exchange) == {token: original_ids[token] for token in ('B', 'C')}
        assert sum(Decimal(str(o['price'])) * Decimal(str(o['original_size']))
            for o in exchange.orders if o['status'] == 'LIVE') == Decimal('15.60')
        old_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        cancel_audits = [a for values in old_audits.values() for a in values if a['role'] == 'reconciliation_cancel']
        assert len(cancel_audits) == 3 and all(a['state'] == 'accepted' for a in cancel_audits)
        assert {a['order_id'] for a in cancel_audits} == {'original-A', 'original-D', 'original-I'}
        silent(completed_at)
        deadline[0] = monotonic() + 5
        armed[0] = True
        assert due(completed_at)
        approved_phase[0] = False
        state, attempted, completed_at = closed()
        assert len(approvals) == 1 and attempted['round_id'] == approvals[0]['round_id']
        assert attempted['round_id'] != cancel_round['round_id']
        assert [a['action_id'] for a in attempted['actions']] == [a['action_id'] for a in approvals[0]['actions']]
        assert Counter(s['token_id'] for s in signatures) == Counter(['E', 'F', 'G'])
        assert all(Decimal(s['price']) == Decimal('.39') and Decimal(s['quantity']) == 20 and s['post_only'] is True for s in signatures)
        assert not any(r['phase'] == 'automatic_approved' for r in reads), reads
        assert {sid: store.lp_actions(sid) for sid in old_audits} == old_audits
        if resource == 'execution_lock':
            assert held and exchange.posts == []
            assert all(a['state'] in ('rejected', 'not_sent') for a in attempted['actions'])
            entry_audits = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id']) if a['role'] == 'entry']
            assert len(entry_audits) == 3 and all(a['state'] == 'rejected' and a['post_started'] is False
                and not a.get('order_id') and Decimal(a['price']) == Decimal('.39')
                and Decimal(a['quantity']) == 20 for a in entry_audits)
            ended_audits = {a['session_id']: deepcopy(store.lp_actions(a['session_id'])) for a in entry_audits}
            silent(completed_at)
            unlock()
            assert due(completed_at)
            state, successful, completed_at = closed()
            assert successful['round_id'] != attempted['round_id']
            assert {a['action_id'] for a in successful['actions']}.isdisjoint(a['action_id'] for a in attempted['actions'])
            assert {sid: store.lp_actions(sid) for sid in ended_audits} == ended_audits
            assert Counter(s['token_id'] for s in signatures) == Counter(['E', 'E', 'F', 'F', 'G', 'G'])
        else:
            successful = attempted
            if blockers:
                assert len(preview_calls) == len(blockers)
                assert len([r for r in reads if r['phase'] == 'preview' and r['method'] == 'lp_account_snapshot']) == len(blockers)
                assert not any(event.is_set() for event in completed.values())
        assert all(a['state'] == 'success' for a in successful['actions'])
        assert Counter(p['token_id'] for p in exchange.posts) == Counter(['E', 'F', 'G'])
        assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20
            and p['post_only'] is True and p['side'] == 'BUY' for p in exchange.posts)
        assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
        assert all(live_orders(exchange)[token] == original_ids[token] for token in ('B', 'C'))
        assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
        assert Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'])
        silent(completed_at)
        release.set()
        for pending in previews:
            preview_results.append(pending.result(timeout=remaining()))
        for token in blockers:
            assert completed[token].wait(remaining())
        for token, job in network_jobs.items():
            try:
                network_results[token] = dict(state='completed', result=job.result(timeout=remaining()))
            except (ValueError, KeyError) as exc:
                network_results[token] = dict(state='completed_error', error=type(exc).__name__, reason=str(exc))
        preview_phase[0] = False
        if blockers:
            with lp._market_reads_lock:
                assert not any(not job.done() for job in lp._market_reads.values())
        assert due(completed_at)
        final_state, final_round, _ = closed()
        assert final_round['round_id'] != successful['round_id']
        assert Counter(p['token_id'] for p in exchange.posts) == Counter(['E', 'F', 'G'])
        assert Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'])
        assert final_state['slots']['occupied'] == 5 and Decimal(final_state['funds']['buy_reserved_usd']) == 39
        assert {sid: store.lp_actions(sid) for sid in old_audits} == old_audits
    finally:
        approved_phase[0] = False
        release.set()
        unlock()
        barrier.abort()
        for pending in previews:
            if not pending.done():
                pending.result(timeout=5)
        workers.shutdown(wait=True)
        lp._market_read_timeout = old_timeout
        (tmp_path / 'RS-public-resource-evidence.json').write_text(json.dumps(dict(resource=resource,
            approvals=approvals, reads=reads, signatures=signatures, original_orders=original_orders,
            result_packets=result_packets, preview_calls=preview_calls, preview_results=preview_results,
            network_results=network_results, network_done={k: v.done() for k, v in network_jobs.items()},
            preview_entered={k: v.is_set() for k, v in entered.items()},
            preview_completed={k: v.is_set() for k, v in completed.items()}), default=str, indent=2) + '\n')




@pytest.mark.parametrize('window', ['precancel_generation', 'precancel_ttl', 'final_buy_generation'])
def test_approved_plan_continues_after_financial_change(tmp_path, monkeypatch, window):
    from collections import Counter
    from copy import deepcopy
    from threading import Event, Lock, get_ident
    from time import monotonic
    import traceback
    from pathlib import Path
    import json
    import sqlite3
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    from tests.test_lp_auto_target_convergence import REWARDS
    initial = ('A','B','C','D','I') if window.startswith('precancel') else ()
    engine, exchange, lp, store = plan_setup(tmp_path,monkeypatch,initial)
    result_reader = install_order_result_reader(exchange,monkeypatch)
    interval = 300 if initial else 1
    configured = engine.lp_auto_configure(dict(round_interval_seconds=interval,
        api_retry_interval_seconds=60,order_check_interval_seconds=1 if initial else 10))
    assert configured['round_interval_seconds'] == interval and configured['api_retry_interval_seconds'] == 60
    assert configured['order_check_interval_seconds'] == (1 if initial else 10)
    scheduler = LPAutoScheduler(engine,clock=lambda:pool.NOW)
    captured, forbidden, financial_reads, signatures, packets, mutations = [], [], [], [], [], []
    fixture_lock = Lock()
    signer_states, signer_errors = [], []
    signer_returned = {token: Event() for token in ('B','C','E','F','G')}
    diagnostic = dict(window=window, completed=None)
    for name in ('lp_account_snapshot','lp_account_snapshot_shared'):
        read = getattr(exchange,name)
        def observed(*args,_read=read,_name=name,**kwargs):
            active = engine.lp_auto_state()['active_plan'] is not None
            financial_reads.append(dict(method=_name,active=active,at=pool.NOW))
            if active:
                forbidden.append(_name)
                raise TimeoutError('approved execution must not reread balance/allowance')
            return _read(*args,**kwargs)
        monkeypatch.setattr(exchange,name,observed)
    def change():
        with fixture_lock:
            if captured:
                return
            state = deepcopy(engine.lp_auto_state())
            assert state['active_plan'] and state['active_plan']['actions']
            captured.append(state)
            before, generation = pool.NOW, store.lp_trade_generation()
            if window == 'precancel_ttl':
                monkeypatch.setattr(pool,'NOW',pool.NOW+timedelta(seconds=61))
                won = None
            else:
                won = store.lp_advance_trade_generation(generation)
            mutations.append(dict(before=before,after=pool.NOW,generation=generation,
                resulting_generation=store.lp_trade_generation(),cas_won=won))
            if window != 'precancel_ttl':
                assert won, 'Report a lost selected native generation CAS before substituting a stronger barrier'
                assert mutations[-1]['resulting_generation'] > generation
    armed, native_commits, caller = [window == 'precancel_generation'], [], get_ident()
    connect = sqlite3.connect
    class ApprovalConnection(sqlite3.Connection):
        def execute(self,sql,parameters=(),/):
            result = super().execute(sql,parameters)
            if sql.strip().upper() == 'COMMIT' and armed[0] and get_ident() == caller:
                row = super().execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
                document = json.loads(row[0]) if row else {}
                plan = document.get('active_plan')
                if plan is not None:
                    armed[0] = False
                    assert not self.in_transaction
                    assert [t['token_id'] for t in plan['targets']] == ['B','C','E','F','G']
                    cancels = [a for a in plan['actions'] if a['kind'] == 'cancel']
                    dependent = [a for a in plan['actions'] if a['kind'] == 'buy']
                    assert {a['order_id'] for a in cancels} == {'original-A','original-D','original-I'}
                    assert all(a['state'] == 'pending' for a in cancels)
                    assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
                    assert super().execute('SELECT COUNT(*) FROM lp_actions').fetchone()[0] == 0
                    assert exchange.posts == exchange.cancels == signatures == []
                    native_commits.append(dict(round_id=plan['round_id'],thread=caller,in_transaction=False))
                    change()
            return result
    def observed_connect(database,*args,**kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs.setdefault('factory',ApprovalConnection)
        return connect(database,*args,**kwargs)
    if window == 'precancel_generation':
        monkeypatch.setattr(sqlite3,'connect',observed_connect)
    sign = exchange.lp_create_limit_order
    def final_buy(**kwargs):
        token = kwargs['token_id']
        try:
            signed = sign(**kwargs)
            signatures.append(deepcopy(signed))
            if not initial and token == 'B':
                before = deepcopy(engine.lp_auto_state())
                signer_states.append(dict(phase='B_real_signed_before_sibling_completion', state=before))
                plan = before['active_plan']
                assert plan and plan['round_id'] == before['last_round']['round_id']
                identities = {a['token_id']: a['action_id'] for a in plan['actions'] if a['kind'] == 'buy'}
                assert set(identities) == {'B','C','E','F','G'}
                until = monotonic() + 5
                while True:
                    observed = deepcopy(engine.lp_auto_state())
                    current = observed['active_plan']
                    assert current and current['round_id'] == plan['round_id'] == observed['last_round']['round_id']
                    actions = {a['token_id']: a for a in current['actions'] if a['kind'] == 'buy'}
                    assert {t:a['action_id'] for t,a in actions.items()} == identities
                    assert actions['B']['state'] == 'pending'
                    assert not any(p['token_id'] == 'B' for p in exchange.posts)
                    if all(actions[t]['state'] == 'success' and signer_returned[t].is_set() for t in ('C','E','F','G')):
                        assert Counter(p['token_id'] for p in exchange.posts) == Counter(['C','E','F','G'])
                        signer_states.append(dict(phase='original_CEFG_public_success_before_unchanged_B_CAS', state=observed))
                        break
                    if monotonic() >= until:
                        signer_states.append(dict(phase='original_CEFG_success_predicate_not_reached', state=observed))
                        raise AssertionError('Independent 5s public original-sibling completion watchdog')
                    signer_returned['B'].wait(.01)
                change()
            return signed
        except Exception as exc:
            if window == 'final_buy_generation':
                signer_errors.append(dict(token=token,type=type(exc).__name__,message=str(exc),
                    repr=repr(exc),traceback=traceback.format_exc()))
            raise
        finally:
            signer_returned[token].set()
    monkeypatch.setattr(exchange,'lp_create_limit_order',final_buy)
    def current_results(request):
        active = engine.lp_auto_state()['active_plan']
        if initial and active is not None and not captured:
            cancels = [a for a in active['actions'] if a['kind'] == 'cancel']
            dependent = [a for a in active['actions'] if a['kind'] == 'buy']
            assert {a['order_id'] for a in cancels} == {'original-A','original-D','original-I'}
            assert all(a['state'] == 'pending' for a in cancels)
            assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
            assert exchange.posts == exchange.cancels == []
            assert not any(a['role'] == 'reconciliation_cancel' for row in store.lp_sessions() for a in store.lp_actions(row['session_id']))
            change()
        packet = result_reader(request)
        packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange,'lp_order_result_snapshot',current_results)
    try:
        assert scheduler.run_due()
        first = engine.lp_auto_state()
        assert len(captured) == len(mutations) == 1 and forbidden == []
        if window == 'precancel_generation':
            assert len(native_commits) == 1 and native_commits[0]['in_transaction'] is False
        if window == 'precancel_ttl':
            assert mutations[0]['after'] == mutations[0]['before']+timedelta(seconds=61)
        approved = captured[0]
        original = deepcopy(first['last_round'])
        assert [t['condition_id'] for t in original['targets']] == ['B','C','E','F','G']
        assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in original['targets'])
        assert original['round_id'] == approved['active_plan']['round_id']
        assert original['targets'] == approved['last_round']['targets']
        assert [a['action_id'] for a in original['actions']] == [a['action_id'] for a in approved['active_plan']['actions']]
        assert original['completed_at'] and first['active_plan'] is None and first['plan_wait']['kind'] == 'round'
        assert first['funds']['as_of'] == approved['funds']['as_of']
        completed = datetime.fromisoformat(original['completed_at'])
        deadline = datetime.fromisoformat(first['plan_wait']['deadline'])
        assert deadline == completed+timedelta(seconds=interval)
        old_audits = {row['session_id']:deepcopy(store.lp_actions(row['session_id'])) for row in store.lp_sessions()}
        if initial:
            assert exchange.posts == [] and Counter(exchange.cancels) == Counter(['original-A','original-D','original-I'])
            assert live_orders(exchange) == {'B':'original-B','C':'original-C'}
            assert sum(Decimal(o['price'])*(Decimal(o['original_size'])-Decimal(o['size_matched']))
                for o in exchange.orders if o['status'] == 'LIVE') == Decimal('15.60')
            dependent = [a for a in original['actions'] if a['kind'] == 'buy']
            assert {a['token_id'] for a in dependent} == {'E','F','G'}
            assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
            for action in original['actions']:
                if action['kind'] == 'cancel':
                    audit, = [a for a in old_audits[action['session_id']] if a['role'] == 'reconciliation_cancel']
                    assert audit['state'] == 'accepted' and audit['post_started'] is True and audit['order_id'] == action['order_id']
            assert packets and packets[0]['read_started_at'] >= mutations[0]['after']
            if window == 'precancel_generation':
                assert all(packets[0][k] is True for k in ('authenticated','open_orders_complete',
                    'positions_complete','trades_complete','pagination_complete'))
                assert packets[0]['wallet_address'] == 'test-wallet'
                assert {o['order_id'] for o in packets[0]['orders']} == {'original-A','original-B','original-C','original-D','original-I'}
                assert all(o['status'] == 'LIVE' and o['side'] == 'BUY' and Decimal(o['price']) == Decimal('.39')
                    and Decimal(o['original_size']) == 20 and Decimal(o['size_matched']) == 0 for o in packets[0]['orders'])
        else:
            assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B','C','E','F','G'])
            assert exchange.cancels == [] and all(a['state'] == 'success' for a in original['actions'])
        before = (len(financial_reads),len(packets),len(signatures),len(exchange.posts),len(exchange.cancels))
        monkeypatch.setattr(pool,'NOW',completed+timedelta(seconds=interval/2))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert (len(financial_reads),len(packets),len(signatures),len(exchange.posts),len(exchange.cancels)) == before
        assert {sid:store.lp_actions(sid) for sid in old_audits} == old_audits
        state = first
        if initial:
            monkeypatch.setattr(pool,'NOW',deadline)
            publish_candidates(exchange,lp,store,REWARDS)
            assert scheduler.run_due()
            state = engine.lp_auto_state()
            assert state['last_round']['round_id'] != original['round_id']
            new_buys = [a for a in state['last_round']['actions'] if a['kind'] == 'buy']
            assert {a['token_id'] for a in new_buys} == {'E','F','G'}
            assert {a['action_id'] for a in new_buys}.isdisjoint({a['action_id'] for a in original['actions']})
            assert all(a.get('session_id') and a['session_id'] not in old_audits and a['state'] == 'success' for a in new_buys)
            assert {sid:store.lp_actions(sid) for sid in old_audits} == old_audits
            assert live_orders(exchange)['B'] == 'original-B' and live_orders(exchange)['C'] == 'original-C'
        assert Counter(p['token_id'] for p in exchange.posts) == Counter(['E','F','G'] if initial else ['B','C','E','F','G'])
        assert Counter(exchange.cancels) == Counter(['original-A','original-D','original-I'] if initial else [])
        assert all(p['price'] == Decimal('.39') and p['quantity'] == 20 and p['post_only'] is True for p in exchange.posts)
        assert set(live_orders(exchange)) == {'B','C','E','F','G'}
        assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        for action in state['last_round']['actions']:
            if action['kind'] == 'buy' and action['state'] == 'success':
                audit, = store.lp_actions(action['session_id'])
                assert audit['state'] == 'accepted' and audit['post_started'] is True
                assert audit['token_id'] == action['token_id']
                assert Decimal(audit['price']) == Decimal('.39') and Decimal(audit['quantity']) == 20
        assert forbidden == [] and not any(r['active'] for r in financial_reads)
        final_completed = datetime.fromisoformat(state['last_round']['completed_at'])
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == final_completed+timedelta(seconds=interval)
        final_audits = {row['session_id']:deepcopy(store.lp_actions(row['session_id'])) for row in store.lp_sessions()}
        before = (len(financial_reads),len(packets),len(signatures),len(exchange.posts),len(exchange.cancels))
        monkeypatch.setattr(pool,'NOW',final_completed+timedelta(seconds=interval/2))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert (len(financial_reads),len(packets),len(signatures),len(exchange.posts),len(exchange.cancels)) == before
        assert {sid:store.lp_actions(sid) for sid in final_audits} == final_audits
        if window == 'final_buy_generation':
            diagnostic['completed'] = 'full'
    finally:
        if window == 'final_buy_generation':
            diagnostic.update(captured=captured,mutations=mutations,public_signer_states=signer_states,
                signer_exceptions=signer_errors,signer_returned={t:e.is_set() for t,e in signer_returned.items()},
                signatures=signatures,packets=packets,financial_reads=financial_reads,forbidden=forbidden,
                state=engine.lp_auto_state(),sessions=store.lp_sessions(),
                audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
                posts=deepcopy(exchange.posts),orders=deepcopy(exchange.orders),clock=pool.NOW)
            (tmp_path/'final-C3-diagnostics.json').write_text(json.dumps(diagnostic,indent=2,default=str)+'\n')
