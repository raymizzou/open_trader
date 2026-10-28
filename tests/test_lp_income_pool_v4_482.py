"""#482 v4 contracts at real owner, adapter and read-only display seams."""
import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import time
import httpx
import pytest

from tests.test_lp_async_source_contract import NOW, CID, independent_watchdog
from open_trader.polymarket_lp_async_source import SourceRequest


def test_reward_share_reads_open_order_native_and_sponsored():
    from open_trader.polymarket_trading import PolymarketTradingClient
    from open_trader.polymarket_lp_read_runtime import ReadDemand
    async def scenario():
        requests=[]; signed=[]; mode=['ok']
        def auth(method,path,body):
            signed.append((method,path,body)); return {'test-owned-get':'yes'}
        adapter=object.__new__(PolymarketTradingClient)
        adapter.config=SimpleNamespace(wallet_address='account')
        adapter._client=SimpleNamespace(_ctx=SimpleNamespace(wallet_type='GNOSIS_SAFE',
            secure_clob=SimpleNamespace(_header_resolver=auth),environment=SimpleNamespace(
                clob_url='http://127.0.0.1',gamma_url='http://127.0.0.1',data_url='http://127.0.0.1',standard_exchange='spender')))
        async def http(req):
            requests.append(req)
            assert req.method=='GET' and req.url.path=='/rewards/user/markets'
            assert req.headers['test-owned-get']=='yes'
            assert req.url.params['only_open_orders']=='true'
            assert req.url.params['only_open_positions']=='false'
            sponsored=req.url.params['sponsored']=='true'
            row=dict(condition_id=CID,earning_percentage='25' if sponsored else '50')
            if mode[0]=='malformed':row['earning_percentage']='NaN'
            if mode[0]=='future':row['checked_at']=(NOW+timedelta(seconds=1)).isoformat()
            if mode[0]=='failure':return httpx.Response(503)
            cursor=req.url.params.get('next_cursor')
            if cursor and mode[0]=='conflict':row['earning_percentage']='99'
            return httpx.Response(200,json=dict(data=[row],next_cursor='LTE=' if cursor else 'second'))
        factory=adapter.lp_async_source_factory(utc_now=lambda:NOW,transport=httpx.MockTransport(http))
        port=factory(ReadDemand('account','epoch','generation','share',1,kind='reward_share',sources=('reward_share',)))
        try:
            req=SourceRequest('reward_share','account',request_id='share')
            result=await port.read(req,time.monotonic()+10)
            assert result.complete, result
            assert result.value['markets'][CID]==dict(native=Decimal('50'),sponsored=Decimal('25'))
            assert len(requests)==4 and {r.url.params['sponsored'] for r in requests}=={'true','false'}
            assert signed==[('GET','/rewards/user/markets',None)]*4
            for method,path,body in [('POST','/rewards/user/markets',None),('GET','/auth/api-key',None),
                    ('GET','/rewards/user/markets',{})]:
                with pytest.raises(ValueError):port.auth_resolver(method,path,body)
            for failure in ('malformed','future','failure','conflict'):
                mode[0]=failure
                failed=await port.read(req,time.monotonic()+10)
                assert not failed.complete and failed.value is None, (failure,failed)
        finally:await port.aclose()
    asyncio.run(scenario())


def test_reward_share_runtime_cadence_and_failure_isolation():
    import threading
    from tests.test_lp_async_read_runtime_contract import Clock, Boundary, demand, collect
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    from open_trader.polymarket_lp_async_source import LPAsyncSourcePort
    clock=Clock(); boundary=Boundary(clock); release=threading.Event(); entered=threading.Event()
    requests=[]
    def factory(d):
        if d.kind!='reward_share':return boundary.factory(d)
        async def http(req):
            requests.append(clock());entered.set()
            if len(requests)==1:
                try:await asyncio.get_running_loop().run_in_executor(None,release.wait)
                except asyncio.CancelledError:
                    await asyncio.get_running_loop().run_in_executor(None,release.wait)
            return httpx.Response(503)
        return LPAsyncSourcePort(dict(clob=httpx.AsyncClient(base_url='http://127.0.0.1',
            transport=httpx.MockTransport(http))),clock=clock,utc_now=clock.utc)
    runtime=LPReadRuntime(factory,clock=clock,utc_now=clock.utc);runtime.start()
    share=demand('share',kind='reward_share',sources=('reward_share',))
    try:
        runtime.reconcile((share,demand('duplicate',kind='reward_share',sources=('reward_share',)),
            demand('B'),demand('sell',kind='sell'),demand('O',kind='financial',sources=('balance','open_orders','positions')),
            demand('exact',kind='exact',sources=('order',))))
        rows=collect(runtime,boundary,lambda rows:{'B','O','exact','sell'}<={r.demand.order_id for r in rows})
        assert entered.wait(2) and requests==[0]
        assert all(r.complete for r in rows if r.demand.order_id in ('B','O','exact','sell'))
        clock.value=5;runtime.wake()
        assert not any(r.demand.kind=='reward_share' for r in runtime.drain_results())
        clock.value=10;runtime.wake()
        expired=collect(runtime,boundary,lambda rows:any(r.demand.kind=='reward_share' for r in rows))
        row=next(r for r in expired if r.demand.kind=='reward_share')
        assert (row.started,row.deadline,row.reason)==(0,10,'timeout')
        assert runtime.status()['new_buy_allowed'] is True
        clock.value=11;runtime.wake()
        boundary.wait(lambda:runtime.status()['cleanup_faults']==1)
        fault,=runtime.status()['cleanup_fault_details']['items']
        assert fault['lane']=='reward_share' and fault['blocks_new_buy'] is False
        assert runtime.status()['new_buy_allowed'] is True
        clock.value=15;runtime.wake()
        healthy=collect(runtime,boundary,lambda rows:any(r.demand.order_id=='B' and r.started==15 for r in rows))
        assert any(r.demand.order_id=='B' and r.complete for r in healthy)
        release.set()
        boundary.wait(lambda:not any(s['active'] for s in runtime.status()['scopes'] if s['kind']=='reward_share'))
        clock.value=29.999;runtime.wake()
        boundary.wait(lambda:runtime.status()['observed_monotonic']==clock())
        assert requests==[0]
        clock.value=30;runtime.wake()
        collect(runtime,boundary,lambda rows:any(r.demand.kind=='reward_share' for r in rows))
        assert requests==[0,30] and runtime.status()['new_buy_allowed'] is True
        assert sum(s['kind']=='reward_share' for s in runtime.status()['scopes'])==1
    finally:
        release.set();boundary.release.set();runtime.reconcile(());assert runtime.close(4)


def test_d2_samples_without_reward_thread(tmp_path,monkeypatch):
    import threading
    from tests.test_lp_observed_pools import AsyncPoolRig
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    held=threading.Event();release=threading.Event();thread=None
    try:
        for name,pool in [('A','240'),('B','120')]:rig.market(name,minimum='2',pool=pool)
        for name in ('A','B'):rig.add_order(name,'2')
        rig.observe_initial_measurement(dict(A='50',B='25'))
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,roles={
            rig.cid(name):dict(pool='trial',episode='old-'+name) for name in ('A','B')})
        rig.hourly.update(A=Decimal('1'),B=Decimal('.5'))
        original=rig.sdk._ctx.secure_clob.get_json
        def stalled(path,**kwargs):
            if path=='/rewards/user/markets':
                held.set();assert release.wait(4),'external reward worker watchdog'
            return original(path,**kwargs)
        rig.sdk._ctx.secure_clob.get_json=stalled
        thread=threading.Thread(target=rig.execution.refresh_lp_observations)
        thread.start();assert held.wait(2)
        rig.tick(30)
        rig.converge_current(lambda:all(rig.store.lp_observations(rig.account).get(rig.cid(n),{}).
            get('pool_result',{}).get('state')=='known' for n in ('A','B')))
        samples=rig.store.lp_observations(rig.account)
        for name in ('A','B'):
            sample=samples[rig.cid(name)]['pool_result']
            actual=next(o for o in rig.sdk.orders if o['market']==rig.cid(name))
            assert sample['source_order']['order_id']==actual['id']
            assert sample['source_order']['token_id']=='t'+name
            assert sample['account_id']==rig.account
        result=rig.cycle(pool='income')
        assert result.rotation_outcomes
        assert not release.is_set()
    finally:
        release.set()
        if thread is not None:thread.join(4);assert not thread.is_alive()
        rig.close()


def test_d2_sample_uses_oldest_input_and_shared_formula():
    from open_trader.polymarket_lp_observed_pools import d2_measured_result, measured_result
    orders=[dict(order_id='B',condition_id=CID,token_id='yes',side='BUY',status='LIVE',
        price=Decimal('.5'),quantity=Decimal(12),remaining_quantity=Decimal(10),filled_quantity=Decimal(2),
        scoring_status='true',scoring_checked_at=NOW-timedelta(seconds=4))]
    sample=d2_measured_result('account',CID,orders,
        dict(native_daily_pool_usd=24,sponsored_daily_pool_usd=48),dict(native=50,sponsored=25),
        pool_checked_at=NOW-timedelta(seconds=3),share_checked_at=NOW-timedelta(seconds=20),
        account_checked_at=NOW-timedelta(seconds=2),as_of=NOW)
    assert sample['state']=='known'
    assert (sample['hourly_reward_usd'],sample['buy_capital_usd'],sample['yield_pct_per_hour'])==(1,5,20)
    assert sample['measured_at']==NOW-timedelta(seconds=20)
    legacy=measured_result('account',CID,orders,Decimal(1),reward_known=True,
        measured_at=sample['measured_at'],reward_checked_at=sample['reward_checked_at'],
        account_checked_at=sample['account_checked_at'])
    assert sample==legacy


def test_d2_sample_unknown_reasons_and_boundaries():
    from open_trader.polymarket_lp_observed_pools import d2_measured_result, result_projection
    def sample(**changes):
        arguments=dict(account_id='account',condition_id=CID,orders=[dict(order_id='B',condition_id=CID,
            token_id='yes',side='BUY',status='LIVE',price='.5',quantity='12',remaining_quantity='10',
            filled_quantity='2',scoring_status='true',scoring_checked_at=NOW)],
            pool=dict(native_daily_pool_usd=24,sponsored_daily_pool_usd=48),shares=dict(native=50,sponsored=25),
            pool_checked_at=NOW-timedelta(seconds=10),share_checked_at=NOW-timedelta(seconds=60),
            account_checked_at=NOW,as_of=NOW)
        arguments.update(changes)
        return d2_measured_result(**arguments)
    known=sample()
    assert known['state']=='known' and known['measured_at']==NOW-timedelta(seconds=60)
    assert result_projection({CID:dict(pool_result=known)},'account',NOW+timedelta(seconds=60))[CID]['eligible']
    assert not result_projection({CID:dict(pool_result=known)},'account',NOW+timedelta(seconds=60,microseconds=1))[CID]['eligible']
    row=dict(order_id='B',condition_id=CID,token_id='yes',side='BUY',status='LIVE',price='.5',quantity='12',
        remaining_quantity='10',filled_quantity='2',scoring_status='true',scoring_checked_at=NOW)
    for changes,reason in [
        (dict(pool_checked_at=NOW-timedelta(seconds=10,microseconds=1)),'reward_pool_stale'),
        (dict(pool_checked_at=NOW+timedelta(seconds=1)),'reward_pool_stale'),
        (dict(shares=None),'reward_share_unknown'),
        (dict(shares={}), 'reward_share_unknown'),
        (dict(share_checked_at=NOW-timedelta(seconds=60,microseconds=1)),'reward_share_unknown'),
        (dict(share_checked_at=NOW+timedelta(seconds=1)),'reward_share_unknown'),
        (dict(orders=[dict(row,scoring_status='false')]),'not_scoring'),
        (dict(orders=[dict(row,scoring_status='unknown')]),'not_scoring'),
        (dict(orders=[row,dict(row,order_id='S',side='SELL')]),'multiple_orders'),
        (dict(orders=[dict(row,condition_id='other')]),'terms_unknown'),
        (dict(orders=[dict(row,filled_quantity=None)]),'terms_unknown'),
        (dict(orders=[dict(row,quantity='13')]),'terms_unknown')]:
        result=sample(**changes)
        assert result['state']=='unknown' and result['reason']==reason and result['yield_pct_per_hour'] is None, (changes,result)
    zero=sample(pool=dict(native_daily_pool_usd=0,sponsored_daily_pool_usd=0),shares={})
    assert zero['state']=='known' and zero['yield_pct_per_hour']==0
    assert sample(pool=dict(native_daily_pool_usd=0,sponsored_daily_pool_usd=0),shares=None)['state']=='unknown'


def test_reward_share_unknown_keeps_600_second_timer(tmp_path,monkeypatch):
    from tests.test_lp_income_pool_ab_482 import _timed_rig, _protect_at, _cancel_reasons, _sample, _finish
    for pool in ('trial','income'):
        rig=_timed_rig(tmp_path/pool,monkeypatch,pool=pool)
        try:
            _protect_at(rig,0);_protect_at(rig,599)
            assert rig.sdk.cancels==[]
            _protect_at(rig,600)
            assert rig.sdk.cancels==([('old-A',)] if pool=='trial' else [])
            if pool=='trial':assert ('old-A','measured_yield_unknown_timeout') in _cancel_reasons(rig)
            _finish(rig.live.run_protection_once())
            assert len(rig.sdk.cancels)==(1 if pool=='trial' else 0)
        finally:rig.close()
    _zero_at_600(tmp_path/'known-zero',monkeypatch)
    _zero_in_protection_at_600(tmp_path/'same-protection-round',monkeypatch)


def _d2_sample_rig(tmp_path,monkeypatch,*,clock_values=None):
    from tests.test_lp_observed_pools import AsyncPoolRig
    rig=AsyncPoolRig(tmp_path,monkeypatch,clock_values=clock_values)
    rig.market('A',minimum='2',pool='240');rig.add_order('A','2')
    rig.observe_initial_measurement(dict(A='50'))
    rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
        roles={rig.cid('A'):dict(pool='trial',episode='old-A')})
    rig.hourly['A']=Decimal(1)
    rig.tick(30)
    def drive():
        rig.live.run_once(pool='trial');rig.live.run_protection_once()
    rig.converge_current(lambda:rig.store.lp_observations(rig.account).get(rig.cid('A'),{}).
        get('pool_result',{}).get('state')=='known',drive=drive)
    return rig


def test_d2_is_only_pool_result_writer(tmp_path,monkeypatch):
    import threading
    from copy import deepcopy
    rig=_d2_sample_rig(tmp_path,monkeypatch)
    try:
        cid=rig.cid('A')
        before=deepcopy(rig.store.lp_observations(rig.account)[cid]['pool_result'])
        attempts=rig.execution.lp_observed_pool_state()['measurement_attempts']
        assert attempts[cid]['result']['state']=='known'
        rig.hourly['A']=Decimal(0)
        rig.execution.refresh_lp_observations()
        assert rig.store.lp_observations(rig.account)[cid]['pool_result']==before
        assert rig.execution.lp_observed_pool_state()['measurement_attempts']==attempts
        stop=threading.Event();stop.set()
        rig.execution.refresh_lp_observations(stop_event=stop)
        assert rig.execution.lp_observed_pool_state()['measurement_attempts']==attempts
        rig.sdk.orders[0]['id']=''
        assert rig.store.lp_advance_trade_generation(rig.store.lp_trade_generation())
        rig.execution.refresh_lp_observations()
        assert rig.store.lp_observations(rig.account)[cid]['pool_result']==before
        assert rig.execution.lp_observed_pool_state()['measurement_attempts']==attempts
        assert len(attempts)<=64
        detached=rig.live.pool_result_projection()
        detached[cid]['result']['source_order']['order_id']='reader mutation'
        assert rig.live.pool_result_projection()[cid]['result']['source_order']['order_id']=='old-A'
        # Public narrow update preserves risk/share-watch and a prior owner's valid archive.
        rig.store.update_lp_observation(rig.account,cid,dict(risk_state='known',share_alert=dict(marker='keep')))
        rig.store.update_lp_observation(rig.account,cid,dict(pool_result=dict(state='unknown',reason='reward_share_unknown')))
        saved=rig.store.lp_observations(rig.account)[cid]
        assert saved['pool_result']==before and saved['risk_state']=='known' and saved['share_alert']['marker']=='keep'
    finally:rig.close()


def test_yield_display_uses_d2_sample(tmp_path,monkeypatch):
    from copy import deepcopy
    rig=_d2_sample_rig(tmp_path,monkeypatch)
    try:
        cid=rig.cid('A');sample=rig.store.lp_observations(rig.account)[cid]['pool_result']
        # Conflicting legacy reward display must not replace the current BUY source.
        rig.hourly['A']=Decimal(0)
        rig.execution.refresh_lp_observations()
        rig.execution.refresh_lp_dashboard_snapshot()
        before=(len(rig.requests),deepcopy(rig.sdk.posts),deepcopy(rig.sdk.cancels),rig.store.lp_trade_generation())
        dashboard=rig.execution.lp_dashboard()
        observation=dashboard['lp_observations'][cid]
        assert observation['yield_source']=='d2_order_sample'
        assert Decimal(observation['current_hourly_reward_usd'])==Decimal(sample['hourly_reward_usd'])==1
        assert Decimal(observation['occupied_capital_usd'])==Decimal(sample['buy_capital_usd'])==2
        assert Decimal(observation['current_yield_pct_per_hour'])==Decimal(sample['yield_pct_per_hour'])==50
        import json
        from tests.test_dashboard_web import run_dashboard_js
        def rendered(row):
            return json.loads(run_dashboard_js('const observation='+json.dumps(row,default=str)+';'
                'console.log(JSON.stringify({yield:lpDashboardYieldCell(observation),'
                'hourly:lpDashboardTodayRewardCell({condition_id:"A"},{},observation)}));'))
        html=rendered(observation)
        assert '实际 <strong>50%' in html['yield'] and '$1.00</strong>/小时' in html['hourly']
        assert before==(len(rig.requests),rig.sdk.posts,rig.sdk.cancels,rig.store.lp_trade_generation())
        original_orders=rig.sdk.list_open_orders
        def unavailable(**kwargs):raise OSError('offline incomplete account boundary')
        rig.sdk.list_open_orders=unavailable
        assert rig.store.lp_advance_trade_generation(rig.store.lp_trade_generation())
        rig.execution.refresh_lp_dashboard_snapshot()
        incomplete=rig.execution.lp_dashboard()['lp_observations'][cid]
        assert incomplete['state']=='unknown' and incomplete['current_yield_pct_per_hour'] is None
        html=rendered(incomplete)
        assert '待更新' in html['yield'] and 'UNKNOWN' in html['hourly']
        rig.sdk.list_open_orders=original_orders
        rig.sdk.orders[0]['price']=Decimal('.3')
        assert rig.store.lp_advance_trade_generation(rig.store.lp_trade_generation())
        rig.execution.refresh_lp_dashboard_snapshot()
        mismatch=rig.execution.lp_dashboard()['lp_observations'][cid]
        assert mismatch['state']=='unknown' and mismatch['current_yield_pct_per_hour'] is None
        rig.sdk.orders[0]['price']=Decimal('.4')
        assert rig.store.lp_advance_trade_generation(rig.store.lp_trade_generation())
        rig.execution.refresh_lp_dashboard_snapshot()
        before=(len(rig.requests),deepcopy(rig.sdk.posts),deepcopy(rig.sdk.cancels),rig.store.lp_trade_generation())
        from open_trader import prediction_arbitrage_execution as execution_module
        monkeypatch.setattr(execution_module,'_utc_now',lambda:rig.utc()+timedelta(seconds=121))
        expired=rig.execution.lp_dashboard()['lp_observations'][cid]
        assert expired['state']=='unknown' and expired['current_yield_pct_per_hour'] is None
        assert expired['reason']=='account_facts_stale'
        assert before==(len(rig.requests),rig.sdk.posts,rig.sdk.cancels,rig.store.lp_trade_generation())
    finally:rig.close()


def test_monitor_shows_sample_and_share_ages(tmp_path,monkeypatch):
    from copy import deepcopy
    from open_trader.lp_monitor_readonly import safe_summary,render_page
    rig=_d2_sample_rig(tmp_path,monkeypatch)
    try:
        cid=rig.cid('A');sample=rig.store.lp_observations(rig.account)[cid]['pool_result']
        from datetime import datetime
        measured=datetime.fromisoformat(sample['measured_at'].replace('Z','+00:00'))
        now=rig.utc()+timedelta(seconds=7)
        before=(len(rig.requests),rig.store.lp_trade_generation(),deepcopy(rig.sdk.cancels))
        summary=safe_summary(rig.execution.lp_monitor_summary(now=now))
        row,=summary['pools']['measurement_attempts']
        assert row['sample_age_seconds']==(now-measured).total_seconds()
        assert row['order_id']=='old-A'
        assert summary['pools']['reward_share']['age_seconds']==7
        page=render_page(summary)
        assert '每单样本年龄' in page and '奖励占比读取年龄' in page
        assert '观测刷新停滞' not in page
        aged=safe_summary(rig.execution.lp_monitor_summary(now=measured+timedelta(seconds=121)))
        assert aged['pools']['reward_share']['state']=='unknown'
        stale,=aged['pools']['measurement_attempts']
        assert stale['state']=='unknown' and stale['reason']=='sample_expired' and stale['yield_pct_per_hour'] is None
        assert before==(len(rig.requests),rig.store.lp_trade_generation(),rig.sdk.cancels)
        malicious=deepcopy(summary);malicious['pools']['measurement_attempts'][0]['reason']='<script>test</script>'
        assert '<script>' not in render_page(malicious) and '&lt;script&gt;' in render_page(malicious)
    finally:rig.close()


def test_air_income_sample_end_to_end(tmp_path,monkeypatch):
    import json
    import threading
    from urllib.request import urlopen
    from tests.test_prediction_service import _Runtime,_server
    from tests.test_frontend_gateway import _running
    from open_trader.lp_monitor_readonly import create_readonly_server
    from datetime import UTC,datetime
    rig=_d2_sample_rig(tmp_path,monkeypatch,clock_values=[datetime.now(UTC)-timedelta(seconds=45)])
    release=threading.Event();held=threading.Event();worker=None
    try:
        original=rig.sdk._ctx.secure_clob.get_json
        def stalled(path,**kwargs):
            if path=='/rewards/user/markets':held.set();assert release.wait(8)
            return original(path,**kwargs)
        rig.sdk._ctx.secure_clob.get_json=stalled
        worker=threading.Thread(target=rig.execution.refresh_lp_observations);worker.start();assert held.wait(2)
        rig.execution.refresh_lp_dashboard_snapshot()
        runtime=_Runtime();runtime.mode='production';runtime.production_owner=True
        runtime.execution=rig.execution;runtime.store=rig.store
        with _server(runtime) as upstream:
            listener=create_readonly_server(host='127.0.0.1',port=0,
                upstream_url=upstream+'/api/prediction-arbitrage/lp/monitor')
            with _running(listener):
                base='http://127.0.0.1:'+str(listener.server_address[1])
                with urlopen(base+'/api/prediction-arbitrage/lp/monitor',timeout=3) as response:summary=json.load(response)
                row,=summary['pools']['measurement_attempts']
                assert row['order_id']=='old-A' and row['state']=='known' and row['sample_age_seconds']<=120
                assert summary['pools']['reward_share']['age_seconds']<=60
                with urlopen(base+'/',timeout=3) as response:page=response.read().decode()
                assert '每单样本年龄' in page and '奖励占比读取年龄' in page
                with urlopen(upstream+'/api/prediction-arbitrage/lp/dashboard',timeout=3) as response:dashboard=json.load(response)
                assert dashboard['lp_observations'][rig.cid('A')]['current_yield_pct_per_hour']=='50.0'
                result=rig.cycle(pool='income');rig.finish_cancels(result)
                assert rig.sdk.cancels==[('old-A',)] and not release.is_set()
    finally:
        release.set()
        if worker is not None:worker.join(4);assert not worker.is_alive()
        rig.close()


def _zero_at_600(tmp_path,monkeypatch):
    import threading
    from tests.test_lp_observed_pools import AsyncPoolRig
    from tests.test_lp_income_pool_ab_482 import _finish
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    entered=threading.Event();release=threading.Event();fail_share=[False]
    try:
        rig.market('A');rig.add_order('A','8')
        rig.observe_initial_measurement(dict(A='0'))
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid('A'):dict(pool='trial',episode='old-A')})
        _finish(rig.live.run_protection_once())
        start=rig.seconds
        original=rig.http
        async def held_share(req):
            if req.headers['x-lp-source']=='reward_share' and fail_share[0]:return httpx.Response(503)
            if req.headers['x-lp-source']=='reward_share' and rig.seconds>=start+599:
                entered.set()
                assert await asyncio.get_running_loop().run_in_executor(None,release.wait,4)
            return await original(req)
        rig.http=held_share
        rig.tick(599)
        def drive():rig.live.run_once(pool='trial')
        rig.pump(lambda:entered.is_set() and rig.live.state.snapshot().observation.components[
            'open_orders'].checked_at==rig.utc(),drive=drive)
        _finish(rig.live.run_protection_once())
        assert rig.seconds==start+599 and rig.sdk.cancels==[]
        rig.hourly['A']=Decimal(0)
        rig.tick(1);release.set()
        rig.pump(lambda:rig.live.pool_result_projection().get(rig.cid('A'),{}).get('result',{}).
            get('yield_pct_per_hour') is not None and Decimal(rig.live.pool_result_projection()[rig.cid('A')][
                'result']['yield_pct_per_hour'])==0,drive=drive)
        _finish(rig.live.run_protection_once())
        assert rig.seconds==start+600 and rig.sdk.cancels==[]
        sample=rig.live.pool_result_projection()[rig.cid('A')]['result']
        assert Decimal(sample['hourly_reward_usd'])==0
        from datetime import datetime
        measured=datetime.fromisoformat(sample['measured_at'].replace('Z','+00:00'))
        fail_share[0]=True
        rig.tick((measured+timedelta(seconds=120)-rig.utc()).total_seconds())
        rig.pump(lambda:rig.live.state.snapshot().observation.components['open_orders'].checked_at==rig.utc()
            and rig.execution.lp_observed_pool_state()['reward_share']['state']=='unknown',drive=drive)
        _finish(rig.live.run_protection_once())
        assert rig.live.pool_result_projection()[rig.cid('A')]['eligible'] is True and rig.sdk.cancels==[]
        rig.tick(.001);drive();_finish(rig.live.run_protection_once())
        rig.tick(599)
        rig.pump(lambda:rig.live.state.snapshot().observation.components['open_orders'].checked_at==rig.utc(),drive=drive)
        _finish(rig.live.run_protection_once());assert rig.sdk.cancels==[]
        rig.tick(1);drive();_finish(rig.live.run_protection_once())
        assert rig.sdk.cancels==[('old-A',)]
    finally:release.set();rig.close()


def _zero_in_protection_at_600(tmp_path,monkeypatch):
    import threading
    from tests.test_lp_observed_pools import AsyncPoolRig
    from tests.test_lp_income_pool_ab_482 import _finish
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    entered=threading.Event();release=threading.Event()
    try:
        rig.market('A');rig.add_order('A','8');rig.hourly['A']=Decimal(0)
        original=rig.http
        async def scoring_boundary(req):
            if req.headers['x-lp-source']=='scoring':
                if rig.seconds<599:return httpx.Response(503)
                entered.set()
                assert await asyncio.get_running_loop().run_in_executor(None,release.wait,4)
            return await original(req)
        rig.http=scoring_boundary
        rig.observe_initial_measurement({'A':'0'})
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid('A'):dict(pool='trial',episode='old-A')})
        _finish(rig.live.run_protection_once())
        start=rig.seconds
        rig.tick(599)
        def drive():
            rig.live.run_once(pool='trial');rig.live.run_once(pool='income')
        rig.pump(lambda:entered.is_set() and rig.live.state.snapshot().observation.components[
            'open_orders'].checked_at==rig.utc() and not any(scope['active'] for scope in
                rig.live.read_runtime.status()['scopes'] if scope['kind']!='exact'),drive=drive)
        drive()
        _finish(rig.live.run_protection_once())
        assert rig.seconds==start+599 and rig.sdk.cancels==[]
        assert not rig.live.pool_result_projection()
        rig.tick(1);release.set()
        rig.pump(lambda:rig.live.read_runtime.status()['ready_count']>0,drive=lambda:None)
        # Only the external exact result is drained; no business round is due.
        result=rig.live.run_once(pool='trial')
        assert result.acceptance.status=='pending'
        assert not rig.live.pool_result_projection()
        attempts=rig.live.run_protection_once()
        _finish(attempts)
        assert attempts==()
        sample=rig.live.pool_result_projection()[rig.cid('A')]['result']
        assert rig.seconds==start+600 and rig.sdk.cancels==[]
        assert Decimal(sample['hourly_reward_usd'])==Decimal(sample['yield_pct_per_hour'])==0
        assert sample['source_order']['order_id']=='old-A'
    finally:release.set();rig.close()
