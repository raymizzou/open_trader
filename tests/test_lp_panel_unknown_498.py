"""LP498: offline public-boundary pool, display and companion lifecycle contracts."""
import asyncio
import faulthandler
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from open_trader.polymarket_lp_read_runtime import LPReadRuntime
from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
from test_lp_async_read_runtime_contract import Clock, Boundary, collect, demand


@pytest.fixture(autouse=True)
def lp498_watchdog():
    faulthandler.dump_traceback_later(40,exit=True)
    try:yield
    finally:faulthandler.cancel_dump_traceback_later()


class FinancialServer:
    def __init__(self):
        self.requests = []
        self.holders = []
        self.release = threading.Event()
        self.closed = threading.Event()
        self.changed = threading.Condition()
        self.mode = 'ok'
        self.error_gate = None
        self.error_entered = set()
        self.error_completed = set()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_): pass
            def do_POST(self):
                with owner.changed:
                    owner.requests.append((self.command,self.path));owner.changed.notify_all()
                self.send_response(503);self.send_header('Content-Length','0');self.end_headers()
            def do_GET(self):
                path = self.path.split('?')[0]
                with owner.changed:
                    owner.requests.append((self.command, path))
                    owner.changed.notify_all()
                if path == '/hold':
                    self.send_response(200);self.send_header('Content-Length','1');self.end_headers()
                    owner.release.wait(45)
                    try:self.wfile.write(b'x');self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):pass
                    return
                values = {'/positions': [], '/balance-allowance':
                    dict(balance='100000000', allowances={'spender':'100000000'}),
                    '/data/orders': dict(data=[],next_cursor='LTE=')}
                body=json.dumps(values[path]).encode()
                if owner.mode=='error' and owner.error_gate is not None:
                    with owner.changed:
                        owner.error_entered.add(path);owner.changed.notify_all()
                    assert owner.error_gate.wait(4)
                self.send_response(503 if owner.mode=='error' else 429 if owner.mode=='cooling' else 200)
                if owner.mode=='cooling':self.send_header('Retry-After','12')
                self.send_header('Content-Length',str(len(body)));self.end_headers()
                if owner.mode=='slow':owner.release.wait(4)
                try:self.wfile.write(body);self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):pass
                if owner.mode=='error' and owner.error_gate is not None:
                    with owner.changed:
                        owner.error_completed.add(path);owner.changed.notify_all()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def close(self):
        if self.error_gate is not None:self.error_gate.set()
        self.release.set();self.server.shutdown();self.server.server_close();self.thread.join(2)


def actual_factory(server, clock, transport=None):
    sdk=SimpleNamespace(_ctx=SimpleNamespace(wallet_type='GNOSIS_SAFE',
        secure_clob=SimpleNamespace(_header_resolver=lambda *_:{'test-capability':'secret-sentinel'}),
        environment=SimpleNamespace(clob_url=server.url,data_url=server.url,gamma_url=server.url,
                                    standard_exchange='spender')))
    adapter=PolymarketTradingClient(TradingConfig('signer','account'),client=sdk)
    return adapter.lp_async_source_factory(clock=clock,utc_now=clock.utc,transport=transport)


class SaturatedFactory:
    """Occupy the actual factory's three slots using incomplete loopback bodies."""
    def __init__(self,server,clock):
        self.server,self.clock=server,clock
        self.create=actual_factory(server,clock);self.not_before=self.create.not_before
        self.ports=[];self.hold_tasks=[];self.responses=[];self.ready=threading.Event()
    def __call__(self,d):
        self.loop=asyncio.get_running_loop()
        port=self.create(d);self.ports.append(port)
        self.timeouts={client:client.timeout for client in port.clients.values()}
        if len(self.ports)==1:
            ready=asyncio.Event()
            async def occupy():
                for service in ('clob','clob','data'):
                    client=port.clients[service]
                    response=await client.send(client.build_request('GET','/hold'),stream=True)
                    self.responses.append(response)
                ready.set();self.ready.set()
            self.hold_tasks.append(asyncio.create_task(occupy()))
            async def before(request):
                if request.url.path!='/hold':await ready.wait()
            for client in port.clients.values():client.event_hooks['request'].insert(0,before)
        return port
    async def saturate_current(self):
        port=self.ports[-1]
        for client in port.clients.values():client.timeout=httpx.Timeout(**(self.timeouts[client].as_dict()|{'pool':.04}))
        for service in ('clob','clob','data'):
            client=port.clients[service]
            self.responses.append(await client.send(client.build_request('GET','/hold'),stream=True))

    async def release_holders(self):
        for response in self.responses:await response.aclose()
        await asyncio.gather(*self.hold_tasks)
        for client,timeout in self.timeouts.items():client.timeout=timeout


def stop(runtime,factory,server):
    server.release.set()
    if factory.ports:
        asyncio.run_coroutine_threadsafe(factory.release_holders(),factory.loop).result(4)
    runtime.reconcile(())
    assert runtime.close(4)
    server.close()


def test_financial_pool_wait_is_distinct_and_bounded():
    clock=Clock();server=FinancialServer();factory=SaturatedFactory(server,clock)
    runtime=LPReadRuntime(factory,clock=clock,utc_now=clock.utc);observer=Boundary(clock)
    runtime.start()
    try:
        runtime.reconcile((demand('financial',kind='financial',sources=('balance','open_orders','positions')),))
        assert factory.ready.wait(3)
        started=time.monotonic()
        rows=[]
        def finished():
            rows.extend(runtime.drain_results());return bool(rows)
        # This wall watchdog is independent of the frozen five-second business clock.
        observer.wait(finished,timeout=4.5)
        row,=rows
        assert 2.5<=time.monotonic()-started<4.5
        assert not row.complete and {s.error for s in row.sources.values()}=={'PoolTimeout'}
        assert all(s.diagnostics['exception_type']=='PoolTimeout' for s in row.sources.values())
        assert server.requests==[('GET','/hold')]*3
        assert 'secret-sentinel' not in repr(runtime.status())+repr(row)
        async def ordinary_timeout():
            port=factory.ports[0]
            await factory.release_holders()
            client=port.clients['data'];client.timeout=httpx.Timeout(None,read=.05)
            from open_trader.polymarket_lp_async_source import SourceRequest
            return await port.read(SourceRequest('positions','account'),clock()+5)
        # A real read timeout uses the same local boundary with a held body.
        server.mode='slow'
        ordinary=asyncio.run_coroutine_threadsafe(ordinary_timeout(),factory.loop).result(2)
        assert not ordinary.complete and ordinary.error=='ReadTimeout'
    finally:stop(runtime,factory,server)


def test_three_pool_timeout_rounds_rotate_and_recover(caplog):
    clock=Clock();server=FinancialServer();factory=SaturatedFactory(server,clock)
    runtime=LPReadRuntime(factory,clock=clock,utc_now=clock.utc);observer=Boundary(clock)
    runtime.start()
    try:
        runtime.reconcile((demand('financial',kind='financial',sources=('balance','open_orders','positions')),))
        assert factory.ready.wait(3)
        for index in range(3):
            clock.value=index*5;runtime.wake()
            failed,=collect(runtime,observer,bool)
            assert not failed.complete and {s.error for s in failed.sources.values()}=={'PoolTimeout'}
            status=runtime.status()
            assert status['financial_pool']['generation']==int(index==2)
            assert status['financial_pool']['consecutive_pool_timeouts']==(0 if index==2 else index+1)
            assert len(factory.ports)==1  # replacement is created at the next legal round
        old=factory.ports[0]
        old.provider_limits.observe('clob','account',17)
        clock.value=15;runtime.wake()
        observer.wait(lambda:runtime.status()['observed_monotonic']==15)
        assert runtime.drain_results()==() and len(factory.ports)==1
        clock.value=17;runtime.wake()
        good,=collect(runtime,observer,lambda rows:any(r.complete for r in rows))
        assert good.complete and good.started==17 and len(factory.ports)==2
        assert factory.ports[1].provider_limits is old.provider_limits
        status=runtime.status()['financial_pool']
        assert status['generation']==1 and status['consecutive_pool_timeouts']==0
        diagnostic=status['last_rotation']
        assert diagnostic['attempt']==failed.attempt and diagnostic['reason']=='pool_wait_timeout'
        assert [(p['service'],p['connections'],p['active']) for p in diagnostic['pools']]==[('clob',2,2),('data',1,1)]
        assert len(json.dumps(dict(diagnostic),default=list))<4096
        assert 'secret-sentinel' not in repr(diagnostic)
        assert caplog.text.count('prediction_lp_financial_pool_rotation')==1
        assert len([r for r in server.requests if r[1]!='/hold'])==3
        # Subsequent schedules exercise resets without repeating the real 3s bound.
        def step(t):
            clock.value=t;runtime.wake();return collect(runtime,observer,bool)[0]
        def saturate():
            asyncio.run_coroutine_threadsafe(factory.saturate_current(),factory.loop).result(2)
        def release():
            asyncio.run_coroutine_threadsafe(factory.release_holders(),factory.loop).result(2)
        saturate()
        for t,streak in ((22,1),(27,2)):
            assert {s.error for s in step(t).sources.values()}=={'PoolTimeout'}
            assert runtime.status()['financial_pool']['consecutive_pool_timeouts']==streak
        release();server.mode='error'
        server.error_gate=threading.Event();gate_result=[]
        def delayed_error_response():
            with server.changed:
                entered=server.changed.wait_for(lambda:len(server.error_entered)==3,timeout=2)
            # Response latency, unlike scheduling, deliberately exceeds the old read budget.
            early=server.error_gate.wait(.08)
            server.error_gate.set();gate_result.append((entered,early))
        delayed=threading.Thread(target=delayed_error_response,daemon=True);delayed.start()
        ordinary=step(32)
        delayed.join(2)
        assert not delayed.is_alive() and gate_result==[(True,False)]
        with server.changed:
            assert server.changed.wait_for(lambda:len(server.error_completed)==3,timeout=2)
        assert {s.error for s in ordinary.sources.values()}=={'ReadResponseError'}
        assert runtime.status()['financial_pool']['consecutive_pool_timeouts']==0
        saturate();assert not step(37).complete
        assert runtime.status()['financial_pool']['consecutive_pool_timeouts']==1
        release();server.mode='ok';assert step(42).complete
        assert runtime.status()['financial_pool']['consecutive_pool_timeouts']==0
        saturate();assert not step(47).complete
        release();server.mode='slow'
        for client in factory.ports[-1].clients.values():client.timeout=httpx.Timeout(**(client.timeout.as_dict()|{'read':.04}))
        assert {s.error for s in step(52).sources.values()}=={'ReadTimeout'}
        assert runtime.status()['financial_pool']['consecutive_pool_timeouts']==0
        assert runtime.status()['financial_pool']['generation']==1 and len(factory.ports)==2
    finally:stop(runtime,factory,server)


def test_pool_rotation_preserves_financial_cleanup_history():
    clock=Clock();observer=Boundary(clock);ports=[];failing=True
    sources=('balance','open_orders','positions')
    async def http(request):
        if failing:raise httpx.PoolTimeout('secret-sentinel',request=request)
        values={'/positions':[], '/balance-allowance':dict(balance='100000000',
            allowances={'spender':'100000000'}), '/data/orders':dict(data=[],next_cursor='LTE=')}
        return httpx.Response(200,json=values[request.url.path])
    create=actual_factory(SimpleNamespace(url='http://127.0.0.1'),clock,httpx.MockTransport(http))
    def factory(d):
        port=create(d);ports.append(port);return port
    factory.not_before=create.not_before
    runtime=LPReadRuntime(factory,clock=clock,utc_now=clock.utc)
    def step(attempt):
        nonlocal failing
        failing=attempt%4!=0
        clock.value=(attempt-1)*5;runtime.wake()
        row,=collect(runtime,observer,bool)
        assert row.attempt==attempt and row.complete is (not failing)
        if failing:assert {s.error for s in row.sources.values()}=={'PoolTimeout'}
        return row
    def snapshot():
        return json.loads(json.dumps(runtime.status()['financial_request_cleanup'],default=dict))
    def assert_history(attempt):
        observer.wait(lambda:runtime.status()['financial_request_cleanup'] is not None
            and runtime.status()['financial_request_cleanup']['attempt']==attempt
            and not runtime.status()['financial_request_cleanup']['current'])
        value=snapshot();history=value['history'];total=attempt*3
        assert sorted(r['sequence'] for r in history)==list(range(max(1,total-31),total+1))
        for row in history:
            assert row['source'] in sources
            assert row['outcome']==('complete' if row['attempt']%4==0 else 'failed')
            assert row['started_monotonic']==row['ended_monotonic']==(row['attempt']-1)*5
        for number in range(max(1,attempt-9),attempt+1):
            assert {r['source'] for r in history if r['attempt']==number}==set(sources)
        assert len(history)<=32 and len(json.dumps(value).encode())<=16*1024
        assert 'secret-sentinel' not in json.dumps(value)
        return value
    runtime.start()
    try:
        runtime.reconcile((demand('financial',kind='financial',sources=sources),))
        for attempt in (1,2,3):step(attempt)
        assert len(ports)==1 and runtime.status()['financial_pool']['generation']==1
        staged=ports[0].financial_cleanup_records(3)
        assert not staged['current'] and sorted(r['sequence'] for r in staged['history'])==list(range(1,10))
        assert {(r['attempt'],r['source']) for r in staged['history']}=={
            (number,source) for number in (1,2,3) for source in sources}
        step(4)
        first=assert_history(4)
        assert {r['sequence'] for r in first['history'] if r['attempt']==4}=={10,11,12}
        detached=runtime.status()['financial_request_cleanup']
        frozen_copy=snapshot()
        with pytest.raises(TypeError):detached['history'][0]['sequence']=-1
        first['history'][0]['sequence']=-1
        assert snapshot()['history'][0]['sequence']>0
        # More rollovers exercise the shared archive without double-offset or unbounded history.
        for attempt in range(5,17):
            step(attempt)
            if attempt%4==0:assert_history(attempt)
        assert json.loads(json.dumps(detached,default=dict))==frozen_copy
        assert sorted(r['sequence'] for r in detached['history'])==list(range(1,13))
        assert len(ports)==5 and runtime.status()['financial_pool']['generation']==4
        observer.wait(lambda:runtime.status()['financial_pool']['retired_groups']==0)
        assert all(client.is_closed for port in ports[:-1] for client in port.clients.values())
        assert all(not client.is_closed for client in ports[-1].clients.values())
    finally:
        runtime.reconcile(());assert runtime.close(4)
    assert all(client.is_closed for port in ports for client in port.clients.values())


def test_pool_rotation_keeps_unknown_and_blocks_new_buy(tmp_path):
    from decimal import Decimal
    from test_lp_async_live_contract import Trading
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    clock=Clock();server=FinancialServer();factory=SaturatedFactory(server,clock)
    runtime=LPReadRuntime(factory,clock=clock,utc_now=clock.utc);observer=Boundary(clock)
    trading=Trading()
    live=LPD2LiveExecution(PredictionArbitrageStore(tmp_path),trading,account_id='account',venue='polymarket',
        process_epoch='epoch',policy=CalculationPolicy(Decimal(100)),read_runtime=runtime,clock=clock,utc_now=clock.utc)
    live.initialize(ValidatedStartAdmission('epoch','offline-permit'))
    try:
        live.run_once()
        assert factory.ready.wait(3)
        for index in range(3):
            clock.value=index*5;runtime.wake()
            observer.wait(lambda:runtime.status()['ready_count']>0,timeout=4.5)
            live.run_once()
            assert live.state.snapshot().new_capacity.state=='unknown'
            assert live.state.snapshot().observation.orders_complete is False
            assert trading.cancels==[]
            assert not any(method=='POST' for method,_ in server.requests)
        assert runtime.status()['financial_pool']['generation']==1
        assert live.state.snapshot().new_capacity.state=='unknown'
        clock.value=15;runtime.wake()
        observer.wait(lambda:runtime.status()['ready_count']>0)
        assert live.state.snapshot().new_capacity.state=='unknown'  # mailbox is not authority
        live.run_once()
        snapshot=live.state.snapshot()
        assert snapshot.observation.orders_complete and snapshot.observation.positions_complete
        assert snapshot.new_capacity.state=='known' and snapshot.new_capacity.value==100
        assert trading.cancels==[] and len(server.requests)==6
        assert not any(method=='POST' for method,_ in server.requests)
    finally:
        server.release.set()
        asyncio.run_coroutine_threadsafe(factory.release_holders(),factory.loop).result(4)
        assert live.close(4);server.close()


def test_rotated_group_closes_and_shutdown_drains():
    class DelayedClose(httpx.AsyncHTTPTransport):
        def __init__(self):
            super().__init__(limits=httpx.Limits(max_connections=3,max_keepalive_connections=3))
            self.entered=threading.Event();self.release=threading.Event();self.exited=threading.Event()
        async def aclose(self):
            self.entered.set()
            while not self.release.is_set():await asyncio.sleep(.01)
            await super().aclose();self.exited.set()
    clock=Clock();server=FinancialServer();factory=SaturatedFactory(server,clock)
    transport=DelayedClose();first=actual_factory(server,clock,transport)
    default=factory.create
    factory.create=lambda d:first(d) if not factory.ports else default(d)
    runtime=LPReadRuntime(factory,clock=clock,utc_now=clock.utc);observer=Boundary(clock)
    runtime.start()
    try:
        runtime.reconcile((demand('financial',kind='financial',sources=('balance','open_orders','positions')),))
        assert factory.ready.wait(3)
        for i in range(3):
            clock.value=5*i;runtime.wake();failed,=collect(runtime,observer,bool)
            assert not failed.complete
        assert transport.entered.wait(2) and not transport.exited.is_set()
        clock.value=15;runtime.wake()
        good,=collect(runtime,observer,lambda rows:any(r.complete for r in rows))
        assert good.complete and len(factory.ports)==2 and not transport.exited.is_set()
        state=runtime.status()
        assert state['financial_pool']['retired_groups']==1
        assert state['financial_pool']['last_rotation']['close_state']=='delayed'
        assert state['cleanup_faults']==1 and not state['new_buy_allowed']
        assert state['pool_count']==4 and state['managed_tasks']>=1
        runtime.reconcile(())
        assert not runtime.close(.02)  # owned background close cannot disappear at shutdown
        assert not transport.exited.is_set()
        transport.release.set()
        observer.wait(lambda:transport.exited.is_set())
        assert runtime.close(4)
        assert runtime.status()['managed_tasks']==0
        assert runtime.status()['financial_pool']['last_rotation']['close_state']=='closed'
        assert all(client.is_closed for port in factory.ports for client in port.clients.values())
        assert transport._pool.connections==[]
    finally:
        transport.release.set();server.release.set()
        # Hold responses are test-owned; their sockets were closed by the group.
        assert runtime.close(4)
        server.close()
    import os,subprocess,sys
    result=subprocess.run([sys.executable,'-c',
        'from test_lp_panel_unknown_498 import failed_group_close; failed_group_close()'],
        env=dict(os.environ,PYTHONPATH='src:tests'),capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def failed_group_close():
    faulthandler.dump_traceback_later(12,exit=True)
    class FailedClose(httpx.AsyncHTTPTransport):
        async def aclose(self):
            await super().aclose()
            raise OSError('external close failure')
    clock=Clock();server=FinancialServer();factory=SaturatedFactory(server,clock)
    transport=FailedClose(limits=httpx.Limits(max_connections=3,max_keepalive_connections=3))
    first=actual_factory(server,clock,transport);default=factory.create
    def create(d):
        port=first(d) if not factory.ports else default(d)
        for client in port.clients.values():client.timeout=httpx.Timeout(None,pool=.04)
        return port
    factory.create=create
    runtime=LPReadRuntime(factory,clock=clock,utc_now=clock.utc);observer=Boundary(clock);runtime.start()
    try:
        runtime.reconcile((demand('financial',kind='financial',sources=('balance','open_orders','positions')),))
        assert factory.ready.wait(3)
        for t in (0,5,10):
            clock.value=t;runtime.wake();assert not collect(runtime,observer,bool)[0].complete
        observer.wait(lambda:runtime.status()['financial_pool']['last_rotation']['close_state']=='failed')
        assert runtime.status()['financial_pool']['retired_groups']==1
        assert not runtime.status()['new_buy_allowed'] and runtime.status()['cleanup_faults']==1
        clock.value=15;runtime.wake();assert collect(runtime,observer,bool)[0].complete
        asyncio.run_coroutine_threadsafe(factory.saturate_current(),factory.loop).result(2)
        for t in (20,25,30,35):
            clock.value=t;runtime.wake();assert not collect(runtime,observer,bool)[0].complete
        assert runtime.status()['financial_pool']['generation']==1 and len(factory.ports)==2
        assert not runtime.close(.02)
        assert runtime.status()['financial_pool']['last_rotation']['close_state']=='failed'
        assert transport._pool.connections==[]
    finally:
        server.release.set();server.close();faulthandler.cancel_dump_traceback_later()


def monitor_input(clock):
    return dict(observed_pools=dict(current_membership=dict(state='unknown',account_id='account',orders=[])),
        effective_settings=dict(trial_target_buy_count=1,income_target_buy_count=1),
        desired_running=True,effective_running=True)


def summarize(clock,auto=None,dashboard=None,audits=None,monitor=None,account='account'):
    from open_trader.polymarket_lp_monitor import LPMonitorSummary
    return (monitor or LPMonitorSummary()).summarize(auto=auto or monitor_input(clock),dashboard=dashboard or {},
        trades={},audits=[] if audits is None else audits,account_id=account,now=clock.utc())


def test_engine_account_read_health_is_explicit():
    from open_trader.polymarket_lp_monitor import LPMonitorSummary
    clock=Clock();boundary=Boundary(clock)
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc);runtime.start()
    monitor=LPMonitorSummary()
    def summary(account='account',patch=None):
        auto=monitor_input(clock)
        status=dict(runtime.status())
        if patch is not None:status['engine_account_read']=patch
        auto['diagnostics']=dict(async_reads=status)
        return summarize(clock,auto,monitor=monitor,account=account)
    try:
        assert summary()['pools']['read_health']['engine_account_read']['status']=='unknown'
        runtime.reconcile((demand('financial',kind='financial',sources=('balance','open_orders','positions')),))
        assert collect(runtime,boundary,bool)[0].complete
        initial=summary()['pools']['read_health']['engine_account_read']
        assert initial['last_success_at']==clock.utc().isoformat() and initial['consecutive_failures']==0
        for t in (5,10):
            boundary.mode.update({('financial',s):'failure' for s in ('balance','open_orders','positions')})
            clock.value=t;runtime.wake();assert not collect(runtime,boundary,bool)[0].complete
        assert summary()['pools']['read_health']['engine_account_read']['consecutive_failures']==2
        runtime.reconcile(())
        boundary.wait(lambda:not runtime.status()['scopes'])
        clock.value=60
        assert summary()['pools']['read_health']['engine_account_read']['status']=='attention'
        clock.value=61
        value=summary()
        assert value['pools']['read_health']['status']=='abnormal'
        assert value['pools']['read_health']['engine_account_read']['age_seconds']==61
        for key in ('orders','balance'):
            assert value[key]['status']=='abnormal'
            assert '做市引擎读不到账户' in value[key]['reason'] and '61 秒' in value[key]['reason']
        requests=tuple(boundary.events)
        for _ in range(10):assert summary()['pools']['read_health']['engine_account_read']==value['pools']['read_health']['engine_account_read']
        assert tuple(boundary.events)==requests
        for patch in (dict(account_id='other',last_success_at=initial['last_success_at'],consecutive_failures=2),
            dict(account_id='account',last_success_at=(clock.utc()+timedelta(seconds=1)).isoformat(),consecutive_failures=2),
            dict(account_id='account',last_success_at=None,consecutive_failures=2)):
            assert summary(patch=patch)['pools']['read_health']['engine_account_read']['status']=='unknown'
        assert summary(account='other')['pools']['read_health']['engine_account_read']['status']=='unknown'
        boundary.mode.clear();clock.value=65;runtime.wake()
        runtime.reconcile((demand('financial',kind='financial',sources=('balance','open_orders','positions')),))
        assert collect(runtime,boundary,bool)[0].complete
        fresh=summary()['pools']['read_health']['engine_account_read']
        assert fresh['status']=='normal' and fresh['consecutive_failures']==0 and fresh['age_seconds']==0
        other_clock=Clock();other_monitor=LPMonitorSummary()
        assert summarize(other_clock,monitor=other_monitor)['balance']['status']=='unknown'
        other_clock.value=301
        switched=summarize(other_clock,monitor=other_monitor,account='other')
        assert switched['balance']['status']=='unknown'  # another account cannot inherit the old UNKNOWN duration
        assert switched['pools']['read_health']['engine_account_read']['last_success_at'] is None
    finally:runtime.reconcile(());assert runtime.close(4)


def panel_html(value):
    from open_trader.lp_monitor_readonly import render_page
    from test_dashboard_web import run_dashboard_js
    yield render_page(value)
    yield json.loads(run_dashboard_js('console.log(JSON.stringify(lpMonitorCards('+json.dumps(value)+')));'))


def test_confirmed_balance_survives_engine_unknown():
    clock=Clock()
    dashboard=dict(account_id='account',authenticated=True,stale=False,checked_at=clock.utc().isoformat(),
        balance='493.97',allowance='1000',positions_complete=True,open_orders_complete=True)
    value=summarize(clock,dashboard=dashboard)
    assert value['balance']['balance_usd']=='493.97' and value['balance']['checked_at']==clock.utc().isoformat()
    assert value['balance']['capacity_usd'] is None and value['balance']['status']=='unknown'
    for html in panel_html(value):
        assert '已确认余额 $493.97' in html and '引擎可用资金（USD）' in html
        assert clock.utc().isoformat() in html
    for patch in (dict(account_id='other'),dict(authenticated=False),dict(checked_at=None),dict(stale=True),
        dict(checked_at=(clock.utc()+timedelta(seconds=1)).isoformat()),dict(balance=None)):
        invalid=summarize(clock,dashboard={**dashboard,**patch})['balance']
        assert invalid['balance_usd'] is None and invalid['checked_at'] is None
    # Neither another account nor previous display reads may supply current capacity.
    assert summarize(clock,dashboard=dashboard,account='other')['balance']['capacity_usd'] is None


def test_stalled_candidates_take_priority_over_zero():
    clock=Clock();stamp=clock.utc().isoformat();clock.value=601
    dashboard=dict(account_id='account',candidate_valid_count=0,candidate_last_success_at=stamp)
    value=summarize(clock,dashboard=dashboard)['pools']
    assert value['status']=='abnormal' and '候选刷新已停' in value['reason'] and '10 分钟' in value['reason']
    for age in (0,600):
        dashboard['candidate_last_success_at']=(clock.utc()-timedelta(seconds=age)).isoformat()
        assert summarize(clock,dashboard=dashboard)['pools']['reason']=='有效候选为零'
    dashboard['candidate_last_success_at']=stamp
    for patch,reason in ((dict(fatal=True),'致命'),(dict(effective_running=False),'想开')):
        assert reason in summarize(clock,auto={**monitor_input(clock),**patch},dashboard=dashboard)['pools']['reason']
    for missing in (None,(clock.utc()+timedelta(seconds=1)).isoformat()):
        value=summarize(clock,dashboard={**dashboard,'candidate_last_success_at':missing})['pools']
        assert value['status']=='unknown' and '候选刷新已停' not in value['reason']


def cancel_audit(clock,age,*,outcome='unknown',order='old'):
    row=dict(kind='CANCEL',account_id='account',process_epoch='epoch',request_id='r-'+order,session_id='s',
        condition_id='condition',token_id='token',order_id=order,requested_at=(clock.utc()-timedelta(seconds=age)).isoformat())
    row['receipt']={**{k:row[k] for k in ('account_id','process_epoch','request_id','session_id','condition_id','token_id','order_id')},'outcome':outcome}
    return row


def confirmed_membership(clock,orders):
    auto=monitor_input(clock)
    auto['observed_pools']['current_membership'].update(state='known',observed_at=clock.utc().isoformat(),orders=orders)
    return auto


def test_old_unknown_cancels_downgrade_only_when_absent():
    from copy import deepcopy
    clock=Clock()
    live=dict(account_id='account',condition_id='condition',token_id='token',order_id='old',side='BUY',pool='trial')
    cases=[(86401,[],None,'attention'),(86400,[],None,'abnormal'),(1,[],None,'abnormal'),
        (86401,[live],None,'abnormal'),(86401,[],dict(state='unknown'),'abnormal'),
        (86401,[],dict(account_id='foreign'),'abnormal'),
        (86401,[{**live,'token_id':'different'}],None,'abnormal')]
    for age,orders,patch,expected in cases:
        auto=confirmed_membership(clock,orders)
        if patch:auto['observed_pools']['current_membership'].update(patch)
        audits=[cancel_audit(clock,age)];before=deepcopy(audits)
        value=summarize(clock,auto=auto,audits=audits)['cancellations']
        assert value['status']==expected,(age,orders,patch,value)
        assert value['unknown_counts']==dict(total=1,recent=int(age<=86400),older=int(age>86400))
        assert value['items'][0]['outcome']=='unknown' and audits==before
    pending=cancel_audit(clock,86401,outcome='accepted')
    assert summarize(clock,auto=confirmed_membership(clock,[live]),audits=[pending])['cancellations']['stuck']
    terminal=deepcopy(pending);terminal['progress']={**{k:pending[k] for k in ('account_id','process_epoch','request_id','session_id','condition_id','token_id','order_id')},'status':'CANCELED'}
    value=summarize(clock,auto=confirmed_membership(clock,[]),audits=[terminal])['cancellations']
    assert value['status']=='normal' and value['unknown_counts']['total']==0
    bad=deepcopy(pending);bad['receipt']['token_id']='different'
    assert summarize(clock,auto=confirmed_membership(clock,[]),audits=[bad])['cancellations']['status']=='unknown'


def test_panels_show_account_read_and_unknown_cancel_counts():
    from open_trader.lp_monitor_readonly import safe_summary
    clock=Clock();last=clock.utc().isoformat();clock.value=90
    auto=confirmed_membership(clock,[])
    auto['diagnostics']=dict(async_reads=dict(engine_account_read=dict(account_id='account',last_success_at=last,consecutive_failures=3),
        cleanup_fault_details=dict(total=0,omitted=0,items=[]),cleanup_faults=0,new_buy_allowed=True,new_buy_block_reasons=[]))
    dashboard=dict(account_id='account',authenticated=True,stale=False,checked_at=clock.utc().isoformat(),balance='493.97',allowance='1000')
    value=summarize(clock,auto=auto,dashboard=dashboard,audits=[cancel_audit(clock,1,order='recent'),cancel_audit(clock,86401)])
    value['pools']['read_health']['engine_account_read']['secret']='nested-secret'
    value['pools']['read_health']['engine_account_read']['reason']='<script>bad()</script>'
    clean=safe_summary(value)
    assert clean['pools']['read_health']['engine_account_read']['last_success_at']==last
    assert clean['cancellations']['unknown_counts']==dict(total=2,recent=1,older=1)
    assert 'nested-secret' not in json.dumps(clean)
    for html in panel_html(value):
        assert '引擎账户读取' in html and '连续失败次数' in html and last in html
        assert '卡单 0 · 结果未知 2' in html and '24 小时内' in html and '更早' in html
        assert '已确认余额 $493.97' in html and '引擎可用资金（USD）' in html
        assert '<script>bad()</script>' not in html and '&lt;script&gt;bad()&lt;/script&gt;' in html


def test_readonly_upstream_wait_covers_slow_summary(monkeypatch):
    from contextlib import contextmanager
    from datetime import UTC,datetime
    from open_trader import lp_monitor_readonly as readonly
    from open_trader.polymarket_lp_monitor import unknown_summary
    seen=[]
    class FailingOpener:
        def open(self,request,timeout):
            seen.append(timeout);raise TimeoutError('local-boundary')
    with monkeypatch.context() as patch:
        patch.setattr(readonly,'build_opener',lambda *_:FailingOpener())
        assert readonly.fetch_summary('http://127.0.0.1:8769'+readonly.SUMMARY_PATH)['orders']['status']=='unknown'
    assert seen==[15]
    entered=threading.Event();release=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            entered.set();assert release.wait(12)
            value=unknown_summary(datetime.now(UTC));value['orders']['reason']='本机慢响应已完成'
            body=json.dumps(value).encode();self.send_response(200)
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    timer=threading.Timer(8,release.set);timer.start()
    try:
        started=time.monotonic()
        value=readonly.fetch_summary(f'http://127.0.0.1:{server.server_port}'+readonly.SUMMARY_PATH)
        assert entered.is_set() and 5<time.monotonic()-started<15
        assert value['orders']['reason']=='本机慢响应已完成'
    finally:release.set();timer.cancel();timer.join(1);server.shutdown();server.server_close();thread.join(2)


def panel_lifecycle_host(tmp_path,monkeypatch):
    from pathlib import Path
    import shlex,subprocess
    from open_trader import prediction_cloud as cloud
    from tests.test_prediction_retirement_lifecycle import cloud_host_fixture
    cfg,host,commands,_=cloud_host_fixture(tmp_path,monkeypatch,'shadow',1)
    config_path=tmp_path/'cloud.json'
    data=json.loads(config_path.read_text());data['lp_monitor_host']='100.70.80.90'
    config_path.write_text(json.dumps(data));cfg=cloud.load_config(config_path)
    panel=cloud.UNIT_PATH.with_name('open-trader-lp-monitor.service')
    monkeypatch.setattr(cloud.os,'geteuid',lambda:0)
    monkeypatch.setattr(cloud,'OPERATION_LOCK',tmp_path/'operation.lock')
    record=json.loads(cfg.record.read_text());record['state']='stopped';cfg.record.write_text(json.dumps(record))
    external=subprocess.run
    panel_pid=23456
    panel_active=[False]
    model=SimpleNamespace(start_panel=True,properties={},listener=None,uid=1001)
    def fake_os(command,**kwargs):
        args=list(command)
        if args[:3]==['systemctl','show',panel.name]:
            commands.append(args)
            fields=dict(LoadState='loaded',ActiveState='active' if panel_active[0] else 'inactive',
                MainPID=str(panel_pid if panel_active[0] else 0),FragmentPath=str(panel),DropInPaths='',
                NeedDaemonReload='no',User='prediction',Group='prediction',WorkingDirectory=str(cfg.release_root))
            fields.update(model.properties)
            return subprocess.CompletedProcess(args,0,'\n'.join(k+'='+v for k,v in fields.items()),'')
        if args[:2] in (['systemctl','start'],['systemctl','stop']):
            commands.append(args)
            assert args[2]==cloud.UNIT
            host['active']=args[1]=='start';panel_active[0]=host['active'] and model.start_panel
            return subprocess.CompletedProcess(args,0,'','')
        if args==['systemctl','daemon-reload'] or args[0]=='systemd-analyze':
            commands.append(args)
            if args==['systemctl','daemon-reload']:host['loaded_unit']=cloud.UNIT_PATH.read_text()
            return subprocess.CompletedProcess(args,0,'','')
        if args[0]=='ss' and args[-1]=='sport = :8780':
            commands.append(args)
            return subprocess.CompletedProcess(args,0,
                model.listener if model.listener is not None else (
                f'LISTEN 0 100 100.70.80.90:8780 0.0.0.0:* users:(("python",pid={panel_pid},fd=3))' if panel_active[0] else ''),'')
        return external(command,**kwargs)
    monkeypatch.setattr(subprocess,'run',fake_os)
    native_text,native_bytes,native_resolve=Path.read_text,Path.read_bytes,Path.resolve
    def text(path,*args,**kwargs):
        if str(path)=='/proc/meminfo':return 'MemAvailable: 4000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n'
        if str(path)==f'/proc/{panel_pid}/status':return f'Uid:\t{model.uid}\t{model.uid}\t{model.uid}\t{model.uid}\nGid:\t1001\t1001\t1001\t1001\n'
        return native_text(path,*args,**kwargs)
    def raw(path,*args,**kwargs):
        if str(path)==f'/proc/{panel_pid}/cmdline':
            return ('\0'.join(shlex.split(panel.read_text().split('ExecStart=',1)[1].splitlines()[0])[6:])+'\0').encode()
        if str(path)==f'/proc/{panel_pid}/environ':
            return f'PATH=/usr/bin:/bin\0PYTHONPATH={cfg.release_root}/src\0PYTHONUNBUFFERED=1\0PYTHONDONTWRITEBYTECODE=1\0'.encode()
        return native_bytes(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',text);monkeypatch.setattr(Path,'read_bytes',raw)
    monkeypatch.setattr(Path,'resolve',lambda path,*a,**kw:cfg.release_root if str(path)==f'/proc/{panel_pid}/cwd' else native_resolve(path,*a,**kw))
    return SimpleNamespace(cloud=cloud,cfg=cfg,host=host,commands=commands,panel=panel,
        panel_active=panel_active,model=model)


def test_panel_deployment_uses_service_release_and_lifecycle(tmp_path,monkeypatch):
    from dataclasses import replace
    fixture=panel_lifecycle_host(tmp_path,monkeypatch)
    cloud,cfg,host,commands,panel=fixture.cloud,fixture.cfg,fixture.host,fixture.commands,fixture.panel
    panel_active=fixture.panel_active
    unit=cloud.render_unit(cfg);panel_text=cloud.render_monitor_unit(cfg)
    assert 'Wants=open-trader-lp-monitor.service' in unit
    assert f'PartOf={cloud.UNIT}' in panel_text and f'After={cloud.UNIT}' in panel_text
    assert f'WorkingDirectory={cfg.release_root}' in panel_text and f'PYTHONPATH={cfg.release_root}/src' in panel_text
    assert f'{cfg.python} -m open_trader.lp_monitor_readonly --host 100.70.80.90 --port 8780' in panel_text
    assert 'ExecStart=/usr/bin/env -i ' in panel_text
    assert all(x not in panel_text for x in ('SSM','CREDENTIAL','wallet','--mode production','prediction_arbitrage.json'))
    assert cloud.render_monitor_unit(replace(cfg,lp_monitor_host=None)) is None
    before=list(commands)
    for bind in ('0.0.0.0','8.8.8.8','localhost','::'):
        with pytest.raises(ValueError):cloud.operate(replace(cfg,lp_monitor_host=bind),'install')
    assert commands==before
    # Wrong source identity must fail before any service mutation or artifact write.
    with pytest.raises(ValueError,match='SHA mismatch'):cloud.operate(replace(cfg,expected_sha='b'*40),'install')
    assert not panel.exists()
    result=cloud.operate(cfg,'install')
    assert result['status']=='INSTALLED_STOPPED' and panel.read_text()==panel_text
    assert not panel_active[0] and not any(c[:2]==['systemctl','start'] for c in commands)
    saved=json.loads(cfg.record.read_text());assert saved['monitor_unit_text']==panel_text
    assert saved['candidate']['git_sha']==cfg.expected_sha
    assert cloud.operate(cfg,'start')['status']=='RUNNING' and panel_active[0]
    assert cloud.operate(cfg,'restart')['status']=='RUNNING' and panel_active[0]
    assert [c[:2] for c in commands if c[:2] in (['systemctl','start'],['systemctl','stop'])]==[
        ['systemctl','start'],['systemctl','stop'],['systemctl','start']]
    assert cloud.operate(cfg,'stop')['status']=='STOPPED' and not panel_active[0]
    assert json.loads(cfg.record.read_text())['monitor_unit_text']==panel_text
    # A companion left at a different release is not accepted on restart.
    panel.write_text(panel_text.replace(str(cfg.release_root),'/wrong-release'))
    count=len(commands)
    with pytest.raises(ValueError,match='monitor'):cloud.operate(cfg,'start')
    assert not any(c[:2]==['systemctl','start'] for c in commands[count:])


@pytest.mark.parametrize('routing', ('mounted_pool', 'opaque_mount'))
def test_pool_occupancy_uses_actual_routed_transport(routing):
    from open_trader.polymarket_lp_async_source import LPAsyncSourcePort
    async def scenario():
        server=FinancialServer()
        class OpaqueMount(httpx.AsyncBaseTransport):
            def __init__(self,backend):self.backend=backend
            async def handle_async_request(self,request):return await self.backend.handle_async_request(request)
            async def aclose(self):await self.backend.aclose()
        clients={};responses=[]
        for name,size in (('clob',2),('data',1)):
            backend=httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=size,max_keepalive_connections=size))
            routed=backend if routing=='mounted_pool' else OpaqueMount(backend)
            clients[name]=httpx.AsyncClient(base_url=server.url,trust_env=False,
                auth=httpx.BasicAuth('diagnostic-user','routing-secret-sentinel'),
                mounts={'all://127.0.0.1':routed})
        port=LPAsyncSourcePort(clients)
        try:
            for name in ('clob','clob','data'):
                client=clients[name]
                responses.append(await client.send(client.build_request('GET','/hold'),stream=True))
            before=tuple(server.requests)
            assert before==(('GET','/hold'),)*3
            for _ in range(3):
                rows=port.financial_pool_status()
                for row,size in zip(rows,(2,1)):
                    if row['connections'] is not None:
                        assert row['connections']==size and row['active']==size and row['idle']==0
                    else:
                        assert row['active'] is None and row['idle'] is None
                if routing=='mounted_pool':assert [row['connections'] for row in rows]==[2,1]
                assert 'routing-secret-sentinel' not in json.dumps(rows) and server.url not in json.dumps(rows)
            assert tuple(server.requests)==before
        finally:
            for response in responses:await response.aclose()
            await port.aclose();server.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ('stop','restart'))
@pytest.mark.parametrize('companion_state', ('inactive','failed'))
def test_absent_companion_allows_main_stop_and_restart(tmp_path,monkeypatch,action,companion_state):
    fixture=panel_lifecycle_host(tmp_path,monkeypatch)
    cloud,cfg=fixture.cloud,fixture.cfg
    assert cloud.operate(cfg,'install')['status']=='INSTALLED_STOPPED'
    assert cloud.operate(cfg,'start')['status']=='RUNNING'
    cloud.installed_unit(cfg)
    assert cloud.verified_record(cfg,('ready',))['state']=='ready'
    assert cloud.live_identity(cfg)['lp_monitor']['pid']>0
    fixture.panel_active[0]=False
    fixture.model.properties['ActiveState']=companion_state
    before=len(fixture.commands)
    if action=='restart':
        # Only an actual main start heals the companion's pre-stop state.
        original=cloud.subprocess.run
        def external(command,**kwargs):
            if list(command)==['systemctl','start',cloud.UNIT]:fixture.model.properties.clear()
            return original(command,**kwargs)
        monkeypatch.setattr(cloud.subprocess,'run',external)
    result=cloud.operate(cfg,action)
    mutations=[c[:2] for c in fixture.commands[before:] if c[:2] in (['systemctl','stop'],['systemctl','start'])]
    assert mutations==([['systemctl','stop']] if action=='stop' else [['systemctl','stop'],['systemctl','start']])
    assert result['status']==('STOPPED' if action=='stop' else 'RUNNING')
    saved=json.loads(cfg.record.read_text())
    assert saved['state']==('stopped' if action=='stop' else 'ready')
    if action=='restart':
        assert fixture.host['active'] and fixture.panel_active[0]
        assert result['lp_monitor']['pid']>0 and cloud.live_identity(cfg)==saved['ready']
    else:
        assert not fixture.host['active'] and not fixture.panel_active[0]


@pytest.mark.parametrize('action', ('stop','restart'))
@pytest.mark.parametrize('evidence', (
    'missing_artifact','mismatched_artifact','mismatched_record','missing_loaded_identity',
    'wrong_loaded_user','reload_pending','unowned_dropin','unexplained_listener',
    'inactive_live_pid','unsupported_state','active_unowned_process','active_unowned_listener','active_valid'))
def test_main_lifecycle_rejects_unproven_companion_absence(tmp_path,monkeypatch,action,evidence):
    fixture=panel_lifecycle_host(tmp_path,monkeypatch)
    cloud,cfg=fixture.cloud,fixture.cfg
    assert cloud.operate(cfg,'install')['status']=='INSTALLED_STOPPED'
    assert cloud.operate(cfg,'start')['status']=='RUNNING'
    fixture.panel_active[0]=False
    if evidence=='missing_artifact':fixture.panel.unlink()
    elif evidence=='mismatched_artifact':fixture.panel.write_text(fixture.panel.read_text().replace(str(cfg.release_root),'/wrong-release'))
    elif evidence=='mismatched_record':
        saved=json.loads(cfg.record.read_text());saved['monitor_unit_text']='unrecognized-unit'
        cfg.record.write_text(json.dumps(saved))
    elif evidence=='missing_loaded_identity':fixture.model.properties['FragmentPath']=''
    elif evidence=='wrong_loaded_user':fixture.model.properties['User']='unowned'
    elif evidence=='reload_pending':fixture.model.properties['NeedDaemonReload']='yes'
    elif evidence=='unowned_dropin':fixture.model.properties['DropInPaths']='/unowned.conf'
    elif evidence=='unexplained_listener':fixture.model.listener='LISTEN 0 100 100.70.80.90:8780 0.0.0.0:* users:(("other",pid=99999,fd=3))'
    elif evidence=='inactive_live_pid':fixture.model.properties['MainPID']='23456'
    elif evidence=='unsupported_state':fixture.model.properties['ActiveState']='activating'
    elif evidence=='active_unowned_process':
        fixture.panel_active[0]=True;fixture.model.uid=9999
    elif evidence=='active_unowned_listener':
        fixture.panel_active[0]=True
        fixture.model.listener='LISTEN 0 100 100.70.80.90:8780 0.0.0.0:* users:(("other",pid=99999,fd=3))'
    elif evidence=='active_valid':fixture.panel_active[0]=True
    before=len(fixture.commands);saved=cfg.record.read_bytes()
    if evidence=='active_valid':
        result=cloud.operate(cfg,action)
        assert result['status']==('STOPPED' if action=='stop' else 'RUNNING')
        assert [c[:2] for c in fixture.commands[before:] if c[:2] in (['systemctl','stop'],['systemctl','start'])]==(
            [['systemctl','stop']] if action=='stop' else [['systemctl','stop'],['systemctl','start']])
    else:
        with pytest.raises((OSError,ValueError)):cloud.operate(cfg,action)
        assert fixture.host['active']
        assert cfg.record.read_bytes()==saved
        assert not any(c[:2] in (['systemctl','stop'],['systemctl','start']) for c in fixture.commands[before:])


@pytest.mark.parametrize('action', ('start','restart'))
def test_absent_companion_never_reports_service_ready(tmp_path,monkeypatch,action):
    fixture=panel_lifecycle_host(tmp_path,monkeypatch)
    cloud,cfg=fixture.cloud,fixture.cfg
    assert cloud.operate(cfg,'install')['status']=='INSTALLED_STOPPED'
    assert cloud.operate(cfg,'start')['status']=='RUNNING'
    fixture.panel_active[0]=False
    saved=cfg.record.read_bytes();before=len(fixture.commands)
    with pytest.raises(ValueError,match='monitor'):cloud.live_identity(cfg)
    with pytest.raises(ValueError,match='monitor'):cloud.monitor_identity(cfg)
    for check in ('status','smoke'):
        with pytest.raises(ValueError,match='monitor'):cloud.operate(cfg,check)
    assert fixture.host['active'] and cfg.record.read_bytes()==saved
    assert not any(c[:2] in (['systemctl','stop'],['systemctl','start']) for c in fixture.commands[before:])
    if action=='start':assert cloud.operate(cfg,'stop')['status']=='STOPPED'
    fixture.model.start_panel=False
    clock=Clock()
    def elapsed(seconds):clock.value+=seconds
    # Replace only this module's clock binding; the native watchdog keeps real time.
    monkeypatch.setattr(cloud,'time',SimpleNamespace(monotonic=clock,sleep=elapsed))
    before=len(fixture.commands)
    with pytest.raises(ValueError,match='candidate readiness not proven'):cloud.operate(cfg,action)
    assert fixture.host['active'] and not fixture.panel_active[0]
    failed=json.loads(cfg.record.read_text())
    assert failed['state']=='failed' and failed['failure_reason']=='candidate_readiness_not_proven'
    assert 'ready' not in failed
    assert [c[:2] for c in fixture.commands[before:] if c[:2] in (['systemctl','stop'],['systemctl','start'])]==(
        [['systemctl','start']] if action=='start' else [['systemctl','stop'],['systemctl','start']])
    with pytest.raises(ValueError):cloud.operate(cfg,'status')
    assert json.loads(cfg.record.read_text())['state']=='failed'
