"""LPYO public observation contracts; offline values and external source ports."""
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import time
from threading import Event, Thread
from types import MappingProxyType

import pytest

from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
from open_trader.polymarket_lp_market_calculation import CalculationPolicy, calculate_current, select_current_composition
from open_trader.polymarket_lp_market_reader import CapturedSources, MarketReader, ReadScope, SourceResult, freeze
from open_trader.polymarket_lp_risk import minimum_order_estimate

NOW = datetime(2026, 10, 19, tzinfo=UTC)
POLICY = CalculationPolicy(Decimal('40'), 1)


def market():
    return dict(metadata=dict(condition_id='c1', reward_min_size='1', reward_max_spread='.10'),
        rewards=dict(complete=True, checked_at=NOW, markets=[dict(condition_id='c1', daily_pool_usd='24')]),
        book=dict(condition_id='c1', token_id='t1', checked_at=NOW, received_at=NOW,
            bids=[dict(price='.40', size='100')], asks=[dict(price='.42', size='100')]))


def candidate(cid='c2', token='t2', pool='48'):
    return dict(condition_id=cid, token_id=token, price='.40', quantity='20',
        direction=dict(market=dict(reward_min_size=Decimal('1'), reward_max_spread=Decimal('.10')),
            book=market()['book'], daily_pool_usd=pool), evaluation_at=NOW)


def frame(value=None, *, error=None, complete=True, include=True, buys=True):
    row=dict(account_id='account-A', order_id='o1', condition_id='c1', token_id='t1',
        side='BUY', status='LIVE', price='.40', original_size='20', size_matched='0', created_at=NOW)
    sources=dict(balance=SourceResult('balance', ('100','100'), checked_at=NOW),
        open_orders=SourceResult('orders', [row] if buys else [], checked_at=NOW),
        positions=SourceResult('positions', [], checked_at=NOW))
    if include:
        sources['market:c1:t1']=SourceResult('market', market() if value is None else value,
            complete=complete, error=error, checked_at=NOW)
    return MarketReader.from_captured_sources(CapturedSources('account-A', sources))


def prepared(raw=None, candidates=None):
    return prepare_current_inputs(raw or frame(),
        candidates or SourceResult('candidates', [candidate()], checked_at=NOW), POLICY, NOW)


@pytest.mark.parametrize('case', ['computed','unknown','no-decision'])
def test_observability_preserves_decisions_fixed_plan_and_native_calls(case, monkeypatch):
    import socket, sqlite3
    def forbidden(*a, **kw):
        pytest.fail('value observation acquired external facts')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(sqlite3,'connect',forbidden)
    raw=frame(include=case!='unknown')
    if case=='no-decision':
        raw=replace(raw,sources=MappingProxyType({**raw.sources,'balance':SourceResult('balance',complete=False)}))
    result=calculate_current(prepared(raw),POLICY,NOW)
    if case=='computed':
        assert result.yields['o1'].quantize(Decimal('.000000000001'))==Decimal('.892857142857')
        assert result.candidate_yields['t2']==Decimal('1.5625')
        expected=(('c2',),('t2',),(),('o1',),(('c2','t2'),))
    elif case=='unknown':
        assert result.yields['o1'] is None
        expected=(('c1',),('o1',),('o1',),(),())
    else:
        assert result.selection_state=='NO_DECISION'
        expected=((),(),(),(),())
    actual=(result.targets,result.target_tokens,result.retained_order_ids,result.victim_order_ids,result.new_target_keys)
    assert actual==expected
    again=select_current_composition(prepared(raw),POLICY,result)
    assert (again.targets,again.target_tokens,again.retained_order_ids,again.victim_order_ids,again.new_target_keys)==expected


DROP_CASES = [
    ('source-missing','market_source_missing'), ('source-failed','market_source_failed'),
    ('source-incomplete','market_source_incomplete'), ('source-payload','market_payload_invalid'),
    ('metadata','market_metadata_missing'), ('rewards','market_rewards_missing'),
    ('book','market_book_missing'), ('metadata-condition','metadata_condition_mismatch'),
    ('book-condition','book_condition_mismatch'), ('book-token','book_token_mismatch'),
    ('catalog-incomplete','reward_catalog_incomplete'), ('reward-none','reward_row_missing'),
    ('reward-many','reward_rows_multiple')]


@pytest.mark.parametrize('case,code', DROP_CASES, ids=[c for c,_ in DROP_CASES])
def test_direction_drop_has_exact_order_reason(case, code):
    value=market()
    if case in ('metadata','rewards','book'): value.pop(case)
    if case=='metadata-condition': value['metadata']['condition_id']='other'
    if case=='book-condition': value['book']['condition_id']='other'
    if case=='book-token': value['book']['token_id']='other'
    if case=='catalog-incomplete': value['rewards']['complete']=False
    if case=='reward-none': value['rewards']['markets']=[]
    if case=='reward-many': value['rewards']['markets']*=2
    if case=='source-payload': value='invalid'
    raw=frame(value,include=case!='source-missing',complete=case not in ('source-failed','source-incomplete'),
        error='capacity' if case=='source-failed' else None)
    adapted=prepared(raw)
    assert 'o1' not in adapted.sources['directions'].value
    assert calculate_current(adapted,POLICY,NOW).yields['o1'] is None
    detail=adapted.sources['direction_failures'].value['o1']
    assert detail['reason_codes']==(code,)
    assert detail['order_id']=='o1' and detail['condition_id']=='c1' and detail['token_id']=='t1'
    if case=='source-failed': assert detail['source_error']=='capacity'
    for key,source in raw.sources.items(): assert adapted.sources[key] is source


@pytest.mark.parametrize('case',['capacity','timeout','in-progress','closed','http-error'])
def test_reader_error_is_attributed_without_extra_reads(case):
    from urllib.error import HTTPError
    release=Event()
    entered={key:Event() for key in ('a','b')}
    calls=[]
    class Port:
        def read(self,name,scope):
            calls.append(name)
            if case=='http-error': raise HTTPError('https://offline.invalid',429,'SECRET',{},None)
            entered[name].set()
            assert release.wait(3), 'native test watchdog'
            return ()
    reader=MarketReader(Port())
    try:
        if case=='closed':
            assert reader.close(timeout=0)
            source=reader.read_current(ReadScope('A',('a',)),time.monotonic()+1).sources['a']
            expected='closed'
            assert calls==[]
        elif case=='http-error':
            source=reader.read_current(ReadScope('A',('a',)),time.monotonic()+1).sources['a']
            expected='HTTPError'
            assert calls==['a'] and source.error_facts['status']==429
        else:
            first=reader.read_current(ReadScope('A',('a','b') if case=='capacity' else ('a',)),time.monotonic()+.02)
            assert entered['a'].wait(1)
            if case=='timeout': source=first.sources['a'];expected='timeout'
            else:
                name='c' if case=='capacity' else 'a'
                source=reader.read_current(ReadScope('A',(name,)),time.monotonic()+1).sources[name]
                expected='capacity' if case=='capacity' else 'in_progress'
            assert Counter(calls)==Counter(['a','b'] if case=='capacity' else ['a'])
        assert source.error==expected and source.complete is False
        assert source.diagnostics['source_error']==expected
        assert source.diagnostics['error_class']==('exception' if case=='http-error' else expected)
        if case=='http-error': assert source.diagnostics['http_status']==429
        assert 'SECRET' not in json.dumps(dict(source.diagnostics))
    finally:
        release.set()
        assert reader.close(timeout=2)


@pytest.mark.parametrize('seconds',[60,120],ids=['default-60','retry-after-120'])
def test_retry_cache_reports_actual_hit_and_remaining(seconds):
    from urllib.error import HTTPError
    clock=[10.0];calls=[]
    class Port:
        def read(self,name,scope):
            calls.append(name)
            if seconds==120: raise HTTPError('https://offline.invalid',429,'SECRET',{'Retry-After':'120'},None)
            raise ValueError('SECRET')
    reader=MarketReader(Port(),clock=lambda:clock[0]);scope=ReadScope('A',('balance',))
    try:
        first=reader.read_current(scope,time.monotonic()+1).sources['balance']
        assert reader.drain(timeout=2)
        assert first.diagnostics['retry_cache_hit'] is False
        clock[0]=25
        second=reader.read_current(scope,time.monotonic()+1).sources['balance']
        assert second.diagnostics['retry_cache_hit'] is True
        assert second.diagnostics['remaining_retry_seconds']==seconds-15
        assert second.source_id==first.source_id and second.failed_at==first.failed_at
        assert second.retry_at==first.retry_at==10+seconds and calls==['balance']
        clock[0]=10+seconds
        third=reader.read_current(scope,time.monotonic()+1).sources['balance']
        assert third.diagnostics['retry_cache_hit'] is False and calls==['balance','balance']
    finally: assert reader.close(timeout=2)


def test_inflight_age_survives_timeout_and_reports_global_slots():
    clock=[10.0]
    entered={k:Event() for k in ('a','b')};release={k:Event() for k in ('a','b')}
    calls=[]
    class Port:
        def read(self,name,scope):
            calls.append(name);entered[name].set()
            assert release[name].wait(3)
            return ()
    reader=MarketReader(Port(),clock=lambda:clock[0])
    try:
        original=reader.read_current(ReadScope('A',('a','b')),time.monotonic()+.02)
        assert all(e.wait(1) for e in entered.values())
        clock[0]=35
        same=reader.read_current(ReadScope('A',('a',)),time.monotonic()+1)
        foreign=reader.read_current(ReadScope('A',('c',)),time.monotonic()+1)
        assert same.sources['a'].error=='in_progress' and foreign.sources['c'].error=='capacity'
        assert same.sources['a'].diagnostics['inflight_age_seconds']==25
        for current in (same,foreign):
            assert current.reader_diagnostics['inflight_count']==2
            assert {tuple(v['key']):v['age_seconds'] for v in current.reader_diagnostics['jobs']}=={('A','a'):25,('A','b'):25}
        release['a'].set()
        assert not reader.drain(timeout=.1)
        current=reader.read_current(ReadScope('A',('b',)),time.monotonic()+1)
        assert current.reader_diagnostics['inflight_count']==1
        assert Counter(calls)==Counter(['a','b'])
        assert original.reader_diagnostics['inflight_count']==2
    finally:
        for e in release.values(): e.set()
        assert reader.close(timeout=2)


ESTIMATOR_CASES=[('estimate-time','estimate_time_unknown'),('book-missing','book_unknown'),
    ('book-levels','book_unknown'),('rules','market_rules_invalid'),('pool','reward_pool_unknown'),
    ('entry-terms','entry_terms_invalid'),('midpoint','midpoint_unknown'),
    ('midpoint-range','midpoint_out_of_range'),('candidate-distance','reward_distance_invalid'),
    ('candidate-score-zero','reward_score_zero')]


@pytest.mark.parametrize('case,code',ESTIMATOR_CASES,ids=[c for c,_ in ESTIMATOR_CASES])
def test_estimator_reasons_survive_calculation(case,code):
    base=prepared(candidates=SourceResult('candidates',[]))
    direction=dict(candidate()['direction']);direction['market']=dict(direction['market'])
    direction['book']=market()['book']; at=NOW
    row=dict(base.orders[0])
    if case=='estimate-time': at=NOW.replace(tzinfo=None)
    if case=='book-missing': direction['book']=None
    if case=='book-levels': direction['book']={'bids':'invalid','asks':[]}
    if case=='rules': direction['market']['reward_min_size']=None
    if case=='pool': direction['daily_pool_usd']=None
    if case=='entry-terms': row['price']=Decimal('-.4')
    if case=='midpoint': direction['book']={'bids':[],'asks':[]}
    if case=='midpoint-range':
        direction['book']={'bids':[dict(price='.04',size='100')],'asks':[dict(price='.06',size='100')]}
        row['price']=Decimal('.04')
    is_candidate=case.startswith('candidate-')
    if is_candidate:
        direction['market']['reward_max_spread']=Decimal('.03')
        if case=='candidate-distance':
            direction['book']={'bids':[dict(price='.50',size='100')],'asks':[dict(price='.52',size='100')]}
        if case=='candidate-score-zero':
            direction['book']={'bids':[dict(price='.48',size='100')],'asks':[dict(price='.54',size='100')]}
            row['price']=Decimal('.48')
    expected=minimum_order_estimate(direction,dict(price=row['price'],quantity=row['remaining']),at,
        resting_quantity=0 if is_candidate else row['remaining'])
    assert expected['state']=='unknown' and expected['reason_codes']==[code]
    sources=dict(base.sources,directions=SourceResult('direction',{'o1':direction}))
    if is_candidate:
        sources['candidates']=SourceResult('candidates',[dict(candidate(),price=row['price'],direction=direction,evaluation_at=at)])
        sources['directions']=SourceResult('direction',{})
    result=calculate_current(replace(base,orders=(freeze(row),),sources=MappingProxyType(sources)),POLICY,at)
    if is_candidate:
        detail=result.candidate_observations[0]
        assert result.candidate_yields['t2'] is None
    else:
        detail=result.incumbent_observations['o1']
        assert result.yields['o1'] is None
    assert detail['reason_codes']==(code,) and detail['state']=='unknown'


@pytest.mark.parametrize('case,exception',[('value-error','ValueError'),('type-error','TypeError'),('key-error','KeyError')],ids=['value-error','type-error','key-error'])
def test_calculator_retains_caught_exception_type(case,exception):
    base=prepared(candidates=SourceResult('candidates',[]))
    direction=dict(candidate()['direction']);row=dict(base.orders[0])
    if case=='value-error': row['remaining']=True
    if case=='type-error': direction['market']=['nonempty']
    if case=='key-error': direction.pop('book')
    with pytest.raises((ValueError,TypeError,KeyError)) as actual:
        minimum_order_estimate(direction,dict(price=row['price'],quantity=row['remaining']),NOW,resting_quantity=row['remaining'])
    assert type(actual.value).__name__==exception
    base=replace(base,orders=(freeze(row),),sources=MappingProxyType({**base.sources,'directions':SourceResult('direction',{'o1':direction})}))
    result=calculate_current(base,POLICY,NOW)
    assert result.yields['o1'] is None
    assert result.incumbent_observations['o1']['exception_type']==exception
    assert result.incumbent_observations['o1']['reason_codes']==('estimator_exception',)


@pytest.mark.parametrize('case',['before-placement','after-placement','no-times'])
def test_own_depth_unknown_reports_depth_and_snapshot_order_times(case):
    value=market();value['book']['bids']=[dict(price='.40',size='3'),dict(price='.40',size='4')]
    stamp=NOW-timedelta(seconds=1) if case=='before-placement' else NOW+timedelta(seconds=1)
    value['book'].update(checked_at=stamp,received_at=stamp)
    raw=frame(value);row=dict(raw.orders[0],remaining=Decimal('10'))
    if case=='no-times':
        value['book'].pop('checked_at');value['book'].pop('received_at');row.pop('created_at')
        raw=frame(value)
    raw=replace(raw,orders=(freeze(row),))
    result=calculate_current(prepared(raw),POLICY,NOW)
    detail=result.incumbent_observations['o1']
    assert result.yields['o1'] is None and detail['reason_codes']==('own_order_depth_unknown',)
    assert detail['public_depth_at_price']==Decimal('7') and detail['resting_quantity']==Decimal('10')
    assert detail['placed_at']==(None if case=='no-times' else NOW)
    assert detail['book_checked_at']==detail['book_received_at']==(None if case=='no-times' else stamp)
    value['book']['bids'].append(dict(price='.40',size='3'))
    enough=replace(frame(value),orders=(freeze(row),))
    assert calculate_current(prepared(enough),POLICY,NOW).yields['o1'] is not None


@pytest.mark.parametrize('case',['scoring','pool-zero','resting-nonscoring'])
def test_estimate_details_preserve_known_zero_and_literal_values(case):
    base=prepared(candidates=SourceResult('candidates',[]))
    d=candidate(pool='0' if case=='pool-zero' else '24')['direction']
    row=dict(base.orders[0])
    if case=='resting-nonscoring': row['price']=Decimal('.10')
    result=minimum_order_estimate(d,dict(price=row['price'],quantity=20),NOW,
        resting_quantity=Decimal('20'),include_observation=True)
    assert result['state']=='known'
    if case=='resting-nonscoring':
        assert result['basis']=='resting_non_scoring_order' and result['yield_pct_per_hour']==0
        assert result['estimated_share'] is None and result['competition_upper_bound'] is None
    else:
        assert result['competition_upper_bound']==Decimal('70.2')
        assert result['estimated_share']==Decimal(1)/14
        assert result['daily_pool_usd']==Decimal('0' if case=='pool-zero' else '24')
        assert result['capital_usd']==8
        assert result['yield_pct_per_hour'].quantize(Decimal('.000000000001'))==Decimal('0' if case=='pool-zero' else '.892857142857')
        fresh=minimum_order_estimate(d,dict(price='.40',quantity=20),NOW,include_observation=True)
        assert fresh['competition_upper_bound']==81 and fresh['estimated_share']==Decimal('.0625')
        assert fresh['yield_pct_per_hour']==Decimal('0' if case=='pool-zero' else '.78125')
        calculated=calculate_current(replace(base,sources=MappingProxyType({**base.sources,'directions':SourceResult('d',{'o1':d})})),POLICY,NOW)
        assert calculated.incumbent_observations['o1']['estimated_share']==Decimal(1)/14


@pytest.mark.parametrize('case',['balance-capacity','orders-timeout','positions-cache','candidates-incomplete','rejected-orders'])
def test_no_decision_records_failed_source_and_read_identity(tmp_path,monkeypatch,case):
    from tests.test_lp_d2_execution_bridge import rotation_runtime
    live,execution,sdk,ids,pools,order,qualify=rotation_runtime(tmp_path,monkeypatch,startup=False)
    original=live.reader._port;release=Event();calls=[]
    class Port:
        def read(self,name,scope):
            calls.append(name)
            if name.startswith('hold:') or case=='orders-timeout' and name=='open_orders':
                assert release.wait(3), 'source watchdog'
                return ()
            if case=='positions-cache' and name=='positions': raise ValueError('SECRET')
            if case=='rejected-orders' and name=='open_orders':
                return [dict(order_id='foreign',account_id='wrong',condition_id=ids['A'][0],token_id=ids['A'][1],side='BUY',status='LIVE',price='.50',original_size='20',size_matched='0')]
            return original.read(name,scope)
    live.reader._port=Port()
    try:
        if case=='balance-capacity':
            live.reader.read_current(ReadScope(live.account_id,('hold:a','hold:b')),time.monotonic()+.02)
        capture=SourceResult('captured',[],complete=case!='candidates-incomplete',error='qualification_unknown' if case=='candidates-incomplete' else None)
        if case=='positions-cache': live.run_once(capture,timeout=.05)
        result=live.run_once(capture,timeout=.05)
        before=tuple(calls)
        diagnostic=execution.lp_auto_state(include_intents=False)['diagnostics']
        observation=diagnostic['yield_observation']
        assert tuple(calls)==before and sdk.posts==sdk.cancels==[]
        expected={'balance-capacity':('balance','capacity','capacity'),
            'orders-timeout':('open_orders','timeout','timeout'),
            'positions-cache':('positions','ValueError','exception'),
            'candidates-incomplete':('candidates','qualification_unknown','exception'),
            'rejected-orders':('open_orders','invalid_order_identity_or_terms','input_invalid')}[case]
        row=next(r for r in observation['source_errors']['samples'] if r['source']==expected[0])
        assert (row['source'],row['source_error'],row['error_class'])==expected
        if case=='positions-cache': assert row['retry_cache_hit'] is True and row['remaining_retry_seconds']>0
        assert observation['read_ticket']['observation_id']==observation['identity']['observation_id']
        assert observation['read_ticket']['read_sequence']>0
        if result.acceptance.observation:
            assert observation['identity']['observation_id']==result.acceptance.observation.observation_id
        assert observation['selection_state']=='NO_DECISION'
        assert 'SECRET' not in json.dumps(diagnostic)
    finally:
        release.set()
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('case',['rank','tie','unknown-receipt','not-sent'])
def test_buy_attempt_diagnostics_keep_selection_estimate_and_full_rank(tmp_path,monkeypatch,case):
    from tests.test_lp_d2_execution_bridge import rotation_runtime
    live,execution,sdk,ids,pools,order,qualify=rotation_runtime(tmp_path,monkeypatch,target=1,budget='10000',wallet='10000')
    producer=execution._lp
    letters='ABCDEFGHIJKLMNOPQRSTUVWXYZ' if case=='rank' else 'ABC'
    for letter in letters: pools[letter]=Decimal('1')
    pools.update(A=Decimal('48' if case=='tie' else '24'),B=Decimal('48'),C=Decimal('12'))
    for letter in letters:
        qualify(letter,quantity='90')
        facts=producer._candidate_qualification_facts[ids[letter][0]]
        direction=facts['directions'][0]
        direction['market']['reward_max_spread']=Decimal('.03')
        direction['market']['reward_min_size']=Decimal('20')
        direction['book']['bids']=[dict(price=Decimal('.50'),size=Decimal('570')),dict(price=Decimal('.47'),size=Decimal('100'))]
        direction['book']['asks']=[dict(price=Decimal('.52'),size=Decimal('570'))]
    if case=='not-sent':
        def fail(**kw): raise ValueError('offline signing unavailable')
        sdk.create_limit_order=fail
    if case=='unknown-receipt':
        def unknown(signed): sdk.posts.append(signed);return dict(accepted=True,status='LIVE',taking_amount='0')
        sdk.post_order=unknown
    try:
        result=execution.lp_auto_scheduled_check()
        assert len(result.outcomes)==1, execution.lp_auto_state(include_intents=False)['diagnostics']['admission']
        detail=execution.lp_auto_state(include_intents=False)['diagnostics']['yield_observation']
        row,=detail['placement_estimates']['samples']
        chosen='A' if case=='tie' else 'B'
        assert row['condition_id']==ids[chosen][0] and row['token_id']==ids[chosen][1]
        assert row['rank']==1 and row['candidate_count']==row['computed_candidate_count']==len(letters)
        assert Decimal(row['estimated_share'])==Decimal('.05')
        assert Decimal(row['capital_usd'])==45 and Decimal(row['daily_pool_usd'])==48
        assert Decimal(row['competition_upper_bound']).quantize(Decimal('.000001'))==Decimal('253.333333')
        assert Decimal(row['yield_pct_per_hour']).quantize(Decimal('.000001'))==Decimal('.222222')
        assert row['request_id']==result.outcomes[0].identity.request_id
        assert row['observation_id']==result.acceptance.observation.observation_id
        assert row['estimate_source_id'] and row['checked_at']
        expected='not_sent' if case=='not-sent' else 'unknown_error' if case=='unknown-receipt' else 'accepted'
        assert row['disposition']==expected
        assert row['order_id']==('O1' if case in ('rank','tie') else None)
        assert row['placement_time_basis']==('accepted_receipt' if case in ('rank','tie') else 'unavailable')
        assert row['placed_at']==(live._utc_now().isoformat() if case in ('rank','tie') else None)
        assert len(sdk.posts)==(0 if case=='not-sent' else 1) and sdk.cancels==[]
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_yield_rounds_bound_lists_and_evict_distinct_observations(tmp_path,monkeypatch):
    from open_trader.polymarket_lp_diagnostics import YIELD_ROW_LIMIT,YIELD_ROUND_LIMIT
    from tests.test_lp_d2_execution_bridge import rotation_runtime
    live,execution,sdk,ids,pools,order,qualify=rotation_runtime(tmp_path,monkeypatch,budget='10000',wallet='10000')
    sdk.orders=tuple(order(k) for k in ids)
    observed=[]
    try:
        for index in range(21):
            sdk.advance(1)
            candidates=SourceResult('capture-'+str(index),[dict(candidate('new-'+str(i),'new-token-'+str(i)),market_id='market-'+str(i),evaluation_at=live._utc_now()) for i in range(26)])
            result=live.run_once(candidates)
            observed.append(result.acceptance.observation.observation_id)
        before=(sdk.order_reads,sdk.position_reads,tuple(sdk.posts),tuple(sdk.cancels))
        state=execution.lp_auto_state(include_intents=False)['diagnostics']
        history=state['yield_rounds']
        assert 1<=YIELD_ROUND_LIMIT<=20 and 1<=YIELD_ROW_LIMIT<=20
        assert [r['identity']['observation_id'] for r in history['recent']]==observed[-YIELD_ROUND_LIMIT:]
        assert history['total_observations']==22 and len(history['recent'])==YIELD_ROUND_LIMIT
        latest=history['recent'][-1]
        assert latest['candidate_count']==latest['computed_candidate_count']==26
        for name in ('candidates','incumbents','selected','victims','new_targets','source_errors','placement_estimates'):
            bucket=latest[name]
            assert len(bucket['samples'])<=YIELD_ROW_LIMIT
            assert bucket['total']==len(bucket['samples'])+bucket['omitted']
        assert latest['incumbents']['total']==26 and latest['candidates']['total']==26
        assert latest['incumbents']['samples'][0]['yield_pct_per_hour'] is not None
        assert isinstance(latest['evaluated_at'],str)
        assert json.loads(json.dumps(state,allow_nan=False))==state
        assert execution.lp_auto_state(include_intents=False)['diagnostics']['yield_rounds']==history
        latest['candidates']['samples'][0]['state']='mutated'
        assert execution.lp_auto_state(include_intents=False)['diagnostics']['yield_rounds']['recent'][-1]['candidates']['samples'][0]['state']=='known'
        assert (sdk.order_reads,sdk.position_reads,tuple(sdk.posts),tuple(sdk.cancels))==before
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_yield_observation_failure_does_not_kill_scheduler(tmp_path,monkeypatch):
    raw=frame()
    poisoned=replace(raw.sources['balance'],diagnostics={'unsupported':object()})
    captured=MarketReader.from_captured_sources(CapturedSources(raw.account,
        {**raw.sources,'balance':poisoned}))
    assert captured.buy_reserved==raw.buy_reserved and captured.orders==raw.orders
    from tests.test_lp_d2_execution_bridge import rotation_runtime
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from open_trader.polymarket_lp_diagnostics import yield_diagnostic_value
    live,execution,sdk,ids,pools,order,qualify=rotation_runtime(tmp_path,monkeypatch)
    scheduler=LPAutoScheduler(execution,clock=live._utc_now)
    before=execution.lp_auto_state(include_intents=False)
    old_instance=before['diagnostics']['yield_rounds']['instance_id']
    # Explicit stored-observation corruption, as in the existing resilience
    # contracts. No estimator, reader or qualification collaborator is mocked.
    live._yield_rounds=({'unsupported':object()},)
    live._diagnostics={**live._diagnostics,'yield_rounds':{'unsupported':object()}}
    completed=Event();failures=[]
    def run():
        try:
            assert scheduler.run_due()
            scheduler.request_check()
            assert scheduler.run_due()
        except BaseException as exc: failures.append(exc)
        finally: completed.set()
    worker=Thread(target=run)
    try:
        degraded=execution.lp_auto_state(include_intents=False)
        assert degraded['diagnostics']['available'] is False
        assert {k:v for k,v in degraded.items() if k!='diagnostics'}=={k:v for k,v in before.items() if k!='diagnostics'}
        assert json.loads(json.dumps(degraded,allow_nan=False))==degraded
        assert yield_diagnostic_value({'amount':Decimal('NaN'),'age':float('inf'),'stamp':NOW})==dict(amount=None,age=None,stamp=NOW.isoformat())
        worker.start()
        assert completed.wait(3), 'scheduler recovery watchdog'
        worker.join(timeout=1)
        assert not worker.is_alive() and failures==[]
        recovered=execution.lp_auto_state(include_intents=False)['diagnostics']
        assert recovered['yield_rounds']['instance_id']!=old_instance
        assert recovered['yield_rounds']['recent'][-1]['available'] is True
        assert scheduler.snapshot()['last_check_error'] is None
        assert sdk.posts==sdk.cancels==[]
    finally:
        if worker.is_alive(): worker.join(timeout=3)
        scheduler.stop()
        assert live.close(timeout=3)
        live.trading.close()


def test_live_unknown_reason_is_current_or_explicitly_attempted(tmp_path,monkeypatch):
    from tests.test_lp_d2_execution_bridge import rotation_runtime
    live,execution,sdk,ids,pools,order,qualify=rotation_runtime(tmp_path,monkeypatch)
    sdk.orders=(order('A'),)
    port=live.reader._port
    release=Event()
    entered={name:Event() for name in ('hold:a','hold:b')}
    class Port:
        def read(self,name,scope):
            if name in entered:
                entered[name].set()
                assert release.wait(3), 'held source watchdog'
                return ()
            value=port.read(name,scope)
            if name.startswith('market:'):
                value=dict(value)
                value['metadata']={**value['metadata'],'reward_min_size':'1'}
                value['book']={**value['book'],'bids':[dict(price='.50',size='7')]}
            return value
    live.reader._port=Port()
    try:
        result=execution.lp_auto_scheduled_check()
        diagnostic=execution.lp_auto_state(include_intents=False)['diagnostics']
        row,=diagnostic['unknown_buy_yields']['recent']
        assert row['reason_codes']==['own_order_depth_unknown'] and row['reason_is_current'] is True
        assert Decimal(row['public_depth_at_price'])==7 and Decimal(row['resting_quantity'])==20
        assert row['reason_observation_id']==result.acceptance.observation.observation_id
        assert row['age_seconds']==0 and row['due_at_600'] is False
        rotation=diagnostic['rotation']['incumbent_yields']['samples'][0]
        assert rotation['state']=='unknown' and rotation['observation']['reason_codes']==['own_order_depth_unknown']
        from open_trader.polymarket_lp_current_state import SourceStamp, ReceiptEvidence
        original=live.state.snapshot().orders[0]
        stamp=SourceStamp('newer-receipt',checked_at=live._utc_now()+timedelta(seconds=2),complete=True,state='known')
        newer=replace(original,source=stamp,**{name:replace(getattr(original,name),sources=(stamp,))
            for name in ('price','original_size','size_matched','remaining','order_type','expiration')})
        receipt=ReceiptEvidence('newer-receipt','fixture-request',live.account_id,live.process_epoch,
            live.state.snapshot().current.cycle_generation,original.key,'BUY',1,order_fact=newer)
        assert live.state.accept_receipt_evidence(receipt)=='applied'
        sdk.advance(1)
        sdk.orders=(order('A',original='19'),)
        execution.lp_auto_scheduled_check()
        row,=execution.lp_auto_state(include_intents=False)['diagnostics']['unknown_buy_yields']['recent']
        assert row['reason_is_current'] is False and row['reason_codes']==['reason_unavailable']
        assert row['attempted_reason_codes']==['own_order_depth_unknown']
        assert row['age_seconds']==1 and sdk.posts==sdk.cancels==[]
        live.reader.read_current(ReadScope(live.account_id,tuple(entered)),time.monotonic()+.02)
        assert all(event.wait(1) for event in entered.values())
        execution.lp_auto_scheduled_check()
        row,=execution.lp_auto_state(include_intents=False)['diagnostics']['unknown_buy_yields']['recent']
        assert row['reason_is_current'] is False
        assert any(r['source']=='open_orders' and r['source_error']=='capacity'
            for r in row['attempted_source_failures'])
        assert sdk.posts==sdk.cancels==[]
    finally:
        release.set()
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('current',[True,False],ids=['accepted-current','attempted-source'])
def test_yield_payload_budget_at_full_caps(current):
    from copy import deepcopy
    from open_trader.polymarket_lp_diagnostics import (YIELD_ROW_LIMIT,YIELD_ROUND_LIMIT,
        YIELD_STRING_LIMIT,yield_diagnostic_value,bounded_yield_rows)
    calculation=calculate_current(prepared(),POLICY,NOW)
    detail=dict(calculation.incumbent_observations['o1'])
    detail.update(state='unknown',reason_codes=['x'*YIELD_STRING_LIMIT]*8,
        exception_type='E'*64,source_error='E'*64,error_class='exception',
        public_depth_at_price=Decimal('1.2300'),resting_quantity=20,
        reason_observation_id='r',order_source_id='orders',accepted_input=True)
    candidate_row=dict(calculation.candidate_observations[0],rank=1,candidate_count=20,computed_candidate_count=20,
        request_id='request',batch_id='batch',order_id='order',estimate_source_id='source',observation_id='r',
        disposition='accepted',venue_finality='unavailable',placed_at=NOW,placement_time_basis='accepted_receipt',
        share_precision_decimal_places=24)
    labels={'account_id','condition_id','token_id','order_id','source_id','estimate_source_id',
        'request_id','batch_id','observation_id','reason_observation_id','order_source_id','source','market_id'}
    def maximize(value,key=None):
        if isinstance(value,dict): return {k:maximize(v,k) for k,v in value.items()}
        if isinstance(value,(list,tuple)): return [maximize(v,key) for v in value]
        if isinstance(value,str) and key in labels: return '界'*YIELD_STRING_LIMIT
        return value
    detail=yield_diagnostic_value(maximize(detail))
    candidate_row=yield_diagnostic_value(maximize(candidate_row))
    identity=yield_diagnostic_value(maximize(dict(account_id='A',generation='g',observation_id='r')))
    targets=bounded_yield_rows([maximize(dict(order_id='o',condition_id='c',token_id='t'))]*20)
    errors=bounded_yield_rows([maximize(dict(source='market:c:t',source_id='source',source_error='E'*64,
        condition_id='c',token_id='t',
        error_class='exception',retry_cache_hit=True,remaining_retry_seconds=60,inflight_age_seconds=25,http_status=429))]*20)
    observation=yield_diagnostic_value(dict(available=True,identity=identity,read_ticket=dict(identity,read_sequence=1,started_at=NOW),
        evaluated_at=NOW,acceptance='accepted',selection_state='DECISION',selection_reasons=['x'*128]*8,
        decision_states=dict(replacement='DECISION',over_target='DECISION',over_budget='DECISION'),
        candidate_count=20,computed_candidate_count=20,candidates=bounded_yield_rows([candidate_row]*20),
        incumbents=bounded_yield_rows([detail]*20),placement_estimates=bounded_yield_rows([candidate_row]*20),
        selected=targets,victims=targets,new_targets=targets,source_errors=errors,
        reader=dict(inflight_count=2,jobs=[dict(key=['A'*128,'m'*128],scope=dict(account='A'*128,component='m'*128,
            condition_id='c'*128,token_id='t'*128),age_seconds=25)]*2)))
    addition=dict(yield_observation=observation,
        yield_rounds=dict(instance_id='i'*128,round_limit=YIELD_ROUND_LIMIT,row_limit=YIELD_ROW_LIMIT,
            recent=[deepcopy(observation) for _ in range(YIELD_ROUND_LIMIT)]),
        unknown_buy_yields=dict(total=20,omitted=0,recent=[dict(deepcopy(detail),
            reason_is_current=current,attempted_reason_codes=detail['reason_codes'],
            reason_codes=detail['reason_codes'] if current else ['reason_unavailable'],
            **({} if current else dict(attempted_source_failures=yield_diagnostic_value([maximize(dict(source='s',source_error='E'*64,
                error_class='exception',retry_cache_hit=True,remaining_retry_seconds=60,inflight_age_seconds=25,http_status=429))]*2)))) for _ in range(20)]),
        rotation=dict(incumbent_yields=dict(total=20,omitted=0,samples=[dict(observation=deepcopy(detail)) for _ in range(20)]),
            source_errors=errors,read_ticket=observation['read_ticket']))
    rendered=json.dumps(addition,ensure_ascii=False,allow_nan=False).encode('utf-8')
    def maximum_strings(value):
        if isinstance(value,str): return 'x'*YIELD_STRING_LIMIT
        if isinstance(value,dict): return {k:maximum_strings(v) for k,v in value.items()}
        if isinstance(value,list): return [maximum_strings(v) for v in value]
        return value
    # Conservatively saturate every text value, including Decimal/time strings
    # and fixed codes. This is stricter than the runtime schema permits.
    saturated=json.dumps(maximum_strings(addition),ensure_ascii=False,allow_nan=False).encode('utf-8')
    print(f'LPYO_PAYLOAD_BYTES={len(rendered)} N={YIELD_ROUND_LIMIT} K={YIELD_ROW_LIMIT} STRING_BYTES={YIELD_STRING_LIMIT}')
    print(f'LPYO_SATURATED_PAYLOAD_BYTES={len(saturated)}')
    assert len(rendered)<=256*1024
    assert len(saturated)<=256*1024
    assert len(observation['incumbents']['samples'])==YIELD_ROW_LIMIT
    assert len(addition['yield_rounds']['recent'])==YIELD_ROUND_LIMIT
    assert observation['incumbents']['samples'][0]['public_depth_at_price']=='1.2300'


@pytest.mark.parametrize('case',['candidate','incumbent','pool-zero','non-scoring','own-depth','pool-unknown'])
def test_default_minimum_estimate_preserves_legacy_output(case):
    direction=candidate(pool='0' if case=='pool-zero' else '24')['direction']
    price=Decimal('.10') if case=='non-scoring' else Decimal('.40')
    resting=Decimal(0) if case=='candidate' else Decimal(20)
    if case=='own-depth': direction['book']['bids']=[dict(price='.40',size='7')]
    if case=='pool-unknown': direction['daily_pool_usd']=None
    result=minimum_order_estimate(direction,dict(price=price,quantity=20),NOW,resting_quantity=resting)
    if case=='pool-unknown':
        assert result==dict(state='unknown',reason_codes=['reward_pool_unknown'],
            **{key:None for key in ('yield_pct_per_hour','yield_pct_per_hour_display',
                'target_quantity','target_capital_usd','hourly_reward_usd','midpoint',
                'competition_upper_bound','checked_at')})
    elif case=='own-depth':
        assert result==dict(state='unknown',reason_codes=['own_order_depth_unknown'])
    else:
        assert set(result)=={'state','basis','quantity','price','capital_usd',
            'hourly_reward_usd','yield_pct_per_hour','checked_at'}
        assert result['state']=='known' and result['checked_at']==NOW
        assert result['price']==price and result['quantity']==20 and result['capital_usd']==price*20
        if case in ('pool-zero','non-scoring'):
            assert result['hourly_reward_usd']==result['yield_pct_per_hour']==0
        elif case=='candidate':
            assert result['hourly_reward_usd']==Decimal('.0625') and result['yield_pct_per_hour']==Decimal('.78125')
        else:
            assert result['yield_pct_per_hour'].quantize(Decimal('.000000000001'))==Decimal('.892857142857')


def test_rotation_observation_fault_preserves_legacy_summary_and_pending(tmp_path,monkeypatch):
    from tests.test_lp_d2_execution_bridge import rotation_runtime,wait_rotation_idle
    live,execution,sdk,ids,pools,order,qualify=rotation_runtime(tmp_path,monkeypatch)
    try:
        sdk.orders=tuple(order(k) for k in 'ABCDEF')
        result=execution.lp_auto_scheduled_check()
        assert result.rotation_outcomes and all(o.handle for o in result.rotation_outcomes)
        for outcome in result.rotation_outcomes: outcome.handle.result(timeout=3)
        wait_rotation_idle(live)
        pending=live._rotation_pending
        assert pending
        before=live.rotation_diagnostics()
        calls=(sdk.order_reads,sdk.position_reads,tuple(sdk.posts),tuple(sdk.cancels))
        observations={key:dict(value,unsupported=object()) for key,value in result.selection.incumbent_observations.items()}
        live._remember_rotation(replace(result,selection=replace(result.selection,incumbent_observations=observations)))
        assert live._rotation_pending==pending
        after=live.rotation_diagnostics()
        assert after['selection_state']==before['selection_state']
        assert after['identity']==before['identity'] and after['victims']==before['victims']
        assert after['outcomes']==before['outcomes']
        for old,new in zip(before['incumbent_yields']['samples'],after['incumbent_yields']['samples'],strict=True):
            assert {k:v for k,v in new.items() if k!='observation'}=={k:v for k,v in old.items() if k!='observation'}
            assert new['observation'] is None
        assert (sdk.order_reads,sdk.position_reads,tuple(sdk.posts),tuple(sdk.cancels))==calls
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_unknown_observation_fault_preserves_legacy_rows(tmp_path,monkeypatch):
    from tests.test_lp_d2_execution_bridge import rotation_runtime
    live,execution,sdk,ids,pools,order,qualify=rotation_runtime(tmp_path,monkeypatch)
    port=live.reader._port
    class Port:
        def read(self,name,scope):
            if name.startswith('market:'): raise ValueError('provider failure')
            return port.read(name,scope)
    live.reader._port=Port()
    sdk.orders=(order('A'),)
    try:
        execution.lp_auto_scheduled_check()
        baseline=execution.lp_auto_state(include_intents=False)['diagnostics']
        old,=baseline['unknown_buy_yields']['recent']
        live._yield_unknown_observations=tuple(dict(row,unsupported=object()) for row in live._yield_unknown_observations)
        calls=(sdk.order_reads,sdk.position_reads,tuple(sdk.posts),tuple(sdk.cancels))
        result=execution.lp_auto_state(include_intents=False)['diagnostics']
        row,=result['unknown_buy_yields']['recent']
        for key in ('order_id','unknown_since_at','age_seconds','due_at_600'): assert row[key]==old[key]
        assert result['accepted_observation']==baseline['accepted_observation']
        assert result['admission']==baseline['admission'] and result['round_start']==baseline['round_start']
        assert row['reason_codes']==['reason_unavailable'] and row['reason_is_current'] is False
        assert (sdk.order_reads,sdk.position_reads,tuple(sdk.posts),tuple(sdk.cancels))==calls
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_ring_update_fault_preserves_existing_iteration_refresh(tmp_path,monkeypatch):
    from tests.test_lp_d2_execution_bridge import rotation_runtime
    live,execution,sdk,ids,pools,order,qualify=rotation_runtime(tmp_path,monkeypatch)
    try:
        before=execution.lp_auto_state(include_intents=False)['diagnostics']
        live._yield_rounds=({'identity':{}},)
        sdk.advance(1)
        # An incomplete candidate source has one diagnostic capture. A normal
        # batch captures twice and can recover the ring before the GET below.
        result=live.run_once(SourceResult('incomplete-candidates',complete=False))
        assert result.finish is None and result.selection.selection_state=='NO_DECISION'
        after=execution.lp_auto_state(include_intents=False)['diagnostics']
        observation=result.acceptance.observation.observation_id
        assert observation!=before['accepted_observation']['observation_id']
        assert after['accepted_observation']['observation_id']==observation
        assert after['round_start']['observation_id']==observation
        assert after['admission']['observation_id']==observation
        assert after['reader_coverage']['observation_id']==observation
        assert after['yield_rounds']['available'] is False
        sdk.advance(1)
        recovered=execution.lp_auto_scheduled_check()
        history=execution.lp_auto_state(include_intents=False)['diagnostics']['yield_rounds']
        assert history['instance_id']!=before['yield_rounds']['instance_id']
        assert history['recent'][-1]['identity']['observation_id']==recovered.acceptance.observation.observation_id
        assert sdk.posts==sdk.cancels==[]
    finally:
        assert live.close(timeout=3)
        live.trading.close()
