"""Runtime contracts observe external request ownership and public results."""
import asyncio
from test_lp_async_source_contract import independent_watchdog
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import threading
import time
import httpx
from open_trader.polymarket_lp_async_source import LPAsyncSourcePort
from open_trader.polymarket_lp_read_runtime import LPReadRuntime, ReadDemand
from open_trader.polymarket_lp_market_reader import CapturedSources, MarketReader, SourceResult
from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
from open_trader.polymarket_lp_market_calculation import calculate_current, CalculationPolicy
from test_lp_async_source_contract import NOW, CID, market, reward, book

class Clock:
    def __init__(self):self.value=0.0
    def __call__(self):return self.value
    def utc(self):return NOW+timedelta(seconds=self.value)

class Boundary:
    def __init__(self,clock):
        self.clock=clock;self.holds=set();self.release=threading.Event()
        self.changed=threading.Condition();self.events=[];self.mode={};self.orders=[];self.book_depth=100;self.market_closed=False;self.paginate=False;self.page_one_release=threading.Event()
    def record(self,event,order,source):
        with self.changed:
            self.events.append((event,order,source,self.clock()))
            self.changed.notify_all()
    def wait(self,predicate,timeout=4):
        end=time.monotonic()+timeout
        while not predicate():
            remaining=end-time.monotonic()
            assert remaining>0, self.events
            # Never hold the external transport Condition while calling a
            # public runtime predicate; the native watchdog also covers it.
            with self.changed:self.changed.wait(min(.02,remaining))
    def factory(self,demand):
        boundary=self
        async def http(req):
            source=req.headers['x-lp-source'];order=req.headers['x-lp-order']
            self.record('entered',order,source)
            try:
                if self.paginate and source=='metadata' and req.url.path=='/markets/keyset':
                    if 'after_cursor' not in req.url.params:
                        await asyncio.get_running_loop().run_in_executor(None,self.page_one_release.wait)
                        return httpx.Response(200,json=dict(markets=[],next_cursor='second'))
                    self.record('page_two_entered',order,source)
                if (order,source) in self.holds:
                    if self.mode.get((order,source))=='block':self.release.wait()
                    else:
                        try:await asyncio.get_running_loop().run_in_executor(None,self.release.wait)
                        except asyncio.CancelledError:
                            self.record('cancel_received',order,source)
                            if self.mode.get((order,source))!='swallow':raise
                            await asyncio.get_running_loop().run_in_executor(None,self.release.wait)
                if self.mode.get((order,source))=='failure':return httpx.Response(503)
                if self.mode.get((order,source))=='book404':return httpx.Response(404)
                if self.mode.get((order,source))=='cooling':return httpx.Response(429,headers={'Retry-After':'12'})
                if source=='metadata':
                    value=(dict(events=[dict(id='7',slug='event',title='Event')],next_cursor=None)
                        if req.url.path=='/events/keyset' else dict(markets=[dict(market(),closed=self.market_closed,acceptingOrders=not self.market_closed,conditionId=demand.condition_id,
                            clobTokenIds='["'+demand.token_id+'","opposite"]')],next_cursor=None))
                elif source in ('native_rewards','sponsored_rewards'):
                    value=dict(data=[dict(reward(),condition_id=demand.condition_id)],next_cursor='LTE=')
                elif source=='book':
                    value=dict(book(),market=demand.condition_id,asset_id=demand.token_id,timestamp=str(int(self.clock.utc().timestamp()*1000)))
                    value['bids']=[dict(price='.40',size=str(self.book_depth))]
                elif source=='open_orders':value=dict(data=self.orders,next_cursor='LTE=')
                elif source=='balance':value=dict(balance='100000000',allowances={'spender':'100000000'})
                elif source=='positions':value=[]
                elif source=='order':value=dict(id='exact',market=CID,asset_id='yes',side='BUY',
                    original_size='20',size_matched='3',price='0.40',status='CANCELED',
                    owner='owner',maker_address='account',outcome='Yes',expiration='0',order_type='GTC',created_at=0)
                else:raise AssertionError(source)
                return httpx.Response(200,json=value)
            except asyncio.CancelledError:
                self.record('cancel_received',order,source);raise
            finally:self.record('finally_exited',order,source)
        # External transport only. Each runtime scope creates its own physical clients.
        return LPAsyncSourcePort({key:httpx.AsyncClient(transport=httpx.MockTransport(http),
            base_url='http://127.0.0.1') for key in (('clob','data') if demand.kind=='financial' else ('clob',)
                if demand.kind=='exact' else ('gamma','clob'))},clock=self.clock,utc_now=self.clock.utc)

def demand(order='B',**kwargs):return ReadDemand('account','epoch','generation',order,1,CID,'yes',**kwargs)

def collect(runtime,boundary,predicate):
    result=[]
    end=time.monotonic()+4
    while not predicate(result):
        result.extend(runtime.drain_results())
        if predicate(result):break
        remaining=end-time.monotonic();assert remaining>0,(result,runtime.status(),boundary.events)
        with boundary.changed:boundary.changed.wait(min(.02,remaining))
    return result

def test_slow_low_reads_and_one_buy_leave_other_buy_and_cancel_progress():
    clock=Clock();boundary=Boundary(clock)
    boundary.holds={('A','book'),('low','book')}
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
    runtime.start(lambda:None)
    try:
        runtime.reconcile((demand('A'),demand(),demand('low',kind='position'),
            demand('O',kind='financial',sources=('balance','open_orders','positions')),
            demand('exact',kind='exact',sources=('order',))))
        results=collect(runtime,boundary,lambda rows: {'B','O','exact'}<={r.demand.order_id for r in rows})
        assert not boundary.release.is_set()
        by_order={r.demand.order_id:r for r in results}
        assert by_order['B'].complete
        assert by_order['O'].complete and by_order['exact'].complete
        exact=by_order['exact'].sources['order'].value
        assert (exact['original_size'],exact['size_matched'])==(Decimal('20'),Decimal('3'))
        assert exact['original_size']-exact['size_matched']==17
        from open_trader.polymarket_lp_async_source import market_value
        market_fact=market_value(CID,by_order['B'].sources)
        # Complete zero rewards is a real known zero, not missing reward data.
        zero=dict(market_fact,rewards=dict(market_fact['rewards'],daily_pool_usd=Decimal(0),
            markets=(dict(market_fact['rewards']['markets'][0],daily_pool_usd=Decimal(0)),)))
        order=dict(order_id='B',condition_id=CID,token_id='yes',side='BUY',status='LIVE',
            original_size='20',size_matched='0',price='0.40')
        frame=MarketReader.from_captured_sources(CapturedSources('account',dict(
            balance=SourceResult('o',(100,100),checked_at=clock.utc()),
            open_orders=SourceResult('o',(order,),checked_at=clock.utc()),
            positions=SourceResult('o',(),checked_at=clock.utc()),
            **{'market:'+CID+':yes':SourceResult('b',zero,checked_at=clock.utc())})))
        calculation=calculate_current(prepare_current_inputs(frame,None,CalculationPolicy(Decimal(100)),clock.utc()),CalculationPolicy(Decimal(100)),clock.utc())
        assert (calculation.yields['B'],calculation.buy_reserved,calculation.new_capacity)==(0,8,92)
        assert runtime.status()['owned_buy_scopes']==2
    finally:
        boundary.release.set();runtime.reconcile(());assert runtime.close(4)

def test_whole_attempt_deadline_cancels_children_once():
    clock=Clock();boundary=Boundary(clock)
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((demand(),))
        assert collect(runtime,boundary,lambda rows:bool(rows))[0].complete
        boundary.paginate=True
        boundary.holds={('B',source) for source in ('metadata','native_rewards','sponsored_rewards')}
        clock.value=30;runtime.wake()
        boundary.wait(lambda:all(any(e[:3]==('entered','B',source) and e[3]==30
            for e in boundary.events) for source in ('metadata','native_rewards','sponsored_rewards')))
        clock.value=32;boundary.page_one_release.set()
        boundary.wait(lambda:('page_two_entered','B','metadata',32) in boundary.events)
        clock.value=34.999;runtime.wake()
        assert runtime.drain_results()==()
        clock.value=35;runtime.wake()
        result=collect(runtime,boundary,lambda rows:bool(rows))
        assert len(result)==1 and not result[0].complete
        assert (result[0].attempt,result[0].started,result[0].deadline,result[0].reason)==(2,30,35,'timeout')
        assert result[0].sources['book'].complete and result[0].sources['book'].checked_at==NOW+timedelta(seconds=30)
        assert all(s.failed_at is None and s.retry_at is None for s in result[0].sources.values())
        boundary.wait(lambda:all(('cancel_received','B',source,35) in boundary.events for source in
            ('metadata','native_rewards','sponsored_rewards')))
        boundary.wait(lambda:all(('finally_exited','B',source,35) in boundary.events for source in
            ('metadata','native_rewards','sponsored_rewards')))
        # A duplicate collection does not repeat the same attempt failure.
        runtime.reconcile(());assert runtime.drain_results()==()
    finally:
        boundary.page_one_release.set();boundary.release.set();runtime.reconcile(());assert runtime.close(4)

def test_loopback_cancellation_closes_response_before_retry():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import socket
    import json
    clock=Clock();changed=threading.Condition();events=[];stop=threading.Event()
    def record(event,order):
        with changed:events.append((event,order,clock()));changed.notify_all()
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_GET(self):
            order=self.headers['x-lp-order'];record('entered',order)
            content=json.dumps(book()).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(content)));self.end_headers()
            with changed:number=sum(e[0:2]==('entered',order) for e in events)
            if order=='A' and number==1:
                self.wfile.write(content[:1]);self.wfile.flush()
                self.connection.settimeout(.05)
                while not stop.is_set():
                    try:
                        if not self.connection.recv(1):record('peer_closed',order);break
                    except socket.timeout:continue
                    except OSError:record('peer_closed',order);break
                self.close_connection=True
            else:self.wfile.write(content);self.wfile.flush();record('complete',order)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    class Observe:
        def __init__(self):self.changed=changed;self.events=events
    observer=Observe()
    def factory(d):
        client=httpx.AsyncClient(base_url=f'http://127.0.0.1:{server.server_port}',
            limits=httpx.Limits(max_connections=3,max_keepalive_connections=3),trust_env=False)
        return LPAsyncSourcePort(dict(clob=client),clock=clock,utc_now=clock.utc)
    runtime=LPReadRuntime(factory,clock=clock,utc_now=clock.utc);runtime.start()
    try:
        runtime.reconcile((demand('A',sources=('book',)),demand('B',sources=('book',))))
        results=collect(runtime,observer,lambda rows:any(r.demand.order_id=='B' for r in rows))
        assert next(r for r in results if r.demand.order_id=='B').complete
        deadline=time.monotonic()+4
        with changed:
            while not any(e[0:2]==('entered','A') for e in events):
                remaining=deadline-time.monotonic();assert remaining>0;changed.wait(remaining)
        clock.value=5;runtime.wake()
        expired=next(r for r in collect(runtime,observer,lambda rows:bool(rows)) if r.demand.order_id=='A')
        assert not expired.complete
        with changed:
            while not any(e[0:2]==('peer_closed','A') for e in events):
                remaining=deadline-time.monotonic();assert remaining>0,events;changed.wait(remaining)
        retried=collect(runtime,observer,lambda rows:any(r.demand.order_id=='A' and r.complete for r in rows))
        assert next(r for r in retried if r.demand.order_id=='A').attempt>expired.attempt
        assert sum(e[0:2]==('entered','A') for e in events)==2
        # The actual second request follows the peer's real disconnect.
        if sum(e[0:2]==('entered','A') for e in events)>1:
            first_close=next(i for i,e in enumerate(events) if e[0:2]==('peer_closed','A'))
            second_enter=[i for i,e in enumerate(events) if e[0:2]==('entered','A')][1]
            assert first_close<second_enter
    finally:
        runtime.reconcile(());stop.set();assert runtime.close(4)
        server.shutdown();server.server_close();thread.join(4)


def test_cleanup_fault_is_supervised_outside_market_loop():
    for mode in ('swallow','block'):
        clock=Clock();boundary=Boundary(clock)
        boundary.holds={('A','book')};boundary.mode[('A','book')]=mode
        runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc);runtime.start()
        try:
            runtime.reconcile((demand('A'),demand('B'),
                demand('O',kind='financial',sources=('balance',)),
                demand('exact',kind='exact',sources=('order',))))
            boundary.wait(lambda:any(e[:3]==('entered','A','book') for e in boundary.events))
            first=collect(runtime,boundary,lambda rows:{'O','exact'}<={r.demand.order_id for r in rows})
            assert all(r.complete for r in first if r.demand.order_id in ('O','exact'))
            if mode=='swallow':
                first+=collect(runtime,boundary,lambda rows:any(r.demand.order_id=='B' for r in rows)) if not any(r.demand.order_id=='B' for r in first) else []
                assert next(r for r in first if r.demand.order_id=='B').complete
            clock.value=5;runtime.wake()
            expired=collect(runtime,boundary,lambda rows:any(r.demand.order_id=='A' for r in rows))
            assert next(r for r in expired if r.demand.order_id=='A').reason=='timeout'
            clock.value=6;runtime.wake()
            fault=collect(runtime,boundary,lambda rows:any(r.demand.order_id=='A' and r.cleanup_fault for r in rows))
            assert next(r for r in fault if r.demand.order_id=='A').cleanup_fault
            status=runtime.status()
            assert status['owned_buy_scopes']==2 and status['cleanup_faults']>=1
            assert not runtime.close(0)
            assert sum(e[:3]==('entered','A','book') for e in boundary.events)==1
        finally:
            boundary.release.set();runtime.reconcile(());assert runtime.close(4)


def test_normal_failure_and_cooling_follow_start_based_cadence():
    for mode,next_start in (('success',5),('failure',5),('cooling',12),('timeout',5.4)):
        clock=Clock();boundary=Boundary(clock)
        boundary.mode[('A','book')]='swallow' if mode=='timeout' else mode
        if mode in ('timeout','success','failure'):boundary.holds={('A','book')}
        runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
        runtime.start(lambda:boundary.record('wakeup','',''))
        try:
            runtime.reconcile((demand('A',sources=('book',)),))
            boundary.wait(lambda:any(e[:3]==('entered','A','book') for e in boundary.events))
            if mode=='timeout':
                clock.value=5;runtime.wake()
                assert not collect(runtime,boundary,lambda rows:bool(rows))[0].complete
                boundary.wait(lambda:any(e[:3]==('cancel_received','A','book') for e in boundary.events))
                clock.value=5.4;boundary.release.set()
            else:
                if mode in ('success','failure'):
                    clock.value=.1;boundary.release.set()
                result=collect(runtime,boundary,lambda rows:bool(rows))[0]
                assert result.complete==(mode=='success')
                if mode in ('success','failure'):assert result.completed==.1
                clock.value=next_start-.001
                runtime.wake()
                boundary.wait(lambda:runtime.status().get('observed_monotonic',-1)>=clock())
                assert runtime.status()['scopes'][0]['attempt']==1
                assert sum(e[:3]==('entered','A','book') for e in boundary.events)==1
                clock.value=next_start
            boundary.mode[('A','book')]='success';boundary.holds.clear();runtime.wake()
            boundary.wait(lambda:sum(e[:3]==('entered','A','book') for e in boundary.events)==2)
            starts=[e[3] for e in boundary.events if e[:3]==('entered','A','book')]
            assert starts==[0,next_start]
            # Result not yet ACKed: neither wake nor reconcile can start a third.
            clock.value=next_start+20;runtime.wake()
            boundary.wait(lambda:runtime.status().get('observed_monotonic',-1)>=clock())
            assert sum(e[:3]==('entered','A','book') for e in boundary.events)==2
        finally:
            boundary.release.set();runtime.reconcile(());assert runtime.close(4)

def test_reference_cache_shares_only_completed_facts_without_renewing_time():
    clock=Clock();boundary=Boundary(clock)
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
    runtime.start(lambda:boundary.record('wakeup','',''))
    try:
        runtime.reconcile((demand('A'),))
        original=collect(runtime,boundary,lambda rows:bool(rows))[0]
        assert original.complete
        runtime.reconcile((demand('A'),demand('B')))
        second=collect(runtime,boundary,lambda rows:any(r.demand.order_id=='B' for r in rows))
        b=next(r for r in second if r.demand.order_id=='B')
        assert b.sources['metadata'].source_id==original.sources['metadata'].source_id
        assert b.sources['book'].source_id!=original.sources['book'].source_id
        for t in (5,25):
            clock.value=t;runtime.wake()
            rows=collect(runtime,boundary,lambda rows:{'A','B'}<={r.demand.order_id for r in rows})
            assert all(r.complete and r.sources['metadata'].checked_at==NOW for r in rows)
            assert not any(e[0]=='entered' and e[2]=='metadata' and e[3]==t for e in boundary.events)
        boundary.mode[('A','metadata')]='failure';boundary.mode[('B','metadata')]='failure'
        clock.value=30;runtime.wake()
        rows=collect(runtime,boundary,lambda rows:{'A','B'}<={r.demand.order_id for r in rows})
        assert all(not r.complete for r in rows)
        assert any(e[0]=='entered' and e[2]=='metadata' and e[3]==30 for e in boundary.events)
        for t in (60,65):
            clock.value=t;runtime.wake()
            rows=collect(runtime,boundary,lambda rows:{'A','B'}<={r.demand.order_id for r in rows})
            assert all(not r.complete and not r.sources['metadata'].complete for r in rows)
            assert any(e[0]=='entered' and e[2]=='metadata' and e[3]==t for e in boundary.events)
        # A completed reference may be cached before another branch times out;
        # a reference that returns after the deadline must never enter the cache.
        runtime.reconcile(());assert runtime.close(4)
        for source in ('book','metadata'):
            c=Clock();bdy=Boundary(c);bdy.holds={('A',source)}
            r=LPReadRuntime(bdy.factory,clock=c,utc_now=c.utc);r.start()
            try:
                r.reconcile((demand('A'),))
                bdy.wait(lambda:all(any(e[:3]==('entered','A',name) for e in bdy.events)
                    for name in ('book','metadata','native_rewards','sponsored_rewards')))
                # Wait for the independently healthy reference to finish its HTTP.
                if source=='book':bdy.wait(lambda:sum(e[:3]==('finally_exited','A','metadata') for e in bdy.events)>=2)
                c.value=5;r.wake();assert not collect(r,bdy,lambda rows:bool(rows))[0].complete
                bdy.release.set();r.reconcile((demand('B'),))
                value=collect(r,bdy,lambda rows:any(v.demand.order_id=='B' for v in rows))
                selected=next(v for v in value if v.demand.order_id=='B')
                assert selected.complete
                assert (selected.sources['metadata'].checked_at==NOW)==(source=='book')
            finally:bdy.release.set();r.reconcile(());assert r.close(4)
    finally:
        boundary.release.set();runtime.reconcile(());assert runtime.close(4)

def test_retiring_resources_mailboxes_and_cache_are_bounded():
    clock=Clock();boundary=Boundary(clock)
    boundary.holds={('A','book'),('B','book')}
    for key in boundary.holds:boundary.mode[key]='swallow'
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,buy_cap=2,cache_entries=2,cache_bytes=2048)
    runtime.start(lambda:boundary.record('wakeup','',''))
    try:
        runtime.reconcile((demand('A'),demand('B')))
        boundary.wait(lambda:all(any(e[:3]==('entered',key,'book') for e in boundary.events) for key in ('A','B')))
        clock.value=5;runtime.wake()
        initial=collect(runtime,boundary,lambda rows:{'A','B'}<={r.demand.order_id for r in rows})
        assert all(not r.complete for r in initial)
        clock.value=6;runtime.wake()
        boundary.wait(lambda:runtime.status()['cleanup_faults']==2)
        next_demands=(demand('C'),replace(demand('A'),generation='new'),
            demand('O',kind='financial',sources=('balance',)),demand('exact',kind='exact',sources=('order',)))
        for _ in range(5):runtime.reconcile(next_demands)
        boundary.wait(lambda:runtime.status().get('deferred_count')==2 and runtime.status().get('retiring_buy_scopes')==2)
        status=runtime.status()
        assert status['owned_buy_scopes']==2 and status['retiring_buy_scopes']==2
        assert status['ready_buy_count']==2 and status['ready_count']<=5 and status['new_buy_allowed'] is False
        assert status['pool_count']<=9 and status['connection_limit']==17
        assert status['managed_tasks']<=48 and status['cache_entries']<=2 and status['cache_bytes']<=2048
        before=list(boundary.events)
        assert not any(e[0]=='entered' and e[1]=='C' for e in before)
        # Fault control events survive the old final result's ACK and repeated reconciles.
        faults=runtime.drain_results()
        assert {r.demand.order_id for r in faults if r.cleanup_fault}=={'A','B'}
        boundary.release.set()
        rows=collect(runtime,boundary,lambda rows:{'C','A'}<={r.demand.order_id for r in rows if r.complete})
        assert all(r.demand.generation=='new' for r in rows if r.demand.order_id=='A' and r.complete)
        status=runtime.status()
        assert status['owned_buy_scopes']==2 and status['deferred_count']==0
        assert status['cache_entries']<=2 and status['cache_bytes']<=2048
        from open_trader.polymarket_lp_read_runtime import BUY_SCOPE_CAP,REFERENCE_CACHE_ENTRIES,REFERENCE_CACHE_BYTES
        assert (BUY_SCOPE_CAP,REFERENCE_CACHE_ENTRIES,REFERENCE_CACHE_BYTES)==(64,256,8*1024*1024)
    finally:boundary.release.set();runtime.reconcile(());runtime.drain_results();assert runtime.close(4)


def test_recreated_buy_scope_keeps_monotonic_attempt_identity():
    from test_lp_async_state_contract import state, financial, row
    clock=Clock();owner=state(clock);boundary=Boundary(clock)
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc);runtime.start()
    key=('account',CID,'yes','A');saved=[]
    try:
        financial(owner,clock,(row('A'),))
        runtime.reconcile((demand('A'),))
        for stamp in (0,5,10):
            clock.value=stamp;runtime.wake()
            result=collect(runtime,boundary,lambda rows:bool(rows))[0]
            assert result.complete and owner.apply_market_attempt(result)=='applied'
            saved.append(result)
        clock.value=11;assert financial(owner,clock,()).status=='accepted'
        runtime.reconcile(())
        boundary.wait(lambda:runtime.status()['owned_buy_scopes']==0)
        assert runtime.status()['pool_count']==0  # actual client/scope retirement
        clock.value=15;assert financial(owner,clock,(row('A'),)).status=='accepted'
        assert owner.snapshot().active_orders[0].terms_version==saved[-1].demand.terms_version
        runtime.reconcile((demand('A'),))
        returned=collect(runtime,boundary,lambda rows:bool(rows))[0]
        assert returned.complete and returned.attempt>saved[-1].attempt
        assert owner.apply_market_attempt(returned)=='applied'
        assert owner.snapshot().yields[key].yield_pct_per_hour.state=='known'
        assert owner.snapshot().read_failures[key].consecutive_failures==0
        assert runtime.status()['owned_buy_scopes']==1<=runtime.status()['policy']['buy_cap']==64
        clock.value=19;runtime.wake()
        boundary.wait(lambda:runtime.status()['observed_monotonic']==19)
        assert len([e for e in boundary.events if e[:3]==('entered','A','book')])==4
        boundary.mode[('A','book')]='failure'
        clock.value=20;runtime.wake()
        failed=collect(runtime,boundary,lambda rows:bool(rows))[0]
        assert not failed.complete and failed.started==20 and failed.attempt>returned.attempt
        assert owner.apply_market_attempt(failed)=='applied'
        current=owner.snapshot()
        assert current.yields[key].yield_pct_per_hour.state=='unknown'
        assert current.read_failures[key].consecutive_failures==1
        for old in (*saved,returned):
            assert owner.apply_market_attempt(old)=='stale'
            assert owner.snapshot()==current
        boundary.mode[('A','book')]='success'
        clock.value=25;runtime.wake()
        healthy=collect(runtime,boundary,lambda rows:bool(rows))[0]
        assert healthy.complete and healthy.started==25 and healthy.attempt>failed.attempt
        assert owner.apply_market_attempt(healthy)=='applied'
        fresh=owner.snapshot()
        assert fresh.read_failures[key].consecutive_failures==0
        assert fresh.yields[key].yield_pct_per_hour.state=='known'
        assert fresh.observation.read_ticket==current.observation.read_ticket
        assert fresh.new_capacity==current.new_capacity  # market work grants no financial authority
        enters=[e[3] for e in boundary.events if e[:3]==('entered','A','book')]
        assert enters==[0,5,10,15,20,25]
        for stamp in enters:
            assert len([e for e in boundary.events if e[:3]==('finally_exited','A','book') and e[3]==stamp])==1
    finally:
        boundary.release.set();runtime.reconcile(())
        assert runtime.close(4)
