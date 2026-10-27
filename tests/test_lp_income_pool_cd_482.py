"""#482 C+D public contracts. External exchange and HTTP stay offline."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import socket

import pytest

from tests.test_lp_observed_pools import AsyncPoolRig
from tests.test_lp_income_pool_ab_482 import _roles


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('#482 tests must not connect to a network')
    monkeypatch.setattr(socket.socket,'connect',forbidden)


HOUTHI=(('.11','100'),('.10','248.8'),('.08','103.88'),('.02','5'))


def _thin(rig,name,*,bids=HOUTHI):
    from polymarket.models.clob.order_book import OrderBookLevel
    rig.market(name,minimum='8')  # literal 20-share minimum
    row=rig.markets[name]
    row['price']=Decimal('.11')
    row['direction']['book'].update(bids=[dict(price=p,size=q) for p,q in bids],asks=[dict(price='.12',size='10000')])
    original=rig.public.get_order_book
    def read(*,token_id):
        value=original(token_id=token_id)
        if token_id!='t'+name:return value
        return value.model_copy(update=dict(bids=tuple(OrderBookLevel(price=Decimal(p),size=Decimal(q)) for p,q in bids),
            asks=(OrderBookLevel(price=Decimal('.12'),size=Decimal('10000')),)))
    rig.public.get_order_book=read
    # Update the public qualification fact through the existing fixture seed seam.
    rig.producer._candidate_pool_record_success(rig.cid(name),dict(condition_id=rig.cid(name),state='eligible',
        selected_direction=dict(token_id='t'+name)),judged_at=rig.now,
        facts=dict(checked_at=rig.now,directions=[row['direction']]),global_recovery_generation=7)


def test_both_observed_pools_ignore_stress_gate_but_record_estimate(tmp_path,monkeypatch):
    from tests.test_polymarket_lp_risk import _direction,_account,NOW
    from open_trader.polymarket_lp_risk import evaluate_lp_entry
    direction=_direction()
    direction['book'].update(bids=[dict(price=p,size=q) for p,q in HOUTHI],asks=[dict(price='.12',size='10000')])
    rejected=evaluate_lp_entry(direction,account=_account(),now=NOW,quantity=Decimal('454.54'))
    assert rejected['state']=='rejected' and rejected['reason_codes']==['exit_liquidity_insufficient']
    for pool in ('income','trial'):
        rig=AsyncPoolRig(tmp_path/pool,monkeypatch)
        try:
            _thin(rig,'A',bids=HOUTHI if pool=='income' else (('.11','100'),('.10','1')))
            if pool=='income':
                rig.add_order('A','8');rig.sdk.orders[0]['price']=Decimal('.11')
                rig.measure({'A':'1'})
                first=rig.cycle(pool='income',qualified=True);rig.finish_cancels(first)
                assert rig.sdk.cancels==[('old-A',)]
                rig.release('old-A')
            result=rig.cycle(pool=pool,qualified=True)
            assert len(rig.sdk.posts)==1,(pool,result,rig.execution.lp_auto_state())
            sent=rig.sdk.posts[0]
            assert sent['price']==Decimal('.11')
            assert sent['size']==Decimal('454.54' if pool=='income' else '20')
            assert sent['price']*sent['size']==Decimal('49.9994' if pool=='income' else '2.20')
            if pool=='income':
                estimate,=rig.execution.lp_auto_state()['observed_pools']['income_estimates']
                assert estimate['quantity']=='454.54'
                assert estimate['stress_exit']['reason_codes']==['exit_liquidity_insufficient']
                assert estimate['stress_exit']['loss_ratio'] is None
                assert rig.execution.lp_auto_state()['observed_pools']['income_gaps']['items']==[]
        finally:rig.close()
    bypass=evaluate_lp_entry(direction,account=_account(),now=NOW,quantity=Decimal('454.54'),stress_exit_check=False)
    assert bypass['state']=='eligible'
    assert bypass['guidance']['estimated_exit_loss_ratio'] is None
    assert bypass['guidance']['stress_exit']['reason_codes']==['exit_liquidity_insufficient']
    assert evaluate_lp_entry(direction,account=_account(),now=NOW,quantity=Decimal('454.54'))==rejected


def test_observed_candidate_production_and_maintenance_keep_thin_markets(tmp_path,monkeypatch):
    from tests.test_lp_candidate_current_authority import current_runtime,produce
    from tests.test_lp_order_registration_contract import CONDITION_ID
    from polymarket.models.clob.order_book import OrderBookLevel
    for mode in ('observed_pools','minimum','unavailable'):
        with current_runtime(tmp_path/mode,monkeypatch,prime=False) as (store,producer,live,execution,sdk,clock):
            settings=store.lp_load_current_settings(live._settings_account())
            store.lp_save_current_settings(live._settings_account(),expected_config_version=settings['config_version'],
                changes=dict(allocation_mode='observed_pools'))
            live.load_settings()
            public=live.trading._public_client_factory()
            original=public.get_order_book
            public.get_order_book=lambda **kw:original(**kw).model_copy(update=dict(bids=(
                OrderBookLevel(price=Decimal('.40'),size=Decimal('1000')),
                OrderBookLevel(price=Decimal('.39'),size=Decimal('1')))))
            if mode=='minimum':
                settings=store.lp_load_current_settings(live._settings_account())
                store.lp_save_current_settings(live._settings_account(),expected_config_version=settings['config_version'],changes=dict(allocation_mode='minimum'))
                live.load_settings()
            if mode=='unavailable':
                # A policy reader is an explicitly bound public boundary, not a trading bypass.
                producer.set_candidate_allocation_mode_reader(lambda:None)
            produce(producer)
            capture=producer.capture_qualified_pool()
            assert (CONDITION_ID in capture.rows)==(mode=='observed_pools'),producer.candidate_snapshot()
            if mode=='observed_pools':
                clock[0]+=timedelta(seconds=31)
                producer.refresh_candidate_recommendations()
                assert CONDITION_ID in producer.capture_qualified_pool().rows
                execution.lp_auto_scheduled_check()  # original empty startup cleanup
                execution.lp_auto_scheduled_check()  # income turn has no measured source
                execution.lp_auto_scheduled_check()  # next trial turn consumes the qualified row
                assert len(sdk.posts)==1 and sdk.posts[0]['size']==20
            else:
                assert sdk.posts==[]


def test_stress_bypass_preserves_other_entry_guards():
    from tests.test_polymarket_lp_risk import _direction,_account,NOW
    from open_trader.polymarket_lp_risk import evaluate_lp_entry
    for case in ('book_unknown','identity','stale','minimum','balance','allowance','holding','reward','tick','spread'):
        direction=_direction();account=_account();quantity=Decimal('20')
        if case=='book_unknown':direction['book']=None
        elif case=='identity':direction['book']['token_id']='other-token'
        elif case=='stale':direction['book']['received_at']=NOW-timedelta(seconds=10,microseconds=1)
        elif case=='minimum':quantity=Decimal('19.99')
        elif case in ('balance','allowance'):account[case]=Decimal('10.19') #20*.51 = 10.20
        elif case=='holding':account['positions']=[dict(condition_id='condition-a',token_id='token-yes',size='1')]
        elif case=='reward':direction['reward_checked_at']=NOW-timedelta(seconds=60,microseconds=1)
        elif case=='tick':direction['market']['tick_size']=Decimal('.02')
        elif case=='spread':
            direction['market']['reward_max_spread']=Decimal('.001')
            direction['book']['asks']=[dict(price='.54',size='100')]
        outcome=evaluate_lp_entry(direction,account=account,now=NOW,quantity=quantity,stress_exit_check=False)
        assert outcome['state']!='eligible' and outcome['guidance'] is None,(case,outcome)
    direction=_direction();direction['book']['received_at']=NOW-timedelta(seconds=10)
    assert evaluate_lp_entry(direction,account=_account(),now=NOW,stress_exit_check=False)['state']=='eligible'


def test_income_stress_bypass_keeps_fixed_budget_weights(tmp_path,monkeypatch):
    from polymarket.models.clob.order_book import OrderBookLevel
    for count in (1,2,3):
        rig=AsyncPoolRig(tmp_path/str(count),monkeypatch)
        try:
            names='ABC'[:count]
            for name in names:
                rig.market(name)
                rig.markets[name]['direction']['book']['bids']=[dict(price='.4',size='10000'),dict(price='.39',size='1')]
                rig.add_order(name,'1')
            book=rig.public.get_order_book
            rig.public.get_order_book=lambda **kw:book(**kw).model_copy(update=dict(bids=(
                OrderBookLevel(price=Decimal('.4'),size=Decimal('10000')),
                OrderBookLevel(price=Decimal('.39'),size=Decimal('1')))))
            rig.measure({n:v for n,v in zip(names,('1','.8','.6'))})
            admission=rig.cycle(pool='income');rig.finish_cancels(admission)
            assert {oid for batch in rig.sdk.cancels for oid in batch}=={'old-'+n for n in names}
            rig.release(*('old-'+n for n in names))
            rig.cycle(pool='income')
            assert [(p['token_id'],p['size']*p['price']) for p in rig.sdk.posts]==[
                ('t'+n,Decimal(v)) for n,v in zip(names,('50','30','20'))]
            assert sum(p['size']*p['price'] for p in rig.sdk.posts)==Decimal(('50','80','100')[count-1])
            assert rig.sdk.market_orders==[]
        finally:rig.close()


def test_income_reads_start_concurrently_while_low_is_busy():
    from tests.test_lp_async_read_runtime_contract import Boundary,Clock,collect,demand
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    clock=Clock();boundary=Boundary(clock);clients=[]
    def factory(d):
        port=boundary.factory(d);clients.extend(port.clients.values());return port
    boundary.holds={('P','book'),('S','book')}
    runtime=LPReadRuntime(factory,clock=clock,utc_now=clock.utc)
    runtime.start()
    try:
        orders=[demand('P',kind='position',sources=('book',)),demand('S',kind='sell',sources=('book',)),
            demand('I1',kind='income',sources=('book',)),demand('I2',kind='income',sources=('book',)),
            demand('B',sources=('book',))]
        runtime.reconcile(orders)
        results=collect(runtime,boundary,lambda rows:{'I1','I2','B'}<={r.demand.order_id for r in rows})
        assert all(r.complete for r in results)
        assert ('entered','P','book',0) in boundary.events
        assert not boundary.release.is_set()
        assert ('finally_exited','P','book',0) not in boundary.events
        # Retire the still-held position. SELL becomes the next low owner.
        orders=orders[1:]+[demand('I3',kind='income',sources=('book',))]
        runtime.reconcile(orders)
        more=collect(runtime,boundary,lambda rows:any(r.demand.order_id=='I3' and r.complete for r in rows))
        boundary.wait(lambda:('entered','S','book',0) in boundary.events)
        assert not boundary.release.is_set()
        assert ('finally_exited','S','book',0) not in boundary.events
        assert any(r.demand.order_id=='I3' and r.complete for r in more)
    finally:
        boundary.release.set();runtime.reconcile(());assert runtime.close(4)
        assert clients and all(c.is_closed for c in clients)


def test_buy_income_share_64_cap_and_wait_order():
    import asyncio,threading,httpx
    from tests.test_lp_async_read_runtime_contract import Boundary,Clock,demand
    from tests.test_lp_async_source_contract import book
    from open_trader.polymarket_lp_async_source import LPAsyncSourcePort
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    clock=Clock();boundary=Boundary(clock);clients=[]
    close_entered=threading.Event();close_release=threading.Event()
    def factory(d):
        class Client(httpx.AsyncClient):
            async def aclose(self):
                if d.order_id=='B0':
                    close_entered.set()
                    await asyncio.get_running_loop().run_in_executor(None,close_release.wait)
                await super().aclose()
        async def http(req):
            boundary.record('entered',d.order_id,'book')
            await asyncio.Event().wait()  # cancellation, not an executor slot, owns the held HTTP read
            return httpx.Response(200,json=dict(book(),market=d.condition_id,asset_id=d.token_id))
        owned={n:Client(transport=httpx.MockTransport(http),base_url='http://127.0.0.1',trust_env=False) for n in ('gamma','clob')}
        clients.extend((d.order_id,c) for c in owned.values())
        return LPAsyncSourcePort(owned,clock=clock,utc_now=clock.utc)
    runtime=LPReadRuntime(factory,clock=clock,utc_now=clock.utc)
    runtime.start()
    try:
        original=[demand('B'+str(i),sources=('book',)) for i in range(63)]+[
            demand('Iold',kind='income',sources=('book',)),demand('Iwait',kind='income',sources=('book',))]
        runtime.reconcile(original)
        boundary.wait(lambda:sum(e[0]=='entered' for e in boundary.events)>=64)
        status=runtime.status()
        assert len(status['scopes'])==status['owned_buy_scopes']==64
        assert status['deferred_order_ids']==('Iwait',) and status['policy']['buy_cap']==64
        assert not status['new_buy_allowed'] and status['pool_count']==128
        newer=demand('Bnew',sources=('book',))
        retained=original[1:]+[newer]
        runtime.reconcile(retained)
        assert close_entered.wait(4),'actual B0 port close watchdog'
        status=runtime.status()
        assert status['owned_buy_scopes']==64 and status['retiring_buy_scopes']==1
        assert status['deferred_order_ids']==('Iwait','Bnew')
        assert not any(e[1] in ('Iwait','Bnew') for e in boundary.events)
        close_release.set()
        boundary.wait(lambda:any(e[:3]==('entered','Iwait','book') for e in boundary.events))
        assert all(c.is_closed for oid,c in clients if oid=='B0')
        assert not any(e[1]=='Bnew' for e in boundary.events)
        assert runtime.status()['owned_buy_scopes']==64
        runtime.reconcile([d for d in retained if d.order_id!='B1'])
        boundary.wait(lambda:any(e[:3]==('entered','Bnew','book') for e in boundary.events))
        assert runtime.status()['owned_buy_scopes']==64
    finally:
        close_release.set();boundary.release.set();runtime.reconcile(());assert runtime.close(4)
        assert all(c.is_closed for _,c in clients)


def test_low_fault_does_not_block_income_read_or_relax_send_gate():
    from tests.test_lp_async_read_runtime_contract import Clock,collect,demand
    from tests.test_lp_low_read_recovery import LowBoundary,low
    from tests.test_lp_financial_read_recovery import set_time,financial
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    clock=Clock();boundary=LowBoundary(clock)
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
    runtime.start()
    try:
        held=low('position')
        runtime.reconcile((held,))
        boundary.wait(lambda:boundary.low_requests==[('L','book',0)])
        set_time(runtime,boundary,clock,5)
        assert boundary.low_close_entered.wait(4)
        expired,=collect(runtime,boundary,bool)
        assert not expired.complete and expired.reason=='timeout'
        old_time=expired.sources['book'].checked_at
        set_time(runtime,boundary,clock,6)
        fault,=collect(runtime,boundary,bool)
        assert fault.cleanup_fault
        assert runtime.status()['new_buy_block_reasons']==('cleanup_fault',)
        runtime.reconcile((held,demand('income',kind='income',sources=('book',)),financial()))
        ready=collect(runtime,boundary,lambda rows:{'income','O'}<={r.demand.order_id for r in rows})
        assert all(r.complete for r in ready)
        assert not runtime.status()['new_buy_allowed']
        assert not boundary.low_close_exited.is_set()
        boundary.low_close_release.set()
        assert boundary.low_close_exited.wait(4)
        boundary.wait(lambda:runtime.status()['cleanup_faults']==0)
        assert runtime.status()['new_buy_allowed']
        assert expired.reason=='timeout' and not expired.complete
        assert expired.sources['book'].checked_at==old_time
    finally:
        boundary.low_close_release.set();runtime.reconcile(());assert runtime.close(4)
        assert all(c.is_closed for c in boundary.low_clients+boundary.clients)


def test_income_read_deadline_cleanup_and_threads_match_buy_contract():
    import asyncio,threading
    from tests.test_lp_async_read_runtime_contract import Clock,collect,demand
    from tests.test_lp_low_read_recovery import LowBoundary
    from tests.test_lp_financial_read_recovery import set_time
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    from open_trader.polymarket_lp_monitor import read_health
    clock=Clock();boundary=LowBoundary(clock)
    port_close_entered=threading.Event();port_close_release=threading.Event()
    def factory(d):
        port=boundary.factory(d)
        if d.kind=='income':
            for client in port.clients.values():
                close=client.aclose
                async def external_close(close=close):
                    port_close_entered.set()
                    await asyncio.get_running_loop().run_in_executor(None,port_close_release.wait)
                    await close()
                client.aclose=external_close
        return port
    runtime=LPReadRuntime(factory,clock=clock,utc_now=clock.utc,buy_cap=1)
    runtime.start()
    names={'lp-market-io','lp-account-exact-io','lp-read-deadlines'}
    threads={t.name:t.ident for t in threading.enumerate() if t.name in names}
    try:
        assert set(threads)==names
        income=demand('L',kind='income',sources=('book',))
        peer=demand('peer',sources=('book',))
        runtime.reconcile((income,peer))
        boundary.wait(lambda:boundary.low_requests==[('L','book',0)])
        set_time(runtime,boundary,clock,4.999)
        assert runtime.drain_results()==()
        set_time(runtime,boundary,clock,5)
        assert boundary.low_close_entered.wait(4)
        expired,=collect(runtime,boundary,bool)
        assert expired.reason=='timeout' and not expired.complete
        assert expired.deadline==5 and expired.started==0
        assert expired.demand.target_order_id=='' and tuple(expired.sources)==('book',)
        set_time(runtime,boundary,clock,6)
        fault,=collect(runtime,boundary,bool)
        assert fault.cleanup_fault and fault.attempt==expired.attempt
        status=runtime.status()
        assert status['owned_buy_scopes']==1 and status['deferred_order_ids']==('peer',)
        detail,=status['cleanup_fault_details']['items']
        assert detail['lane']=='buy' and detail['kind']=='income' and detail['blocks_new_buy'] is False
        health=read_health(dict(diagnostics=dict(async_reads=status)),clock.utc())
        assert health['status']=='attention' and health['cleanup_fault_details']['total']==1
        assert 'cleanup_fault' not in status['new_buy_block_reasons']
        assert boundary.low_requests==[('L','book',0)]
        runtime.reconcile((peer,))
        assert runtime.status()['owned_buy_scopes']==1
        boundary.low_close_release.set()
        assert boundary.low_close_exited.wait(4)
        assert port_close_entered.wait(4)
        status=runtime.status()
        assert status['owned_buy_scopes']==1 and status['retiring_buy_scopes']==1
        assert status['deferred_order_ids']==('peer',)
        assert not any(e[1]=='peer' for e in boundary.events)
        port_close_release.set()
        ready=collect(runtime,boundary,lambda rows:any(r.demand.order_id=='peer' for r in rows))
        assert ready[0].complete and ready[0].demand.order_id=='peer'
        assert all(c.is_closed for c in boundary.low_clients)
        assert {t.name:t.ident for t in threading.enumerate() if t.name in names}==threads
        assert runtime.drain_results()==()
    finally:
        boundary.low_close_release.set();port_close_release.set();runtime.reconcile(());assert runtime.close(4)
        assert all(c.is_closed for c in boundary.low_clients)
        assert not any(t.ident in threads.values() for t in threading.enumerate())
