"""Public live-owner contracts with real runtime and offline HTTP boundaries."""
from test_lp_async_source_contract import independent_watchdog
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import threading
import time
from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
from open_trader.polymarket_lp_market_calculation import CalculationPolicy
from open_trader.polymarket_lp_market_reader import CapturedSources, MarketReader, SourceResult
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_lp_async_read_runtime_contract import Clock, Boundary, demand, collect, LPReadRuntime, CID, NOW

class Trading:
    config=SimpleNamespace(wallet_address='account')
    def __init__(self):self.cancels=[];self.cancelled=threading.Event()
    def lp_reader_source_port(self):
        class Port:
            def read(self,component,scope):
                if component=='balance':return (100,100)
                if component in ('open_orders','positions'):return ()
                raise ValueError('offline_no_legacy_reads')
        return Port()
    def cancel_orders_detailed(self,ids):
        self.cancels.append(ids);self.cancelled.set();raise TimeoutError('unknown_native_outcome')


def wire_row(order='A',matched='0',side='BUY'):
    return dict(id=order,market=CID,asset_id='yes',side=side,original_size='20',size_matched=matched,
        price='0.40',status='LIVE',owner='credential-id',maker_address='account',outcome='Yes',
        expiration='0',order_type='GTC',created_at=0)

def live_owner(tmp_path):
    clock=Clock();boundary=Boundary(clock);boundary.orders=[wire_row('A'),dict(wire_row('B'),market='0x'+'2'*64,asset_id='b')]
    trading=Trading();store=PredictionArbitrageStore(tmp_path)
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
    live=LPD2LiveExecution(store,trading,account_id='account',venue='polymarket',process_epoch='epoch',
        policy=CalculationPolicy(Decimal(100)),read_runtime=runtime,clock=clock,utc_now=clock.utc)
    live.initialize(ValidatedStartAdmission('epoch','offline-permit'))
    return clock,boundary,trading,live

def advance(live,boundary,clock,t,predicate):
    clock.value=t
    if live.read_runtime:live.read_runtime.wake()
    end=time.monotonic()+4
    def settled():
        snapshot=live.state.snapshot();observation=snapshot.observation
        return bool(observation is not None and observation.orders_complete
            and observation.order_view.orders_source.checked_at==clock.utc()
            and all(o.key in snapshot.read_failures and o.key in snapshot.yields
                and snapshot.yields[o.key].evaluated_at==clock.utc()
                for o in snapshot.active_orders if o.side=='BUY'))
    while not (predicate() and settled()):
        live.run_once()
        if predicate() and settled():break
        left=end-time.monotonic();assert left>0,(live.state.snapshot(),boundary.events)
        with boundary.changed:boundary.changed.wait(min(left,.01))

def count(live,order='A'):
    value=live.state.snapshot().read_failures.get(('account',CID,'yes',order))
    return value.consecutive_failures if value else 0

def test_eleventh_failure_latches_exact_cancel_and_success_resets_before_latch(tmp_path):
    for reset in (False,True):
        clock,boundary,trading,live=live_owner(tmp_path/str(reset))
        boundary.mode[('A','native_rewards')]='failure'
        try:
            for number in range(1,10):
                advance(live,boundary,clock,(number-1)*5,lambda:count(live)==number)
                assert trading.cancels==[] and count(live,'B')==0
            start=45
            if reset:
                boundary.mode[('A','native_rewards')]='success'
                advance(live,boundary,clock,45,lambda:count(live)==0)
                boundary.mode[('A','native_rewards')]='failure';start=75
                for number in range(1,10):
                    advance(live,boundary,clock,start+(number-1)*5,lambda:count(live)==number)
                start+=45
            advance(live,boundary,clock,start,lambda:count(live)==10)
            assert trading.cancels==[]
            advance(live,boundary,clock,start+5,lambda:count(live)==11)
            live.run_protection_once()
            assert trading.cancelled.wait(4), (live.status(), live.state.snapshot().progress)
            assert trading.cancels==[('A',)]
            boundary.mode[('A','native_rewards')]='success'
            advance(live,boundary,clock,start+10,lambda:count(live)==0)
            handles=live.run_protection_once()
            for handle in handles:
                handle.result(4);handle._thread.join(4);assert handle.done()
            assert trading.cancels==[('A',),('A',)]
            for _ in range(3):live.run_once();assert live.run_protection_once()==()
            assert trading.cancels==[('A',),('A',)]
            read=live.state.snapshot().read_failures[('account',CID,'yes','A')]
            assert 'read_failure_limit' in read.reason_codes
        finally:boundary.release.set();assert live.close(4)

def test_read_failure_cancel_needs_no_queue_baseline_and_keeps_exact_guards(tmp_path):
    for trigger in ('eleven','cleanup'):
        clock,boundary,trading,live=live_owner(tmp_path/trigger)
        boundary.book_depth=1000  # Keep original queue ratio above 50%; isolate the new exact reason.
        boundary.orders=[wire_row('A'),wire_row('manual'),wire_row('partial','3'),wire_row('peer'),wire_row('sell',side='SELL')]
        targets={'A','manual','partial'}
        if trigger=='eleven':
            for key in targets:boundary.mode[(key,'book')]='failure'
        else:
            boundary.holds={(key,'book') for key in targets}
            for key in targets:boundary.mode[(key,'book')]='swallow'
        from concurrent.futures import Future
        changed=threading.Condition();entered=set();finished=set()
        releases={key:threading.Event() for key in targets}
        actual_cancel=trading.cancel_orders_detailed
        def held_cancel(ids):
            assert len(ids)==1 and ids[0] in targets
            oid=ids[0]
            with changed:entered.add(oid);changed.notify_all()
            assert releases[oid].wait(4), 'external sender release watchdog'
            try:return actual_cancel(ids)
            finally:
                with changed:finished.add(oid);changed.notify_all()
        trading.cancel_orders_detailed=held_cancel
        schedule=Future();controller=None
        admitted=[False]
        class Lease:
            def allowed(self,action=None):return admitted[0]
            def release(self):pass
        live.bind_mutation_admission(trading,lambda action:Lease() if admitted[0] else None)
        try:
            if trigger=='eleven':
                for number in range(1,12):
                    advance(live,boundary,clock,(number-1)*5,lambda:all(count(live,key)==number for key in targets))
            else:
                live.run_once()
                end=time.monotonic()+4
                while not all(any(e[:3]==('entered',key,'book') for e in boundary.events) for key in targets):
                    live.run_once()
                    left=end-time.monotonic();assert left>0
                    with boundary.changed:boundary.changed.wait(min(.01,left))
                clock.value=5;live.read_runtime.wake()
                end=time.monotonic()+4
                while count(live)<1:
                    live.run_once();assert time.monotonic()<end
                clock.value=6;live.read_runtime.wake()
                end=time.monotonic()+4
                while not all('read_cleanup_fault' in live.state.snapshot().read_failures.get(
                    ('account',CID,'yes',key),SimpleNamespace(reason_codes=())).reason_codes for key in targets):
                    live.run_once();assert time.monotonic()<end
            before=live.state.snapshot()
            assert not any(k.kind=='queue_bucket' for k in before.baselines)
            live.run_protection_once()
            assert trading.cancels==[]  # no mutation permission; obligations remain pending
            assert before.current.effective_running is False
            admitted[0]=True
            def deliver():
                try:
                    end=time.monotonic()+4
                    with changed:
                        while entered!=targets:
                            left=end-time.monotonic();assert left>0;changed.wait(left)
                    assert live.status()['native_inflight']==3
                    if trigger=='eleven':
                        for oid in sorted(targets):
                            releases[oid].set()
                            with changed:
                                while oid not in finished:
                                    left=end-time.monotonic();assert left>0;changed.wait(left)
                    else:
                        for event in releases.values():event.set()
                except BaseException as exc:schedule.set_exception(exc)
                else:schedule.set_result(True)
            controller=threading.Thread(target=deliver,name='external-cancel-delivery')
            controller.start()
            end=time.monotonic()+4
            while len(trading.cancels)<3:
                handles=live.run_protection_once()
                assert handles, 'pending exact target had no admitted native sender'
                for handle in handles:
                    left=end-time.monotonic();assert left>0
                    handle.result(left)
                    handle._thread.join(max(0,end-time.monotonic()))
                    assert not handle._thread.is_alive()
            assert schedule.result(max(0,end-time.monotonic()))
            controller.join(max(0,end-time.monotonic()))
            assert not controller.is_alive() and entered==finished==targets
            assert sorted(trading.cancels)==[('A',),('manual',),('partial',)],repr([(k.kind,p.reason_codes,[t.order_key[3] for t in p.cancel_targets]) for k,p in live.state.snapshot().progress.items()])
            for _ in range(3):live.run_protection_once()
            assert len(trading.cancels)==3
            partial=next(o for o in live.state.snapshot().orders if o.key[3]=='partial')
            assert partial.remaining.value==17 and partial.size_matched.value==3
            assert all('peer' not in ids and 'sell' not in ids for ids in trading.cancels)
        finally:
            for event in releases.values():event.set()
            if controller is not None:controller.join(4)
            boundary.release.set();assert live.close(4)

def test_existing_protections_keep_their_own_thresholds_and_send_ownership(tmp_path,monkeypatch):
    clock,boundary,trading,live=live_owner(tmp_path/'async-queue')
    try:
        advance(live,boundary,clock,0,lambda:bool(live.state.snapshot().read_failures))
        live.run_protection_once()
        boundary.mode[('A','book')]='failure'
        for number in range(1,10):
            advance(live,boundary,clock,number*5,lambda:count(live)==number)
            live.run_protection_once()
            assert trading.cancels==[]
        advance(live,boundary,clock,50,lambda:count(live)==10)
        handles=live.run_protection_once()
        assert trading.cancelled.wait(4)
        assert trading.cancels==[('A',)] and count(live)==10
        for handle in handles:handle.result(4)
        live.run_protection_once();assert trading.cancels==[('A',)]
    finally:boundary.release.set();assert live.close(4)
    from tests.test_lp_d2_integration import _d2_protection_runtime
    owner,sdk,public,times=_d2_protection_runtime(tmp_path/'async-scoring',monkeypatch)
    sdk.get_order_scoring=lambda **kw:False
    boundary,clock=sdk_boundary(owner,sdk,public,times)
    owner.read_runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
    try:
        scope=next(iter(owner.state.snapshot().openings))
        def settled_score():
            scoring=owner.state.snapshot().openings[scope].scoring
            return scoring.checked_at==clock.utc() and not any(s['active'] for s in owner.read_runtime.status()['scopes'])
        pump_live(owner,boundary,settled_score)
        lost=owner.state.snapshot().openings[scope].scoring.lost_since_monotonic
        first=len([e for e in boundary.events if e[0]=='entered' and e[2]=='scoring'])
        times[0]=4;owner.read_runtime.wake();owner.run_once();owner.run_protection_once()
        assert len([e for e in boundary.events if e[0]=='entered' and e[2]=='scoring'])==first
        for stamp in (5,59):
            times[0]=stamp;owner.read_runtime.wake();pump_live(owner,boundary,settled_score)
            opening=owner.state.snapshot().openings[scope]
            assert opening.scoring.lost_since_monotonic==lost and not opening.stop_requested
            assert sdk.cancels==[]
        times[0]=60;owner.read_runtime.wake()
        pump_live(owner,boundary,lambda:owner.state.snapshot().openings[scope].stop_requested and bool(sdk.cancels))
        assert sdk.cancels==[('entry-A',)] and sdk.market_orders==[]
        assert owner.state.snapshot().openings[scope].review_status=='scoring_continuously_lost'
    finally:assert owner.close(4);owner.trading.close()
    # Preserve the existing literal boundary/negative contracts at their public
    # owner seam as invariants; they remain separate from the new read counter.
    from tests.test_lp_d2_integration import (
        test_d2_unknown_yield_protection_cancels_at_600_without_replay,
        test_d2_scoring_review_uses_exact_current_order_and_continuous_window,
        test_d2_queue_cancel_capacity_defers_only_unattempted_targets)
    test_d2_unknown_yield_protection_cancels_at_600_without_replay(tmp_path/'unknown',monkeypatch)
    test_d2_scoring_review_uses_exact_current_order_and_continuous_window(tmp_path/'scoring',monkeypatch)
    for occupied in (False,True):
        test_d2_queue_cancel_capacity_defers_only_unattempted_targets(tmp_path/('capacity-'+str(occupied)),monkeypatch,occupied)

def sdk_wire(value):
    """Serialize external SDK fixtures using their declared wire aliases."""
    from datetime import datetime,date
    if hasattr(type(value),'model_fields'):
        return {field.validation_alias if isinstance(field.validation_alias,str) else name:sdk_wire(getattr(value,name))
            for name,field in type(value).model_fields.items()}
    if isinstance(value,SimpleNamespace):return sdk_wire(vars(value))
    if isinstance(value,dict):return {k:sdk_wire(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [sdk_wire(v) for v in value]
    if isinstance(value,(datetime,date)):return value.isoformat()
    if isinstance(value,Decimal):return str(value)
    return value


def sdk_boundary(live,sdk,public,clock_values):
    import httpx
    from open_trader.polymarket_lp_async_source import LPAsyncSourcePort
    from test_lp_async_source_contract import market,reward
    class BusinessClock:
        def __call__(self):return clock_values[0]
        def utc(self):return live._utc_now()
    clock=BusinessClock();boundary=Boundary(clock)
    def factory(d):
        async def http(req):
            source=req.headers['x-lp-source'];oid=req.headers['x-lp-order'];boundary.record('entered',oid,source)
            try:
                if boundary.mode.get((oid,source))=='failure':return httpx.Response(503)
                if source=='balance':value=sdk_wire(sdk.get_balance_allowance(asset_type='COLLATERAL'))
                elif source=='open_orders':value=dict(data=sdk_wire(sdk.orders),next_cursor='LTE=')
                elif source=='positions':value=[dict(conditionId=r['condition_id'],asset=r['token_id'],
                    size=str(r['size']),avgPrice=str(r['average_price'])) for r in sdk.positions]
                elif source=='book':
                    model=public.get_order_book(token_id=req.url.params['token_id'])
                    value=dict(sdk_wire(model),timestamp=str(int(model.timestamp.timestamp()*1000)))
                elif source=='metadata':
                    value=(dict(events=[dict(id='7',slug='event',title='Event')],next_cursor=None)
                        if req.url.path=='/events/keyset' else dict(markets=[dict(market(),conditionId=d.condition_id,
                            clobTokenIds='["'+d.token_id+'","opposite"]')],next_cursor=None))
                elif source in ('native_rewards','sponsored_rewards'):
                    raw=reward();raw['condition_id']=d.condition_id
                    value=dict(data=[raw],next_cursor='LTE=')
                elif source=='scoring':value=dict(scoring=sdk.get_order_scoring(order_id=req.url.params['order_id']))
                elif source=='order':
                    value=sdk_wire(sdk.get_order(order_id=req.url.path.rsplit('/',1)[-1]))
                    if value is None:return httpx.Response(404)
                elif source=='fills':value=dict(data=sdk_wire(sdk.list_account_trades(market=req.url.params['market'])),next_cursor='LTE=')
                else:raise AssertionError(source)
                return httpx.Response(200,json=value)
            finally:boundary.record('finally_exited',oid,source)
        return LPAsyncSourcePort({key:httpx.AsyncClient(transport=httpx.MockTransport(http),
            base_url='http://127.0.0.1') for key in (('clob','data') if d.kind=='financial' else ('clob',)
                if d.kind=='exact' else ('gamma','clob'))},clock=clock,utc_now=clock.utc,spender=sdk.environment.standard_exchange)
    boundary.factory=factory
    return boundary,clock


def pump_live(live,boundary,predicate):
    end=time.monotonic()+4
    while True:
        live.run_once();handles=live.run_protection_once()
        if predicate():return handles
        left=end-time.monotonic();assert left>0,repr((live.status(),live.diagnostics(),boundary.events))
        with boundary.changed:boundary.changed.wait(min(.01,left))


def test_sell_thirty_second_cadence_retains_reprice_and_finality(tmp_path,monkeypatch):
    from tests.test_lp_d2_integration import _d2_filled_opening, test_d2_sell_reconciliation_preserves_real_quantity_and_pending_attempt
    live,sdk,public,values=_d2_filled_opening(tmp_path/'cadence',monkeypatch)
    try:
        for _ in range(3):
            live.run_once()
            for h in live.run_protection_once():h.result(4)
        assert sdk.posts[-1]['side']=='SELL' and sdk.posts[-1]['size']==60
        boundary,clock=sdk_boundary(live,sdk,public,values)
        live.read_runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
        start=clock()
        def low_ready():
            observation=live.state.snapshot().observation
            if observation is None:return False
            sources=observation.source_frame.sources
            return all(any(k.startswith('market_'+kind+':') and v.complete
                and v.checked_at==clock.utc() for k,v in sources.items()) for kind in ('sell','position'))
        pump_live(live,boundary,low_ready)
        reads=sum(e[0]=='entered' and e[2]=='book' for e in boundary.events)
        public.ask=Decimal('.33');values[0]=start+29;live.read_runtime.wake()
        pump_live(live,boundary,lambda:live.state.snapshot().observation.order_view.orders_source.checked_at==clock.utc())
        assert sum(e[0]=='entered' and e[2]=='book' for e in boundary.events)==reads
        assert sdk.cancels==[]
        values[0]=start+30;live.read_runtime.wake()
        pump_live(live,boundary,lambda:bool(sdk.cancels))
        assert sdk.cancels==[('passive-exit',)]
        assert sdk.market_orders==[]
        sell=next(iter(live.state.snapshot().sells.values()))
        assert sell.residual_quantity.value==60
        assert sell.passive.submit_quantity.value==60
    finally:assert live.close(4);live.trading.close()
    test_d2_sell_reconciliation_preserves_real_quantity_and_pending_attempt(tmp_path/'finality',monkeypatch)

def test_position_sixty_second_cadence_retains_stop_loss_and_quantity(tmp_path,monkeypatch):
    from tests.test_lp_d2_integration import _d2_filled_opening, test_d2_opening_stop_loss_is_current_and_latched_once
    live,sdk,public,values=_d2_filled_opening(tmp_path/'cadence',monkeypatch)
    public.bid=Decimal('.251')
    try:
        for _ in range(3):
            live.run_once()
            for h in live.run_protection_once():h.result(4)
        scope=next(iter(live.state.snapshot().openings))
        assert live.state.snapshot().openings[scope].economics.opening_loss_usd.value==Decimal('4.94')
        boundary,clock=sdk_boundary(live,sdk,public,values)
        initial_observation=live.state.snapshot().observation
        live.read_runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
        start=clock()
        def position_ready():
            observation=live.state.snapshot().observation
            # Market projection alone can retain the prior financial observation.
            return (observation is not None
                and observation.account_id==initial_observation.account_id
                and observation.process_epoch==initial_observation.process_epoch
                and observation.cycle_generation==initial_observation.cycle_generation
                and observation.observation_id!=initial_observation.observation_id
                and all(k in observation.source_frame.sources
                    and observation.source_frame.sources[k].complete
                    and observation.source_frame.sources[k].checked_at==clock.utc()
                    for k in ('balance','open_orders','positions'))
                and any(k.startswith('market_position:') and v.complete and v.checked_at==clock.utc()
                    for k,v in observation.source_frame.sources.items()))
        pump_live(live,boundary,lambda:position_ready() and not any(s['active'] for s in live.read_runtime.status()['scopes']))
        values[0]=start+59;public.bid=Decimal('.25');live.read_runtime.wake()
        pump_live(live,boundary,lambda:live.state.snapshot().observation.order_view.orders_source.checked_at==clock.utc()
            and any(e[0]=='entered' and e[2]=='book' and e[3]==clock() for e in boundary.events)
            and not any(s['active'] for s in live.read_runtime.status()['scopes']))
        snapshot=live.state.snapshot()
        assert not snapshot.openings[scope].stop_loss_latched.value
        assert not any(e[0]=='entered' and e[1].startswith('position:') and e[2]=='book' and e[3]==clock() for e in boundary.events)
        assert snapshot.sells[scope].residual_quantity.value==60
        assert sdk.cancels==[]
        values[0]=start+60;live.read_runtime.wake()
        pump_live(live,boundary,lambda:live.state.snapshot().openings[scope].stop_loss_latched.value
            and sdk.cancels==[('passive-exit',)])
        opening=live.state.snapshot().openings[scope]
        assert opening.stop_loss_triggered_loss_usd.value==Decimal('5.00')
        assert live.state.snapshot().sells[scope].residual_quantity.value==60
        assert sdk.cancels==[('passive-exit',)] and sdk.market_orders==[]
        at=opening.stop_loss_triggered_at
        live.run_once();live.run_protection_once()
        assert live.state.snapshot().openings[scope].stop_loss_triggered_at==at
        assert sdk.cancels==[('passive-exit',)]
    finally:assert live.close(4);live.trading.close()
    test_d2_opening_stop_loss_is_current_and_latched_once(tmp_path/'financial-finality',monkeypatch)
    for closed in (False,True):
        c=Clock();bdy=Boundary(c);bdy.market_closed=closed;bdy.mode[('position','book')]='book404'
        runtime=LPReadRuntime(bdy.factory,clock=c,utc_now=c.utc);runtime.start()
        try:
            runtime.reconcile((demand('position',kind='position'),))
            result=collect(runtime,bdy,lambda rows:bool(rows))[0]
            assert result.complete is closed
            from open_trader.polymarket_lp_async_source import closed_position_value
            proof=closed_position_value(CID,'yes',result.sources)
            assert (proof is not None) is closed
            if closed:assert proof['state']=='non_tradable' and proof['book'] is None and proof['book_absence_status']==404
        finally:bdy.release.set();assert runtime.close(4)


def test_status_reads_are_pure_and_manual_commands_keep_authorization(tmp_path,monkeypatch):
    from datetime import timedelta
    from tests.test_lp_d2_manual_bridge import manual_runtime,entry,test_d2_manual_entry_uses_current_frame_and_explicit_terms
    business=Clock()
    execution,live,sdk,public,dates=manual_runtime(tmp_path/'status',monkeypatch,monotonic_clock=business)
    try:
        result=execution.lp_submit_entry(entry(dates,idempotency_key='status-owned'))
        assert result['state']=='entry_open'
        sid=result['session_id']
        baseline=(sdk.balance_reads,sdk.order_reads,sdk.position_reads,public.book_reads)
        for _ in range(5):
            assert execution.lp_status(sid)['session_id']==sid
            execution.lp_auto_state(include_intents=False)
        assert (sdk.balance_reads,sdk.order_reads,sdk.position_reads,public.book_reads)==baseline
        origin=dates[0]
        class Seconds:
            def __getitem__(self,index):return (dates[0]-origin).total_seconds()
        boundary,clock=sdk_boundary(live,sdk,public,Seconds())
        boundary.mode[(result['entry_order_id'],'book')]='failure'
        live.read_runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
        def fresh():
            observation=live.state.snapshot().observation
            return (observation is not None and observation.order_view.orders_source.source_id.startswith(live.account_id+':')
                and any(y.evaluated_at==clock.utc() for y in live.state.snapshot().yields.values())
                and any(f.consecutive_failures==(1 if clock()==0 else 2)
                    for f in live.state.snapshot().read_failures.values()))
        pump_live(live,boundary,fresh)
        http_reads=len(boundary.events)
        for _ in range(5):execution.lp_status(sid);execution.lp_auto_state(include_intents=False)
        assert len(boundary.events)==http_reads
        assert next(iter(live.state.snapshot().read_failures.values())).consecutive_failures==1
        dates[0]=origin+timedelta(seconds=5);business.value=5;live.read_runtime.wake()
        pump_live(live,boundary,fresh)
        assert any(e[0]=='entered' and e[2]=='book' and e[3]==5 for e in boundary.events)
        assert len(sdk.posts)==1 and sdk.cancels==[]
    finally:assert live.close(4);live.trading.close()
    test_d2_manual_entry_uses_current_frame_and_explicit_terms(tmp_path/'commands',monkeypatch)

def test_runtime_resume_and_shutdown_keep_owner_until_real_drain(tmp_path):
    clock,boundary,trading,live=live_owner(tmp_path)
    boundary.mode[('A','native_rewards')]='failure'
    entered=threading.Event();native_release=threading.Event()
    def cancel(ids):
        trading.cancels.append(ids);entered.set()
        assert native_release.wait(4)
        raise TimeoutError('native_outcome_unknown')
    trading.cancel_orders_detailed=cancel
    try:
        for number in range(1,12):
            advance(live,boundary,clock,(number-1)*5,lambda:count(live)==number)
        live.run_protection_once();assert entered.wait(4)
        saved=[s for s in live.store.lp_sessions() if 'A' in s.get('lp_read_protection',{})]
        assert len(saved)==1
        committed=saved[0]['lp_read_protection']['A']
        assert committed['consecutive_failures']==11
        assert committed['reason_codes']==['read_failure_limit']
        assert committed['cancel_request_id']
        audit,=live.store.lp_actions('d2:'+committed['cancel_request_id'])
        assert audit['order_id']=='A' and audit['kind']=='CANCEL' and audit['d2_record']=='request'
        boundary.holds.add(('A','book'));boundary.mode[('A','book')]='swallow'
        clock.value=55;live.read_runtime.wake();live.run_once()
        end=time.monotonic()+4
        while not any(e[:3]==('entered','A','book') and e[3]==55 for e in boundary.events):
            assert time.monotonic()<end
            with boundary.changed:boundary.changed.wait(.01)
        live.request_running(False)
        old_generation=live.state.snapshot().current.cycle_generation
        from tests.test_lp_d2_integration import settle_control
        boundary.holds.add(('O','balance'))
        live.request_running(True);settle_control(live)
        assert live.state.snapshot().current.cycle_generation!=old_generation
        live.run_once()
        # Resume keeps the real old epoch and the same native request owner.
        assert trading.cancels==[('A',)]
        live.request_running(False);settle_control(live)
        assert live.close(.02) is False
        assert live.status()['native_inflight']==1
        assert live.read_runtime.status()['owned_buy_scopes']>=1
        assert live.store.lp_session(saved[0]['session_id'])['lp_read_protection']['A']==committed
    finally:
        boundary.release.set();native_release.set()
        assert live.close(4)
    # New process restores only committed facts; a held old attempt is not a failure.
    newer_boundary=Boundary(clock);newer_boundary.orders=[wire_row('A'),wire_row('new')]
    newer_boundary.holds={(key,source) for key in ('A','new') for source in ('book','metadata','native_rewards','sponsored_rewards')}
    newer_trading=Trading()
    newer=LPD2LiveExecution(live.store,newer_trading,account_id='account',venue='polymarket',process_epoch='new-epoch',
        policy=CalculationPolicy(Decimal(100)),read_runtime=LPReadRuntime(newer_boundary.factory,clock=clock,utc_now=clock.utc),clock=clock,utc_now=clock.utc)
    newer.initialize(ValidatedStartAdmission('new-epoch','new-claim'))
    try:
        pump_live(newer,newer_boundary,lambda:any(o.key[3]=='new' for o in newer.state.snapshot().active_orders))
        assert count(newer)==11 and count(newer,'new')==0
        newer.run_protection_once();newer.run_once()
        assert newer_trading.cancels==[]
        assert newer.state.snapshot().read_failures[('account',CID,'yes','A')].reason_codes==('read_failure_limit',)
    finally:newer_boundary.release.set();assert newer.close(4)

def test_nonblocking_scheduled_checks_publish_fresh_incumbents_once(tmp_path,monkeypatch):
    import asyncio,httpx
    from datetime import timedelta
    from tests.test_lp_d2_execution_bridge import rotation_runtime,attach
    from test_lp_async_source_contract import market,reward,book
    for case in ('replace-zero','retain-positive','failed-book'):
        root=tmp_path/case
        old,execution,sdk,ids,pools,order,qualify=rotation_runtime(root,monkeypatch,target=2,startup=False)
        assert old.close(4)
        clock=Clock();clock.utc=old._utc_now
        boundary=Boundary(clock);auth=[];wake=threading.Event()
        rows=[];cancel_release=threading.Event();cancel_entered=threading.Event()
        actual_cancel=sdk.cancel_orders
        def held_cancel(*,order_ids):
            assert case=='replace-zero' and order_ids==('B',)
            cancel_entered.set()
            assert cancel_release.wait(4),'external rotation observation watchdog'
            return actual_cancel(order_ids=order_ids)
        if case=='replace-zero':sdk.cancel_orders=held_cancel
        def headers(method,path,body):
            assert method=='GET' and body is None
            auth.append(path);return {'test-read-capability':'already-owned'}
        sdk._ctx=SimpleNamespace(wallet_type='GNOSIS_SAFE',secure_clob=SimpleNamespace(_header_resolver=headers),
            environment=SimpleNamespace(gamma_url='http://127.0.0.1',clob_url='http://127.0.0.1',data_url='http://127.0.0.1',
                standard_exchange=sdk.environment.standard_exchange))
        async def http(req):
            source=req.headers['x-lp-source'];oid=req.headers['x-lp-order']
            boundary.record('entered',oid,source)
            try:
                if oid=='A' and source=='book':
                    try:await asyncio.get_running_loop().run_in_executor(None,boundary.release.wait)
                    except asyncio.CancelledError:boundary.record('cancel_received',oid,source);raise
                if source in ('balance','open_orders','order','fills','scoring'):
                    assert req.headers['test-read-capability']=='already-owned'
                if source=='balance':value=dict(balance='100000000',allowances={sdk.environment.standard_exchange:'100000000','unrelated':'0'})
                elif source=='open_orders':value=dict(data=rows,next_cursor='LTE=')
                elif source=='positions':value=[]
                elif source=='scoring':value=dict(scoring=True)
                elif source=='order':value=next(r for r in rows if r['id']==req.url.path.rsplit('/',1)[-1])
                elif source=='fills':value=dict(data=[],next_cursor='LTE=')
                elif source=='book':
                    if case=='failed-book' and clock()==10 and oid=='B':return httpx.Response(503)
                    token=req.url.params['token_id'];cid=next(cid for cid,t in ids.values() if t==token)
                    value=dict(book(),market=cid,asset_id=token,timestamp=str(int(clock.utc().timestamp()*1000)))
                    if case=='replace-zero':value.update(bids=[dict(price='.60',size='100')],asks=[dict(price='.62',size='100')])
                elif source=='metadata':
                    if req.url.path=='/events/keyset':value=dict(events=[dict(id='7',slug='event',title='Event')],next_cursor=None)
                    else:
                        cid=req.url.params['condition_ids'];token=next(t for c,t in ids.values() if c==cid)
                        value=dict(markets=[dict(market(),conditionId=cid,clobTokenIds='["'+token+'","opposite"]')],next_cursor=None)
                else:
                    cid=req.url.path.rsplit('/',1)[-1]
                    value=dict(data=[dict(reward(),condition_id=cid)],next_cursor='LTE=')
                return httpx.Response(200,json=value)
            finally:boundary.record('finally_exited',oid,source)
        live=LPD2LiveExecution(old.store,old.trading,account_id=old.account_id,venue='polymarket',process_epoch='async-'+case,
            policy=CalculationPolicy(Decimal(100),target_count=2),clock=clock,utc_now=clock.utc,read_transport=httpx.MockTransport(http))
        live.load_settings();live.initialize(ValidatedStartAdmission('async-'+case,'offline-async-permit'))
        producer=execution._lp;execution=attach(live,producer,root)
        execution.set_lp_auto_wakeup(wake.set)
        try:
            assert isinstance(live.read_runtime,LPReadRuntime)
            end=time.monotonic()+4
            while live.rounds.snapshot().round_number==0:
                execution.lp_auto_scheduled_check();assert time.monotonic()<end
                wake.wait(.01);wake.clear()
            rows.extend(dict(wire_row(k),market=ids[k][0],asset_id=ids[k][1],maker_address=old.account_id) for k in 'AB')
            for stamp in (5,10):
                before=next((f.last_attempt for k,f in live.state.snapshot().read_failures.items() if k[3]=='B'),None)
                sdk.advance(5);clock.value=stamp;live.read_runtime.wake();end=time.monotonic()+4
                if stamp==10:
                    qualify('F')
                    # Selection must see the actual complete O/B mailbox at10,
                    # not an earlier financial callback ahead of B's fresh result.
                    # BUY scope keys name the kind at[3] and exact order at[4].
                    def named_mailbox():
                        state=live.read_runtime.status()
                        scopes={('financial' if s['key'][3]=='financial' else s['key'][4]):s for s in state['scopes']}
                        return state['ready_count']>=2 and all(k in scopes and
                            scopes[k]['started']==10 and not scopes[k]['active'] for k in ('financial','B'))
                    boundary.wait(named_mailbox,timeout=max(0,end-time.monotonic()))
                while True:
                    started=time.monotonic();result=execution.lp_auto_scheduled_check()
                    assert time.monotonic()-started<1, 'scheduled owner waited for held HTTP'
                    # The scheduled result owns the facts used for selection.
                    # A real cancel receipt may already have removed B's yield
                    # from fresh state before this test thread observes it.
                    publication=result.acceptance.observation
                    values=publication.yields if publication is not None else live.state.snapshot().yields
                    y=next((y for k,y in values.items() if k[3]=='B'),None)
                    failure=next((f for k,f in live.state.snapshot().read_failures.items() if k[3]=='B'),None)
                    ready=y and y.evaluated_at==clock.utc() and failure and failure.last_attempt!=before
                    if ready and (case!='replace-zero' or stamp!=10 or result.rotation_outcomes):break
                    assert time.monotonic()<end,(case,stamp,before,failure,y,result)
                    wake.wait(.01);wake.clear()
                expected=None if stamp==10 and case=='failed-book' else Decimal(0) if case=='replace-zero' else Decimal('0.892857142857')
                assert (None if y.yield_pct_per_hour.value is None else y.yield_pct_per_hour.value.quantize(Decimal('.000000000001')))==expected
                assert not boundary.release.is_set()
            assert len([s for s in live.read_runtime.status()['scopes'] if s['key'][3]=='financial'])==1
            if case=='replace-zero':
                assert result.selection.victim_order_ids==('B',)
                assert cancel_entered.wait(4)
                cancel_release.set()
                for outcome in result.rotation_outcomes:outcome.handle.result(4)
                assert sdk.cancels==[('B',)],(result,live.rotation_diagnostics(),live.status())
            else:
                assert 'B' not in result.selection.victim_order_ids
                assert sdk.cancels==[]
            live.dispatch_rotation(result);live.dispatch_rotation(result)
            assert sdk.cancels==([('B',)] if case=='replace-zero' else [])
            assert auth and wake.wait(1)
        finally:cancel_release.set();boundary.release.set();assert live.close(4);live.trading.close()


def test_rejected_financial_observation_keeps_independent_market_failure_and_cleanup(tmp_path):
    from open_trader.polymarket_lp_current_state import ReceiptEvidence, TerminalProof, Fact, SourceStamp
    for variant in ('http_failure', 'timeout_then_fault', 'co_delivered_cleanup_fault'):
        clock,boundary,trading,live=live_owner(tmp_path/variant)
        akey=('account',CID,'yes','A')
        def accept_peer_terminal():
            snapshot=live.state.snapshot();peer=next(o for o in snapshot.orders if o.key[3]=='B')
            stamp=SourceStamp('external-B-final',checked_at=clock.utc(),complete=True,state='known')
            proof=TerminalProof(peer.key,'CANCELED',peer.original_size,Fact('known',Decimal(0),(stamp,)),stamp,True)
            assert live.state.accept_receipt_evidence(ReceiptEvidence('receipt-B','request-B','account','epoch',
                snapshot.current.cycle_generation,peer.key,'BUY',peer.terms_version,terminal_proof=proof))=='applied'
            return proof
        try:
            advance(live,boundary,clock,0,lambda:len(live.state.snapshot().read_failures)==2)
            initial=live.state.snapshot()
            assert initial.yields[akey].yield_pct_per_hour.state=='known' and count(live)==0
            boundary.holds={('A','book'),('O','balance')}
            boundary.mode[('A','book')]='failure' if variant=='http_failure' else 'swallow'
            clock.value=5;live.read_runtime.wake()
            boundary.wait(lambda:all(any(e[:3]==('entered',oid,source) and e[3]==5
                for e in boundary.events) for oid,source in boundary.holds))
            if variant=='http_failure':
                proof=accept_peer_terminal()
                clock.value=5.1;boundary.release.set()
                boundary.wait(lambda:not any(s['active'] for s in live.read_runtime.status()['scopes'])
                    and live.read_runtime.status()['ready_count']>=2)
                financial_before=initial.observation.read_ticket
            else:
                # Attempt starts at5: timeout10 and cleanup fault11. Exercise
                # both separate owner collections and a single co-delivery.
                clock.value=10;live.read_runtime.wake()
                boundary.wait(lambda:any(e[:3]==('cancel_received','A','book') for e in boundary.events)
                    and live.read_runtime.status()['ready_count']>=2)
                if variant=='timeout_then_fault':
                    assert live.run_once().acceptance.status=='partial'
                    timed_out=live.state.snapshot()
                    assert timed_out.read_failures[akey].consecutive_failures==1
                    assert timed_out.yields[akey].yield_pct_per_hour.state=='unknown'
                    assert 'read_cleanup_fault' not in timed_out.read_failures[akey].reason_codes
                    financial_before=timed_out.observation.read_ticket
                    boundary.wait(lambda:any(e[:3]==('entered','O','balance') and e[3]==10 for e in boundary.events))
                else:
                    financial_before=initial.observation.read_ticket
                proof=accept_peer_terminal()
                clock.value=11;live.read_runtime.wake()
                boundary.wait(lambda:bool(live.read_runtime.status()['cleanup_faults']))
                assert any(scope['active'] and scope['cleanup_fault'] for scope in live.read_runtime.status()['scopes'])
                if variant=='timeout_then_fault':
                    boundary.release.set()
                    boundary.wait(lambda:any(e[:3]==('finally_exited','O','balance') and e[3]==11 for e in boundary.events)
                        and live.read_runtime.status()['ready_count']>=2)
            result=live.run_once()
            if variant=='timeout_then_fault':
                # An old observation-ID result may already have been filtered;
                # either disposition must still deny stale financial authority.
                assert result.acceptance.status in ('partial','stale')
            else:
                assert result.acceptance.status=='stale'
            current=live.state.snapshot()
            assert current.read_failures[akey].consecutive_failures==1
            assert current.read_failures[akey].last_attempt!=initial.read_failures[akey].last_attempt
            assert current.yields[akey].yield_pct_per_hour.state=='unknown'
            assert current.yields[akey].input_sources!=initial.yields[akey].input_sources
            assert current.new_capacity.state=='unknown'
            assert next(o for o in current.orders if o.key[3]=='B').terminal_proof==proof
            assert current.observation.read_ticket==financial_before
            if variant!='http_failure':
                assert 'read_cleanup_fault' in current.read_failures[akey].reason_codes
                if variant=='timeout_then_fault':
                    assert current.read_failures[akey].last_attempt==timed_out.read_failures[akey].last_attempt
            else:
                assert current.yields[akey].evaluated_at==clock.utc()
            for _ in range(3):live.run_once()
            assert count(live)==1 and trading.cancels==[]
        finally:
            boundary.release.set()
            assert live.close(4)


def test_read_failure_cancel_without_opening_collects_exact_final_quantity(tmp_path):
    clock,boundary,trading,live=live_owner(tmp_path)
    boundary.orders=[wire_row('exact'),dict(wire_row('peer'),market='0x'+'2'*64,asset_id='b'),wire_row('sell',side='SELL')]
    boundary.mode[('exact','book')]='failure';boundary.book_depth=1000
    boundary.holds={('exact','order')}
    def cancel(ids):
        trading.cancels.append(ids)
        boundary.orders=[r for r in boundary.orders if r['id'] not in ids]
        trading.cancelled.set()
        return {'canceled':list(ids),'not_canceled':{}}
    trading.cancel_orders_detailed=cancel
    key=('account',CID,'yes','exact')
    try:
        for number in range(1,12):
            advance(live,boundary,clock,(number-1)*5,lambda:count(live,'exact')==number)
        snapshot=live.state.snapshot()
        assert not snapshot.openings
        assert not any(k.kind=='queue_bucket' and 'exact' in b.anchor_order_ids for k,b in snapshot.baselines.items())
        for handle in live.run_protection_once():handle.result(4)
        assert trading.cancelled.wait(4) and trading.cancels==[('exact',)]
        order=next(o for o in live.state.snapshot().orders if o.key==key)
        assert order.terminal_proof is not None
        assert order.terminal_proof.matched.state=='unknown' and order.remaining.state=='unknown'
        # Observe the actual next financial request rather than requiring its
        # pre-ACK ticket to be accepted after the receipt changes trade_version.
        for stamp in (55,60):
            clock.value=stamp;live.read_runtime.wake()
            end=time.monotonic()+4
            while True:
                live.run_once();live.run_protection_once()
                status=live.read_runtime.status()
                exited=any(e[:3]==('finally_exited','O','open_orders') and e[3]==stamp for e in boundary.events)
                busy=any(s['active'] and s['key'][3]!='exact' for s in status['scopes'])
                if exited and not busy and status['ready_count']==0:break
                left=end-time.monotonic();assert left>0,(status,boundary.events)
                with boundary.changed:boundary.changed.wait(min(.01,left))
        boundary.wait(lambda:any(e[:3]==('entered','exact','order') for e in boundary.events))
        requests=[e for e in boundary.events if e[0]=='entered' and e[2]=='order']
        assert requests and {e[1] for e in requests}=={'exact'}, (live.state.snapshot().trade_version,live.state.snapshot().observation.read_ticket.trade_version_at_start,live.state.snapshot().observation.order_view.orders_source.checked_at,[(s['key'][3],s['active']) for s in live.read_runtime.status()['scopes']])
        order=next(o for o in live.state.snapshot().orders if o.key==key)
        assert order.terminal_proof.matched.state=='unknown' and order.remaining.state=='unknown'
        # The exact request entered at60 and still owns its response. Its real
        # authoritative final proof arrives before this attempt's deadline.
        boundary.release.set()
        pump_live(live,boundary,lambda:next(o for o in live.state.snapshot().orders if o.key==key).remaining.state=='known')
        final=next(o for o in live.state.snapshot().orders if o.key==key)
        assert final.terminal_proof.status=='CANCELED'
        assert final.original_size.value==20
        assert final.terminal_proof.matched.state=='known' and final.terminal_proof.matched.value==3
        assert final.remaining.value==17 and final.size_matched.value==3
        assert not any(s['key'][3]=='buy' and s['key'][4]=='exact' for s in live.read_runtime.status()['scopes'])
        for _ in range(3):live.run_once();live.run_protection_once()
        assert trading.cancels==[('exact',)]
        assert {o.key[3] for o in live.state.snapshot().active_orders}=={'peer','sell'}
        assert not live.state.snapshot().openings
    finally:
        boundary.release.set()
        assert live.close(4)


def test_scheduler_waits_for_async_dependencies_and_completion_wakes_early(tmp_path):
    from datetime import datetime,timedelta
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_d2_execution_bridge import attach
    clock,boundary,trading,live=live_owner(tmp_path)
    boundary.orders=[];boundary.holds={('O','balance')}
    producer=PolymarketLPService(live.store,trading,clock=clock.utc)
    execution=attach(live,producer,tmp_path)
    scheduler=LPAutoScheduler(execution,clock=clock.utc)
    execution.set_lp_auto_scheduler(scheduler)
    woken=threading.Event()
    def wake():woken.set();scheduler.request_check()
    execution.set_lp_auto_wakeup(wake)
    try:
        assert scheduler.run_due()
        boundary.wait(lambda:any(e[:3]==('entered','O','balance') for e in boundary.events)
            and all(any(e[:3]==('finally_exited','O',source) for e in boundary.events) for source in ('open_orders','positions')))
        assert woken.wait(4);woken.clear()
        scheduler.run_due()  # consume the real startup ownership/completion notice
        status=live.read_runtime.status()
        assert status['ready_count']==0 and any(s['active'] for s in status['scopes'])
        state=execution.lp_auto_state(include_intents=False)
        assert state.get('plan_wait',{}).get('kind')=='dependencies'
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==NOW+timedelta(seconds=5)
        assert datetime.fromisoformat(scheduler.snapshot()['next_check_at'])==clock.utc()+timedelta(seconds=5)
        before=list(boundary.events)
        for _ in range(100):
            assert not scheduler.run_due()
            execution.lp_auto_state(include_intents=False)
        assert boundary.events==before and live.state.snapshot().observation is None
        # Actual completion before the dependency deadline wakes the registered
        # Scheduler and publishes financial facts without waiting until5.
        clock.value=1;woken.clear();boundary.release.set()
        assert woken.wait(4)
        end=time.monotonic()+4
        while live.state.snapshot().observation is None:
            scheduler.run_due()
            left=end-time.monotonic();assert left>0
            with boundary.changed:boundary.changed.wait(min(.01,left))
        assert live.state.snapshot().observation.components['balance'].checked_at==clock.utc()
        assert live.state.snapshot().observation.order_view.orders_source.checked_at==NOW
        assert live.state.snapshot().new_capacity.value==100
        # Consume only real completion/ACK notices until the stable idle scope
        # publishes its actual next start, rather than inventing a UI timer.
        while datetime.fromisoformat(scheduler.snapshot()['next_check_at'])<=clock.utc():
            assert woken.wait(max(0,end-time.monotonic()));woken.clear()
            assert scheduler.run_due()
        assert datetime.fromisoformat(scheduler.snapshot()['next_check_at'])==NOW+timedelta(seconds=5)
        before=list(boundary.events)
        for _ in range(100):assert not scheduler.run_due()
        assert boundary.events==before
        # Explicit public commands remain runnable while read dependencies wait.
        assert execution.lp_auto_run_once()['state']=='scheduled'
        assert scheduler.run_due()
        assert boundary.events==before and trading.cancels==[]
        assert live.state.snapshot().observation.components['balance'].checked_at==clock.utc()
        assert live.state.snapshot().observation.order_view.orders_source.checked_at==NOW
    finally:
        boundary.release.set();scheduler.stop()
        assert live.close(4)


def test_scheduler_waits_for_cleanup_fault_exit_without_busy_loop(tmp_path):
    from datetime import datetime,timedelta
    from test_lp_financial_read_recovery import FinancialBoundary
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_d2_execution_bridge import attach
    for with_buy in (False,True):
        root=tmp_path/('with-buy' if with_buy else 'empty')
        clock,_,trading,live=live_owner(root)
        boundary=FinancialBoundary(clock)
        live.read_runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
        boundary.orders=[wire_row('A')] if with_buy else []
        producer=PolymarketLPService(live.store,trading,clock=clock.utc)
        execution=attach(live,producer,root)
        scheduler=LPAutoScheduler(execution,clock=clock.utc)
        execution.set_lp_auto_scheduler(scheduler)
        notices=[];changed=threading.Condition()
        def notice():
            status=live.read_runtime.status()
            scheduler.request_check()
            with changed:notices.append(status);changed.notify_all()
        execution.set_lp_auto_wakeup(notice)
        def drive_until(predicate):
            end=time.monotonic()+4
            while not predicate():
                scheduler.run_due()
                left=end-time.monotonic();assert left>0,(live.status(),boundary.events)
                with boundary.changed:boundary.changed.wait(min(.01,left))
        def collect_at(stamp,*,faults,dependency):
            clock.value=stamp;live.read_runtime.wake()
            boundary.wait(lambda:live.read_runtime.status()['ready_count']>0)
            scheduler.request_check();assert scheduler.run_due()
            # Observe the actual post-ACK publication, then consume its wakeup.
            boundary.wait(lambda:any(n['observed_monotonic']==stamp and n['ready_count']==0
                and n['cleanup_faults']==faults and n['next_dependency_monotonic']==dependency for n in notices))
            scheduler.run_due()
        try:
            if with_buy:
                scheduler.run_due()
                drive_until(lambda:('account',CID,'yes','A') in live.state.snapshot().read_failures
                    and live.state.snapshot().yields[('account',CID,'yes','A')].yield_pct_per_hour.state=='known')
            boundary.hold_source='balance'
            start=5 if with_buy else 0
            clock.value=start;live.read_runtime.wake();scheduler.request_check();assert scheduler.run_due()
            boundary.wait(lambda:any(e[:3]==('entered','O','balance') and e[3]==start for e in boundary.events))
            if with_buy:
                drive_until(lambda:live.state.snapshot().yields[('account',CID,'yes','A')].evaluated_at==clock.utc()
                    and live.read_runtime.status()['ready_count']==0)
            first=next(s for s in live.read_runtime.status()['scopes'] if s['key'][3]=='financial')
            deadline=start+5;fault_at=start+6
            if with_buy:
                clock.value=deadline;live.read_runtime.wake()
                boundary.wait(lambda:any(e[:3]==('finally_exited','A','book') and e[3]==deadline for e in boundary.events))
                # HTTP finally is earlier than the complete runtime mailbox.
                # Both A's result and O's timeout must be ready for co-delivery.
                boundary.wait(lambda:live.read_runtime.status()['ready_count']>=2
                    and not next(s for s in live.read_runtime.status()['scopes'] if s['key'][3]=='buy')['active'])
            collect_at(deadline,faults=0,dependency=fault_at)
            snapshot=live.state.snapshot()
            assert snapshot.new_capacity.state=='unknown'
            assert snapshot.observation.components['balance'].error_code=='timeout'
            assert execution.lp_auto_state(include_intents=False)['plan_wait']==dict(
                kind='dependencies',deadline=(NOW+timedelta(seconds=fault_at)).isoformat())
            assert datetime.fromisoformat(scheduler.snapshot()['next_check_at'])==NOW+timedelta(seconds=fault_at)
            expected_dependency=15 if with_buy else None
            collect_at(fault_at,faults=1,dependency=expected_dependency)
            status=live.read_runtime.status()
            financial_scope=next(s for s in status['scopes'] if s['key'][3]=='financial')
            assert financial_scope['active'] and financial_scope['cleanup_fault']
            assert financial_scope['attempt']==first['attempt'] and status['ready_count']==0
            assert not status['new_buy_allowed'] and status['pool_count']>=2
            waiting=execution.lp_auto_state(include_intents=False)['plan_wait']
            if with_buy:
                assert waiting==dict(kind='dependencies',deadline=(NOW+timedelta(seconds=15)).isoformat())
                assert datetime.fromisoformat(scheduler.snapshot()['next_check_at'])==NOW+timedelta(seconds=15)
                assert live.state.snapshot().yields[('account',CID,'yes','A')].evaluated_at==NOW+timedelta(seconds=10)
            else:
                assert waiting==dict(kind='events')
                assert scheduler.snapshot()['next_check_at'] is None
                assert not scheduler.snapshot()['check_in_progress']
            before=list(boundary.events)
            assert all(not scheduler.run_due() for _ in range(100))
            assert boundary.events==before
            assert execution.lp_auto_run_once()['state']=='scheduled'
            assert scheduler.run_due() and not scheduler.run_due()
            assert boundary.events==before and live.state.snapshot().new_capacity.state=='unknown'
            assert next(s for s in live.read_runtime.status()['scopes'] if s['key'][3]=='financial')['active']
            if with_buy:
                clock.value=15;live.read_runtime.wake();assert scheduler.run_due()
                drive_until(lambda:live.state.snapshot().yields[('account',CID,'yes','A')].evaluated_at==clock.utc())
                assert count(live)==0
            # Actual response close releases only the financial delay blocker.
            # Its original notification was collected above; fresh facts need a new attempt.
            assert boundary.close_entered.is_set() and not boundary.close_exited.is_set()
            boundary.close_release.set()
            assert boundary.close_exited.wait(4)
            boundary.wait(lambda:live.read_runtime.status()['last_financial_cleanup_fault']['ended_monotonic']==clock())
            drive_until(lambda:live.state.snapshot().observation.orders_complete
                and live.state.snapshot().observation.components['balance'].checked_at==clock.utc())
            status=live.read_runtime.status()
            assert status['cleanup_faults']==0 and status['new_buy_allowed']
            assert status['financial_cleanup_faults']==1
            assert status['pool_count']>=2 and live.state.snapshot().new_capacity.state=='known'
            assert len([e for e in boundary.events if e[:3]==('entered','O','balance') and e[3]>=start])==2
            assert len(boundary.clients)==2 and len(boundary.ports)==1
            assert trading.cancels==[]
            scheduler.start()
            def monitor_waiting():
                monitor=scheduler.snapshot()
                return monitor['scheduler_running'] and not monitor['check_in_progress'] and (
                    monitor['next_check_at'] is not None
                    and datetime.fromisoformat(monitor['next_check_at'])>clock.utc())
            boundary.wait(monitor_waiting)
            scheduler.stop()
            assert not scheduler.snapshot()['scheduler_running']
        finally:
            boundary.close_release.set();boundary.release.set();scheduler.stop()
            assert live.close(4)


def test_scheduler_preserves_control_retry_and_completed_save_during_read_wait(tmp_path):
    from datetime import datetime,timedelta
    import hashlib
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_d2_execution_bridge import attach
    for event_only in (False,True):
        root=tmp_path/('fault' if event_only else 'cooldown')
        clock=Clock();boundary=Boundary(clock)
        class SaveBoundary(PredictionArbitrageStore):
            armed=False
            def __init__(self,path):
                super().__init__(path);self.calls=[];self.threads=[]
                self.entered=[threading.Event(),threading.Event()]
                self.releases=[threading.Event(),threading.Event()]
            def lp_save_current_control(self,write,**kwargs):
                if not self.armed:return super().lp_save_current_control(write,**kwargs)
                index=len(self.calls);assert index<2,'no third save permitted'
                self.calls.append((clock(),dict(write)));self.threads.append(threading.current_thread())
                self.entered[index].set()
                assert self.releases[index].wait(4),'external control-save release watchdog'
                if index==0:raise OSError('offline first save failure')
                return super().lp_save_current_control(write,**kwargs)
        store=SaveBoundary(root)
        store.lp_save_current_control(dict(account_id=hashlib.sha256(b'account').hexdigest(),
            process_epoch='seed',request_version=1,request_id='seed',desired_running=True,
            requested_at=clock.utc(),actor=None))
        trading=Trading();runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
        live=LPD2LiveExecution(store,trading,account_id='account',venue='polymarket',process_epoch='epoch',
            policy=CalculationPolicy(Decimal(100)),read_runtime=runtime,clock=clock,utc_now=clock.utc)
        live.initialize(ValidatedStartAdmission('epoch','offline-permit'));store.armed=True
        producer=PolymarketLPService(store,trading,clock=clock.utc)
        execution=attach(live,producer,root);scheduler=LPAutoScheduler(execution,clock=clock.utc)
        execution.set_lp_auto_scheduler(scheduler)
        notices=[]
        def notice():
            status=runtime.status();scheduler.request_check();notices.append(status)
        execution.set_lp_auto_wakeup(notice)
        def collect_read(stamp,dependency,faults):
            clock.value=stamp;runtime.wake()
            boundary.wait(lambda:any(n['observed_monotonic']==stamp and n['ready_count']>0 for n in notices))
            assert scheduler.run_due()
            boundary.wait(lambda:any(n['observed_monotonic']==stamp and n['ready_count']==0
                and n['next_dependency_monotonic']==dependency and n['cleanup_faults']==faults for n in notices))
            scheduler.run_due()  # consume the real post-ACK wake, never a periodic request
        try:
            assert live.control.status().current.effective_running is True
            if event_only:
                boundary.holds={('O','balance')};boundary.mode[('O','balance')]='swallow'
            else:boundary.mode[('O','balance')]='cooling'  # real HTTP429, Retry-After12
            assert scheduler.run_due()
            boundary.wait(lambda:('entered','O','balance',0) in boundary.events)
            if event_only:
                collect_read(5,6,0);collect_read(6,None,1)
                assert execution.lp_auto_state(include_intents=False)['plan_wait']==dict(kind='events')
                assert scheduler.snapshot()['next_check_at'] is None
                assert runtime.status()['cleanup_faults']==1 and not runtime.status()['new_buy_allowed']
            else:
                collect_read(0,12,0)
                assert datetime.fromisoformat(scheduler.snapshot()['next_check_at'])==NOW+timedelta(seconds=12)
            origin=clock();before=list(boundary.events)
            pause=execution.lp_auto_set_desired_running(False)
            assert pause['effective_running'] is False
            assert store.entered[0].wait(4)
            assert live.control.status().save_state=='saving'
            assert execution.lp_auto_state(include_intents=False)['plan_wait'] is None
            # Native save actually returns before its completion is collected.
            store.releases[0].set();store.threads[0].join(4);assert not store.threads[0].is_alive()
            uncollected=live.control.status()
            assert uncollected.save_state=='saving' and not uncollected.native_save_in_flight
            assert scheduler.run_due()
            retry=live.control.status()
            assert retry.save_state=='retry_wait' and retry.retry_at_monotonic==origin+1
            assert retry.attempt_number==1 and retry.saved_version<retry.requested_version
            assert datetime.fromisoformat(scheduler.snapshot()['next_check_at'])==NOW+timedelta(seconds=origin+1)
            assert execution.lp_auto_state(include_intents=False)['plan_wait']==dict(
                kind='dependencies',deadline=(NOW+timedelta(seconds=origin+1)).isoformat())
            assert not retry.current.effective_running and retry.fatal is None
            clock.value=origin+.999
            assert not scheduler.run_due() and len(store.calls)==1 and boundary.events==before
            clock.value=origin+1
            assert scheduler.run_due() and store.entered[1].wait(4)
            assert live.control.status().save_state=='retrying'
            assert execution.lp_auto_state(include_intents=False)['plan_wait'] is None
            store.releases[1].set();store.threads[1].join(4);assert not store.threads[1].is_alive()
            uncollected=live.control.status()
            assert uncollected.save_state=='retrying' and not uncollected.native_save_in_flight
            assert uncollected.saved_version<uncollected.requested_version
            assert execution.lp_auto_state(include_intents=False)['plan_wait'] is None
            assert scheduler.run_due()  # completed save remains immediately consumable at1/7
            saved=live.control.status()
            assert saved.save_state=='saved' and saved.saved_version==saved.requested_version==1
            assert not saved.current.effective_running and saved.fatal is None
            assert [t for t,_ in store.calls]==[origin,origin+1] and boundary.events==before
            assert not scheduler.run_due() and len(store.calls)==2 and trading.cancels==[]
            if event_only:
                financial=next(s for s in runtime.status()['scopes'] if s['key'][3]=='financial')
                assert financial['active'] and financial['cleanup_fault']
                assert runtime.status()['pool_count']>=2 and not runtime.status()['new_buy_allowed']
                assert scheduler.snapshot()['next_check_at'] is None
            else:
                assert datetime.fromisoformat(scheduler.snapshot()['next_check_at'])==NOW+timedelta(seconds=12)
        finally:
            for release in store.releases:release.set()
            # Failure evidence must not strand a failed-save retry during close.
            clock.value=max(clock(),(store.calls[0][0]+1) if store.calls else clock())
            boundary.release.set();scheduler.stop();assert live.close(4)


def test_scheduler_preserves_owner_error_retry_during_provider_cooldown(tmp_path):
    from datetime import datetime,timedelta
    import httpx
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_async_source import LPAsyncSourcePort
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_d2_execution_bridge import attach
    clock=Clock()
    class CooldownBoundary(Boundary):
        def factory(self,demand):
            async def http(req):
                source=req.headers['x-lp-source'];order=req.headers['x-lp-order']
                self.record('entered',order,source)
                try:
                    if source=='open_orders':return httpx.Response(200,json=dict(data=[wire_row('A')],next_cursor='LTE='))
                    if source=='positions':return httpx.Response(200,json=[])
                    return httpx.Response(429,headers={'Retry-After':'300'})
                finally:self.record('finally_exited',order,source)
            return LPAsyncSourcePort({key:httpx.AsyncClient(transport=httpx.MockTransport(http),
                base_url='http://127.0.0.1') for key in (('clob','data') if demand.kind=='financial' else ('gamma','clob'))},
                clock=clock,utc_now=clock.utc)
    class RegistrationBoundary(PredictionArbitrageStore):
        failing=True
        def __init__(self,path):super().__init__(path);self.calls=[]
        def lp_register_exchange_orders(self,*args,**kwargs):
            self.calls.append(clock())
            if self.failing:raise OSError('offline order-registration storage fault')
            return super().lp_register_exchange_orders(*args,**kwargs)
    boundary=CooldownBoundary(clock);store=RegistrationBoundary(tmp_path);trading=Trading()
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
    live=LPD2LiveExecution(store,trading,account_id='account',venue='polymarket',process_epoch='epoch',
        policy=CalculationPolicy(Decimal(100)),read_runtime=runtime,clock=clock,utc_now=clock.utc)
    live.initialize(ValidatedStartAdmission('epoch','offline-permit'))
    producer=PolymarketLPService(store,trading,clock=clock.utc)
    execution=attach(live,producer,tmp_path);scheduler=LPAutoScheduler(execution,clock=clock.utc)
    execution.set_lp_auto_scheduler(scheduler)
    notices=[]
    def notice():
        status=runtime.status();scheduler.request_check();notices.append(status)
    execution.set_lp_auto_wakeup(notice)
    try:
        assert execution.lp_auto_state(include_intents=False)['api_retry_interval_seconds']==60
        assert scheduler.run_due()
        end=time.monotonic()+4
        def settled():
            return any(n['next_dependency_monotonic']==300 and n['ready_count']==0
                and n['owned_buy_scopes']==1 and not any(s['active'] for s in n['scopes']) for n in notices)
        while not settled():
            scheduler.run_due()
            left=end-time.monotonic();assert left>0,(runtime.status(),scheduler.snapshot(),boundary.events)
            with boundary.changed:boundary.changed.wait(min(.01,left))
        scheduler.run_due()  # consume actual final completion/ACK notice
        snapshot=live.state.snapshot();status=runtime.status()
        assert {o.key[3] for o in snapshot.active_orders}=={'A'}
        assert snapshot.observation.orders_complete and snapshot.new_capacity.state=='unknown'
        assert status['ready_count']==0 and status['next_dependency_monotonic']==300
        assert not any(s['active'] for s in status['scopes'])
        assert store.calls and set(store.calls)=={0}
        state=scheduler.snapshot();assert state['last_check_error']=='RuntimeError'
        error_at=datetime.fromisoformat(state['last_check_at'])
        assert error_at==NOW and datetime.fromisoformat(state['next_check_at'])==error_at+timedelta(seconds=60)
        assert execution.lp_auto_state(include_intents=False)['plan_wait']==dict(
            kind='dependencies',deadline=(NOW+timedelta(seconds=300)).isoformat())
        before=list(boundary.events);registrations=len(store.calls)
        failures=dict(snapshot.read_failures)
        clock.value=59.999
        assert not scheduler.run_due() and len(store.calls)==registrations
        assert boundary.events==before and dict(live.state.snapshot().read_failures)==failures
        store.failing=False;clock.value=60
        assert scheduler.run_due() and len(store.calls)==registrations+1
        assert store.calls[-1]==60 and scheduler.snapshot()['last_check_error'] is None
        assert live.status()['read_persistence_error'] is None
        assert datetime.fromisoformat(scheduler.snapshot()['next_check_at'])==NOW+timedelta(seconds=300)
        assert not scheduler.run_due() and boundary.events==before
        assert dict(live.state.snapshot().read_failures)==failures and trading.cancels==[]
        clock.value=299.999
        assert not scheduler.run_due() and boundary.events==before
    finally:
        store.failing=False;boundary.release.set();scheduler.stop();assert live.close(4)


def test_scheduler_wakes_deferred_read_fault_cancels_after_native_completion(tmp_path):
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_d2_execution_bridge import attach
    # Observe the approved public admission port while the real service remains
    # its binding owner and its actual safety lease/permission remains in force.
    class AdmissionObservedLive(LPD2LiveExecution):
        def bind_mutation_admission(self,owner,acquire):
            def observed(action):
                if not admitted:return None
                actual=acquire(action)
                return Lease(actual) if actual is not None else None
            return super().bind_mutation_admission(owner,observed)
    clock=Clock();boundary=Boundary(clock);trading=Trading();store=PredictionArbitrageStore(tmp_path)
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
    live=AdmissionObservedLive(store,trading,account_id='account',venue='polymarket',process_epoch='epoch',
        policy=CalculationPolicy(Decimal(100)),read_runtime=runtime,clock=clock,utc_now=clock.utc)
    live.initialize(ValidatedStartAdmission('epoch','offline-permit'))
    targets={'A','B','C'}
    boundary.orders=[wire_row('A'),wire_row('B'),wire_row('C','3')]
    boundary.book_depth=1000  # isolate read faults from the original queue threshold
    producer=PolymarketLPService(live.store,trading,clock=clock.utc)
    execution=attach(live,producer,tmp_path);scheduler=LPAutoScheduler(execution,clock=clock.utc)
    execution.set_lp_auto_scheduler(scheduler)
    notices=[]
    def notice():
        state=live.read_runtime.status();scheduler.request_check();notices.append(state)
    execution.set_lp_auto_wakeup(notice)
    changed=threading.Condition();entered=set();finished=set();threads={}
    releases={oid:threading.Event() for oid in targets}
    active=0;maximum=0;admitted=False;gate=False;pair=set();deferred=None
    owner_thread=threading.current_thread();actual_cancel=trading.cancel_orders_detailed
    def cancel(ids):
        nonlocal active,maximum
        assert len(ids)==1 and ids[0] in targets
        oid=ids[0]
        with changed:
            assert oid not in entered,'UNKNOWN cancel must not replay'
            entered.add(oid);threads[oid]=threading.current_thread()
            active+=1;maximum=max(maximum,active);changed.notify_all()
        try:
            assert releases[oid].wait(4),'external native cancel release watchdog'
            return actual_cancel(ids)  # actual offline UNKNOWN result, no receipt invented
        finally:
            with changed:active-=1;finished.add(oid);changed.notify_all()
    trading.cancel_orders_detailed=cancel
    def requests():
        return {t.order_key[3]:t.request_id for p in live.state.snapshot().progress.values()
            for t in p.cancel_targets if t.order_key[3] in targets}
    class Lease:
        def __init__(self,actual):self.actual=actual
        def allowed(self,action=None):return admitted and self.actual.allowed(action)
        def release(self):
            self.actual.release()
    def drive_until(predicate):
        end=time.monotonic()+4
        while not predicate():
            scheduler.run_due()
            left=end-time.monotonic();assert left>0,(live.status(),boundary.events)
            with changed:changed.wait(min(.01,left))
    def collect_at(stamp,dependency,faults):
        clock.value=stamp;live.read_runtime.wake()
        boundary.wait(lambda:any(n['observed_monotonic']==stamp and n['ready_count']==4 for n in notices))
        assert scheduler.run_due()
        boundary.wait(lambda:any(n['observed_monotonic']==stamp and n['ready_count']==0
            and n['cleanup_faults']==faults and n['next_dependency_monotonic']==dependency for n in notices))
        scheduler.run_due()  # actual post-ACK notice, not a manufactured rescue
    try:
        assert scheduler.run_due()
        drive_until(lambda:len(live.state.snapshot().read_failures)==3
            and all(y.yield_pct_per_hour.state=='known' for y in live.state.snapshot().yields.values())
            and len(live.state.snapshot().yields)==3)
        # Start four real held attempts at5; timeout10 and cleanup fault11
        # preserve the original per-attempt5/6 boundaries.
        boundary.holds={('O','balance'),*((oid,'book') for oid in targets)}
        for key in boundary.holds:boundary.mode[key]='swallow'
        clock.value=5;live.read_runtime.wake()
        drive_until(lambda:all(('entered',oid,source,5) in boundary.events for oid,source in boundary.holds))
        collect_at(10,11,0);collect_at(11,None,4)
        state=live.read_runtime.status();snapshot=live.state.snapshot()
        assert not snapshot.current.effective_running and snapshot.new_capacity.state=='unknown'
        assert all('read_cleanup_fault' in f.reason_codes for f in snapshot.read_failures.values())
        assert len(state['scopes'])==4 and all(s['active'] and s['cleanup_fault'] for s in state['scopes'])
        assert state['next_dependency_monotonic'] is None and state['ready_count']==0 and state['waiting_for_event']
        assert not state['new_buy_allowed'] and state['pool_count']==8
        assert scheduler.snapshot()['next_check_at'] is None
        assert requests()=={} and trading.cancels==[] and not entered
        before=list(boundary.events)
        admitted=True
        execution.lp_auto_run_once();scheduler.run_due()
        boundary.wait(lambda:entered==targets)
        assert set(requests())==targets and len(set(requests().values()))==3
        assert live.status()['native_inflight']==3 and maximum==3
        assert boundary.events==before and clock()==11
        pair=set(sorted(targets)[:2]);deferred=next(iter(targets-pair))
        for oid in pair:releases[oid].set()
        for oid in pair:threads[oid].join(4);assert not threads[oid].is_alive()
        assert scheduler.run_due(),'actual native completion must wake the existing owner'
        assert live.status()['native_inflight']==1 and not releases[deferred].is_set()
        assert set(requests())==targets and len(trading.cancels)==2
        releases[deferred].set();threads[deferred].join(4);assert not threads[deferred].is_alive()
        assert scheduler.run_due()
        drive_until(lambda:scheduler.snapshot()['next_check_at'] is None)
        snapshot=live.state.snapshot();state=live.read_runtime.status()
        assert sorted(trading.cancels)==[('A',),('B',),('C',)] and finished==targets
        assert maximum==3 and live.status()['native_inflight']==0
        partial=next(o for o in snapshot.orders if o.key[3]=='C')
        assert (partial.original_size.value,partial.size_matched.value,partial.remaining.value)==(20,3,17)
        assert {o.key[3] for o in snapshot.active_orders}==targets and not snapshot.openings
        assert all(t.local_outcome=='unknown_error' for p in snapshot.progress.values() for t in p.cancel_targets)
        assert all('read_cleanup_fault' in f.reason_codes for f in snapshot.read_failures.values())
        assert snapshot.new_capacity.state=='unknown' and not state['new_buy_allowed']
        assert state['next_dependency_monotonic'] is None and state['cleanup_faults']==4 and state['pool_count']==8
        assert state['ready_count']==0 and len(state['scopes'])==4 and boundary.events==before
        assert all(not scheduler.run_due() for _ in range(100))
        assert len(trading.cancels)==3 and clock()==11
    finally:
        gate=False
        for release in releases.values():release.set()
        boundary.release.set();scheduler.stop();assert live.close(4)


def test_scheduler_wakes_deferred_read_fault_cancel_after_manual_cancel_completion(tmp_path,monkeypatch):
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_d2_execution_bridge import attach
    # Observe the approved public admission port while the real service remains
    # its binding owner and its actual safety lease/permission remains in force.
    class AdmissionObservedLive(LPD2LiveExecution):
        def bind_mutation_admission(self,owner,acquire):
            def observed(action):
                if not admitted:return None
                actual=acquire(action)
                return Lease(actual) if actual is not None else None
            return super().bind_mutation_admission(owner,observed)
    clock=Clock();boundary=Boundary(clock);trading=Trading();store=PredictionArbitrageStore(tmp_path)
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
    live=AdmissionObservedLive(store,trading,account_id='account',venue='polymarket',process_epoch='epoch',
        policy=CalculationPolicy(Decimal(100)),read_runtime=runtime,clock=clock,utc_now=clock.utc)
    live.initialize(ValidatedStartAdmission('epoch','offline-permit'))
    # Manual explicit routes read the external legacy source boundary. Keep its
    # source timestamps on the same isolated business clock as the real runtime.
    import open_trader.polymarket_lp_market_reader as reader_module
    from datetime import datetime
    class ClockMeta(type):
        def __instancecheck__(cls,value):return isinstance(value,datetime)
    class BusinessDateTime(datetime,metaclass=ClockMeta):
        @classmethod
        def now(cls,tz=None):return clock.utc() if tz is not None else clock.utc().replace(tzinfo=None)
    monkeypatch.setattr(reader_module,'datetime',BusinessDateTime)
    manual_reads=[];captured_orders=[]
    class ManualSources:
        def read(self,component,scope):
            manual_reads.append(component)
            if component=='open_orders':return tuple(captured_orders)
            if component=='positions':return ()
            if component=='balance':raise OSError('offline unavailable financial source')
            raise AssertionError(component)
    live.reader=MarketReader(ManualSources(),clock=clock)  # external source port, no internal read mocks
    targets={'A','B','C'}
    boundary.orders=[wire_row('A'),wire_row('B'),wire_row('C','3')]
    boundary.book_depth=1000  # isolate read faults from the original queue threshold
    producer=PolymarketLPService(live.store,trading,clock=clock.utc)
    execution=attach(live,producer,tmp_path);scheduler=LPAutoScheduler(execution,clock=clock.utc)
    execution.set_lp_auto_scheduler(scheduler)
    notices=[]
    def notice():
        state=live.read_runtime.status();scheduler.request_check();notices.append(state)
    execution.set_lp_auto_wakeup(notice)
    changed=threading.Condition();entered=set();finished=set();threads={}
    releases={oid:threading.Event() for oid in targets}
    protection_release=threading.Event();native_calls=set()
    active=0;maximum=0;admitted=False
    manual_phase=False;manual_calls=[];callers={};responses={};errors=[]
    actual_cancel=trading.cancel_orders_detailed
    def cancel(ids):
        nonlocal active,maximum
        assert len(ids)==1 and ids[0] in targets
        oid=ids[0]
        manual=threading.current_thread().name=='lp-d2-manual-cancel'
        if not manual_phase:return actual_cancel(ids)  # separately recorded setup protection ownership
        with changed:
            assert (manual,oid) not in native_calls,'same request/source must not replay'
            native_calls.add((manual,oid))
            entered.add(oid);threads[(manual,oid)]=threading.current_thread()
            active+=1;maximum=max(maximum,active);changed.notify_all()
        try:
            assert (releases[oid] if manual else protection_release).wait(4),'external native cancel release watchdog'
            if manual:
                manual_calls.append(ids)
                return dict(canceled=[],not_canceled={oid:'UNKNOWN'})
            return actual_cancel(ids)
        finally:
            with changed:active-=1;finished.add((manual,oid));changed.notify_all()
    trading.cancel_orders_detailed=cancel
    def requests():
        return {t.order_key[3]:t.request_id for p in live.state.snapshot().progress.values()
            for t in p.cancel_targets if t.order_key[3] in targets}
    class Lease:
        def __init__(self,actual):self.actual=actual
        def allowed(self,action=None):return admitted and self.actual.allowed(action)
        def release(self):
            self.actual.release()
    def drive_until(predicate):
        end=time.monotonic()+4
        while not predicate():
            scheduler.run_due()
            left=end-time.monotonic();assert left>0,(live.status(),boundary.events)
            with changed:changed.wait(min(.01,left))
    def collect_at(stamp,dependency,faults):
        clock.value=stamp;live.read_runtime.wake()
        boundary.wait(lambda:any(n['observed_monotonic']==stamp and n['ready_count']==4 for n in notices))
        assert scheduler.run_due()
        boundary.wait(lambda:any(n['observed_monotonic']==stamp and n['ready_count']==0
            and n['cleanup_faults']==faults and n['next_dependency_monotonic']==dependency for n in notices))
        scheduler.run_due()  # actual post-ACK notice, not a manufactured rescue
    try:
        assert scheduler.run_due()
        drive_until(lambda:len(live.state.snapshot().read_failures)==3
            and all(y.yield_pct_per_hour.state=='known' for y in live.state.snapshot().yields.values())
            and len(live.state.snapshot().yields)==3)
        # Start four real held attempts at5; timeout10 and cleanup fault11
        # preserve the original per-attempt5/6 boundaries.
        boundary.holds={('O','balance'),*((oid,'book') for oid in targets)}
        for key in boundary.holds:boundary.mode[key]='swallow'
        clock.value=5;live.read_runtime.wake()
        drive_until(lambda:all(('entered',oid,source,5) in boundary.events for oid,source in boundary.holds))
        collect_at(10,11,0);collect_at(11,None,4)
        state=live.read_runtime.status();snapshot=live.state.snapshot()
        assert not snapshot.current.effective_running and snapshot.new_capacity.state=='unknown'
        assert all('read_cleanup_fault' in f.reason_codes for f in snapshot.read_failures.values())
        assert len(state['scopes'])==4 and all(s['active'] and s['cleanup_fault'] for s in state['scopes'])
        assert state['next_dependency_monotonic'] is None and state['ready_count']==0 and state['waiting_for_event']
        assert not state['new_buy_allowed'] and state['pool_count']==8
        assert scheduler.snapshot()['next_check_at'] is None
        assert requests()=={} and trading.cancels==[] and not entered
        setup_source=snapshot.observation.order_view.orders_source
        captured_orders.extend(snapshot.observation.source_frame.orders)
        # Real public protection requests establish A/B UNKNOWN ownership before
        # the reported manual interleaving; no private cancel markers injected.
        admitted=True
        setup_ids={}
        for oid in ('A','B'):
            key=('account',CID,'yes',oid);failure=live.state.snapshot().read_failures[key]
            handle=live.cancel_independent(key,protection_key=failure.protection_key,reason='read_cleanup_fault')
            assert handle.result(4).status.value=='unknown_error'
            handle._thread.join(4);assert not handle._thread.is_alive()
            setup_ids[oid]=handle.identity.request_id
        assert requests()==setup_ids and trading.cancels==[('A',),('B',)]
        admitted=False
        assert scheduler.run_due()  # consume only setup's real completion notice
        assert scheduler.snapshot()['next_check_at'] is None and not scheduler.run_due()
        manual_phase=True
        def manual(oid):
            try:responses[oid]=execution.lp_cancel_orders(dict(confirm=True,order_ids=[oid]))
            except BaseException as exc:errors.append(exc)
        for oid in ('A','B'):
            callers[oid]=threading.Thread(target=manual,args=(oid,),name='public-manual-'+oid)
            callers[oid].start();boundary.wait(lambda:oid in entered)
        assert live.status()['native_inflight']==2 and all(t.is_alive() for t in callers.values())
        assert requests()==setup_ids
        current_source=live.state.snapshot().observation.order_view.orders_source
        assert (current_source.source_id,current_source.checked_at)!=(setup_source.source_id,setup_source.checked_at)
        before=list(boundary.events);reads_before=list(manual_reads)
        admitted=True
        # Manual reads accepted a new raw source: A/B get distinct protection
        # requests while their manual calls remain held; C enters independently.
        execution.lp_auto_run_once();assert scheduler.run_due()
        boundary.wait(lambda:native_calls=={(True,'A'),(True,'B'),*( (False,oid) for oid in targets)})
        assert set(requests())==targets and len(set(requests().values()))==3
        assert all(requests()[oid]!=setup_ids[oid] for oid in ('A','B'))
        assert live.status()['native_inflight']==5 and maximum==5
        assert all(t.is_alive() for t in callers.values()) and not finished
        assert manual_calls==[] and trading.cancels==[('A',),('B',)]
        assert live.diagnostics()['cancel_blocks']['by_reason'].get('native_capacity',0)==0
        assert boundary.events==before and manual_reads==reads_before and clock()==11
        pair={'A','B'};deferred='C'
        for oid in pair:releases[oid].set()
        for oid in pair:threads[(True,oid)].join(4);assert not threads[(True,oid)].is_alive()
        for oid in pair:callers[oid].join(4);assert not callers[oid].is_alive()
        assert errors==[] and set(responses)==pair and finished=={(True,oid) for oid in pair}
        assert sorted(manual_calls)==[('A',),('B',)]
        assert all(responses[oid]==dict(requested=1,canceled=[],not_canceled={oid:'UNKNOWN'},skipped=[]) for oid in pair)
        assert live.status()['native_inflight']==3 and not protection_release.is_set()
        state=live.read_runtime.status()
        assert state['next_dependency_monotonic'] is None and state['ready_count']==0
        assert len(state['scopes'])==4 and not any(s['key'][3]=='exact' for s in state['scopes'])
        assert boundary.events==before and manual_reads==reads_before and clock()==11
        assert scheduler.run_due(),'actual manual lease/native completion must wake protection at the same business time'
        assert live.status()['native_inflight']==3 and set(requests())==targets
        protection_release.set()
        for oid in targets:
            threads[(False,oid)].join(4);assert not threads[(False,oid)].is_alive()
        assert scheduler.run_due()  # consume actual C completion, then idle
        drive_until(lambda:scheduler.snapshot()['next_check_at'] is None)
        snapshot=live.state.snapshot();state=live.read_runtime.status()
        assert sorted(trading.cancels)==[('A',),('A',),('B',),('B',),('C',)]
        assert finished=={(True,'A'),(True,'B'),*((False,oid) for oid in targets)}
        assert all(requests()[oid]!=setup_ids[oid] for oid in ('A','B'))
        from tests.test_lp_cancel_lock_regressions import drained, action_rows
        drained(live.writer)
        audit={row['request_id']:row for row in action_rows(store)}
        for oid,request_id in setup_ids.items():
            assert audit[request_id]['order_id']==oid and audit[request_id]['receipt']['outcome']=='unknown'
        assert len({requests()[oid] for oid in targets}|set(setup_ids.values()))==5
        assert sorted(manual_calls)==[('A',),('B',)] and manual_reads==reads_before
        assert maximum==5 and live.status()['native_inflight']==0
        partial=next(o for o in snapshot.orders if o.key[3]=='C')
        assert (partial.original_size.value,partial.size_matched.value,partial.remaining.value)==(20,3,17)
        assert {o.key[3] for o in snapshot.active_orders}==targets and not snapshot.openings
        assert all(t.local_outcome=='unknown_error' for p in snapshot.progress.values() for t in p.cancel_targets)
        assert all('read_cleanup_fault' in f.reason_codes for f in snapshot.read_failures.values())
        assert snapshot.new_capacity.state=='unknown' and not state['new_buy_allowed']
        assert state['next_dependency_monotonic'] is None and state['cleanup_faults']==4 and state['pool_count']==8
        assert state['ready_count']==0 and len(state['scopes'])==4 and boundary.events==before
        assert all(not scheduler.run_due() for _ in range(100))
        assert len(trading.cancels)==5 and clock()==11
    finally:
        protection_release.set()
        for release in releases.values():release.set()
        for caller in callers.values():caller.join(4)
        boundary.release.set();scheduler.stop();assert live.close(4)


def test_protection_native_completion_notifies_owner_and_preserves_outcome(tmp_path,monkeypatch):
    from tests.test_lp_d2_integration import _d2_filled_opening
    live,sdk,public,clock=_d2_filled_opening(tmp_path,monkeypatch)
    posted=threading.Event();post_release=threading.Event()
    cancelled=threading.Event();cancel_release=threading.Event();woken=threading.Event()
    broken=False;handles=[]
    def notice():
        woken.set()
        if broken:raise RuntimeError('offline owner callback failure')
    live.set_read_wakeup(notice)
    post=sdk.post_order;cancel=sdk.cancel_orders
    def held_post(signed):
        assert signed['side']=='SELL' and signed['size']==60 and signed['order_type']=='GTC'
        posted.set();assert post_release.wait(4),'external passive SELL return watchdog'
        return post(signed)
    def held_cancel(*,order_ids):
        assert order_ids==('passive-exit',)
        cancelled.set();assert cancel_release.wait(4),'external cancel_exit return watchdog'
        return cancel(order_ids=order_ids)
    sdk.post_order=held_post;sdk.cancel_orders=held_cancel
    def tick():
        live.run_once();current=live.run_protection_once();handles.extend(current)
        return current
    try:
        assert live.read_runtime is None  # no unrelated asynchronous HTTP wake can satisfy this oracle
        clock[0]=2;public.bid=Decimal('.251')
        end=time.monotonic()+4;passive=None
        while passive is None:
            current=tick()
            passive=next((h for h in current if h.identity.kind=='passive_exit'),None)
            assert time.monotonic()<end
        assert posted.wait(4) and not passive.done()
        assert live.status()['native_inflight']==1 and not woken.is_set()
        post_release.set();actual=passive.result(4);passive._thread.join(4)
        assert passive.done() and (actual.status,actual.order_id)==('accepted','passive-exit')
        assert live.status()['native_inflight']==0
        assert woken.wait(4),'released passive SELL must notify the registered owner'
        snapshot=live.state.snapshot();scope=next(iter(snapshot.openings))
        assert snapshot.openings[scope].economics.opening_loss_usd.value==Decimal('4.94')
        assert snapshot.openings[scope].stop_loss_latched.value is False
        assert snapshot.sells[scope].passive.submit_quantity.value==60
        assert sdk.posts[-1]['size']==60 and sdk.posts[-1]['price']==Decimal('.32')
        assert sdk.posts[-1]['expiration'] is None and sdk.posts[-1]['post_only'] is True
        woken.clear();broken=True;clock[0]=3;public.bid=Decimal('.25')
        current=tick();exit_cancel,=current
        assert exit_cancel.identity.kind=='cancel_exit' and cancelled.wait(4)
        assert not exit_cancel.done() and live.status()['native_inflight']==1 and not woken.is_set()
        snapshot=live.state.snapshot()
        assert snapshot.openings[scope].stop_loss_latched.value is True
        assert snapshot.openings[scope].stop_loss_triggered_loss_usd.value==Decimal('5.00')
        assert sdk.market_orders==[]
        cancel_release.set();actual=exit_cancel.result(4);exit_cancel._thread.join(4)
        assert exit_cancel.done() and (actual.status,actual.order_id)==('accepted','passive-exit')
        assert woken.wait(4) and live.status()['native_inflight']==0  # callback raised after recording
        broken=False
        assert sdk.cancels==[('passive-exit',)] and sdk.market_orders==[]
        for h in tick():h.result(4);h._thread.join(4)
        assert sdk.market_orders==[]  # ID-only ACK cannot authorize replacement before exact final0
        sdk.allow_exact=True
        for _ in range(2):
            for h in tick():h.result(4);h._thread.join(4)
        snapshot=live.state.snapshot()
        assert len(sdk.market_orders)==1 and sdk.market_orders[0]['shares']==60
        assert sdk.market_orders[0]['min_price']==Decimal('.25')
        assert snapshot.sells[scope].passive.state=='terminal'
        assert snapshot.sells[scope].protected.submit_quantity.value==60
        for h in tick():h.result(4);h._thread.join(4)
        assert sdk.cancels==[('passive-exit',)] and len(sdk.market_orders)==1
    finally:
        post_release.set();cancel_release.set()
        assert live.close(4);live.trading.close()


def test_manual_entry_capacity_completion_notifies_owner_without_status_wakes(tmp_path,monkeypatch):
    import sqlite3,fcntl,pytest
    from tests.test_lp_d2_manual_bridge import manual_runtime,entry
    execution,live,sdk,public,clock=manual_runtime(tmp_path,monkeypatch)
    sdk_entered=threading.Event();sdk_release=threading.Event();returned=threading.Event()
    sql_entered=threading.Event();sql_release=threading.Event();woken=threading.Event()
    results=[];errors=[];command_threads=[];caller=None
    post=sdk.post_order;real_connect=sqlite3.connect
    execution.set_lp_auto_wakeup(woken.set)
    def held_post(signed):
        command_threads.append(threading.current_thread())
        assert signed['side']=='BUY' and signed['price']==Decimal('.29') and signed['size']==20
        assert signed['order_type']=='GTD' and signed['expiration'] is not None and signed['post_only'] is True
        sdk_entered.set();assert sdk_release.wait(6),'actual manual native post watchdog'
        result=post(signed);returned.set();return result
    sdk.post_order=held_post
    class HeldFinish(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if sql=='COMMIT' and returned.is_set() and threading.current_thread().name=='lp-d2-manual-command':
                sql_entered.set();assert sql_release.wait(6),'actual reliable manual-result SQL watchdog'
            return super().execute(sql,*args,**kwargs)
    def submit():
        try:results.append(execution.lp_submit_entry(entry(clock)))
        except BaseException as exc:errors.append(exc)
    try:
        assert live.read_runtime is None and not live.state.snapshot().current.effective_running
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3,'connect',lambda *args,**kwargs:real_connect(*args,**(kwargs|dict(factory=HeldFinish))))
            caller=threading.Thread(target=submit,name='public-manual-entry');caller.start()
            assert sdk_entered.wait(4) and caller.is_alive()
            assert live.status()['native_inflight']==1 and not woken.is_set() and sdk.posts==[]
            sdk_release.set();assert sql_entered.wait(4)
            assert live.status()['native_inflight']==0 and caller.is_alive() and len(sdk.posts)==1
            assert woken.wait(4),'actual manual-entry reservation release must notify before reliable SQL ends'
            assert live.state.snapshot().orders[0].key[3]=='manual-order-1'
            # Actual native capacity is free; reliable whole-command/file-lock
            # ownership continues until the real SQL completion, as before.
            with (tmp_path/'execution.lock').open('a+') as foreign:
                with pytest.raises(BlockingIOError):fcntl.flock(foreign.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            sql_release.set();caller.join(4);assert not caller.is_alive() and errors==[]
            for thread in command_threads:thread.join(4);assert not thread.is_alive()
        result,=results
        assert result['state']=='entry_open' and result['submit_status']=='accepted'
        assert result['entry_order_id']=='manual-order-1' and Decimal(result['price'])==Decimal('.29')
        assert Decimal(result['quantity'])==20
        with (tmp_path/'execution.lock').open('a+') as foreign:
            fcntl.flock(foreign.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(foreign.fileno(),fcntl.LOCK_UN)
        for handle in live.run_protection_once():handle.result(4);handle._thread.join(4)
        woken.clear()
        before=(sdk.balance_reads,sdk.order_reads,sdk.position_reads,public.book_reads,len(sdk.posts),tuple(sdk.cancels))
        for _ in range(20):
            assert execution.lp_status(result['session_id'])['entry_order_id']=='manual-order-1'
            execution.lp_auto_state(include_intents=False)
        assert not woken.is_set(),'snapshot queries must not become scheduler drivers'
        assert (sdk.balance_reads,sdk.order_reads,sdk.position_reads,public.book_reads,len(sdk.posts),tuple(sdk.cancels))==before
        assert execution.lp_submit_entry(entry(clock))==result  # same key replays response, never native work
        assert execution.lp_submit_entry(entry(clock,idempotency_key=''))['state']=='rejected'
        assert not woken.is_set() and len(sdk.posts)==1 and sdk.cancels==[]
        assert live.status()['native_inflight']==0 and live.control.status().fatal is None
    finally:
        sdk_release.set();sql_release.set()
        if caller:caller.join(4)
        assert live.close(4);live.trading.close()


import pytest


@pytest.mark.parametrize('variant',['rejected_post_sql_tail','missing_preview_sql_read'])
def test_scheduler_wakes_read_fault_cancel_after_manual_lease_release(tmp_path,monkeypatch,variant):
    import sqlite3,fcntl
    from datetime import timedelta
    from polymarket.models.clob.order_response import RejectedOrder
    from tests.test_lp_d2_manual_bridge import manual_runtime,entry,OTHER_CID,OTHER_TOKEN
    from tests.test_lp_order_registration_contract import _open_order,WALLET
    from tests.test_lp_d2_execution_bridge import attach
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    business=Clock()
    original,old,sdk,public,utc=manual_runtime(tmp_path,monkeypatch,monotonic_clock=business)
    assert old.close(4)
    base=utc[0];business.utc=lambda:utc[0]
    boundary=Boundary(business);boundary.book_depth=1000
    boundary.orders=[dict(wire_row('C','3'),market=OTHER_CID,asset_id=OTHER_TOKEN,maker_address=WALLET)]
    sdk.orders=(_open_order('C','BUY',price='.40',original='20',matched='3').model_copy(
        update={'condition_id':OTHER_CID,'token_id':OTHER_TOKEN}),)
    posted=threading.Event();post_release=threading.Event();native_returned=threading.Event()
    sql_entered=threading.Event();sql_release=threading.Event();early_notice=threading.Event()
    gate=False;caller=None;responses=[];errors=[];denied=[];command_threads=[];notices=[];cancelled=[];cancel_threads=[]
    class ObservedLive(LPD2LiveExecution):
        def bind_mutation_admission(self,owner,acquire):
            def observe(action):
                nonlocal gate
                actual=acquire(action)  # unchanged real service permission/exclusive lease
                if action=='CANCEL' and actual is None and gate:
                    gate=False
                    assert sql_entered.is_set() and not sql_release.is_set() and caller.is_alive()
                    assert self.status()['native_inflight']==0 and not cancelled
                    assert not any(t.order_key[3]=='C' for p in self.state.snapshot().progress.values() for t in p.cancel_targets)
                    denied.append(business())
                    sql_release.set();caller.join(4);assert not caller.is_alive(),'public command return watchdog'
                    for thread in command_threads:thread.join(4);assert not thread.is_alive()
                    assert errors==[]
                return actual  # preserve the actual refusal for this protection pass
            return super().bind_mutation_admission(owner,observe)
    runtime=LPReadRuntime(boundary.factory,clock=business,utc_now=business.utc)
    live=ObservedLive(old.store,old.trading,account_id=WALLET,venue='polymarket',process_epoch='p16-'+variant,
        policy=CalculationPolicy(None),read_runtime=runtime,clock=business,utc_now=business.utc)
    live.initialize(ValidatedStartAdmission('p16-'+variant,'offline-permit'))
    execution=attach(live,original._lp,tmp_path);scheduler=LPAutoScheduler(execution,clock=business.utc)
    execution.set_lp_auto_scheduler(scheduler)
    def notice():
        scheduler.request_check();notices.append(runtime.status())
        if native_returned.is_set() and sql_entered.is_set() and not sql_release.is_set():early_notice.set()
    execution.set_lp_auto_wakeup(notice)
    real_connect=sqlite3.connect
    class HeldSQL(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            manual=threading.current_thread().name=='lp-d2-manual-command'
            tail=variant=='rejected_post_sql_tail' and sql=='COMMIT' and native_returned.is_set()
            preview=variant=='missing_preview_sql_read' and sql=='SELECT * FROM previews WHERE preview_id=?'
            if manual and (tail or preview):
                if threading.current_thread() not in command_threads:command_threads.append(threading.current_thread())
                sql_entered.set();assert sql_release.wait(6),'actual manual SQL release watchdog'
            return super().execute(sql,*args,**kwargs)
    def post(signed):
        assert variant=='rejected_post_sql_tail'
        assert signed['price']==Decimal('.29') and signed['size']==20 and signed['order_type']=='GTD'
        assert signed['post_only'] is True and signed['expiration'] is not None
        command_threads.append(threading.current_thread());posted.set()
        assert post_release.wait(6),'actual manual post return watchdog'
        sdk.posts.append(signed);native_returned.set()
        return RejectedOrder(ok=False,code='post_only_would_cross',message='offline venue rejection')
    sdk.post_order=post
    def cancel(*,order_ids):
        assert order_ids==('C',)
        cancelled.append(order_ids);cancel_threads.append(threading.current_thread())
        raise TimeoutError('offline UNKNOWN cancel')
    sdk.cancel_orders=cancel
    def submit():
        try:
            responses.append(execution.lp_submit_entry(entry(utc)) if variant=='rejected_post_sql_tail'
                else execution.lp_start('missing-preview','p16-missing-preview'))
        except BaseException as exc:errors.append(exc)
    def drive_until(predicate):
        end=time.monotonic()+4
        while not predicate():
            scheduler.run_due()
            left=end-time.monotonic();assert left>0,(runtime.status(),scheduler.snapshot(),errors,responses)
            with boundary.changed:boundary.changed.wait(min(.005,left))
    def at(stamp):
        business.value=stamp;utc[0]=base+timedelta(seconds=stamp);runtime.wake()
    def collect_at(stamp,faults,dependency):
        at(stamp)
        boundary.wait(lambda:any(n['observed_monotonic']==stamp and n['ready_count']==2 for n in notices))
        assert scheduler.run_due()
        boundary.wait(lambda:any(n['observed_monotonic']==stamp and n['ready_count']==0
            and n['cleanup_faults']==faults and n['next_dependency_monotonic']==dependency for n in notices))
        scheduler.run_due()  # consume actual read/ACK notices while exclusive lease remains held
    try:
        assert scheduler.run_due()
        drive_until(lambda:len(live.state.snapshot().read_failures)==1 and runtime.status()['ready_count']==0)
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3,'connect',lambda *a,**kw:real_connect(*a,**(kw|dict(factory=HeldSQL))))
            caller=threading.Thread(target=submit,name='public-manual-'+variant);caller.start()
            assert (posted if variant=='rejected_post_sql_tail' else sql_entered).wait(4),(errors,responses)
            assert caller.is_alive() and live.status()['native_inflight']==(1 if variant=='rejected_post_sql_tail' else 0)
            with (tmp_path/'execution.lock').open('a+') as foreign:
                with pytest.raises(BlockingIOError):fcntl.flock(foreign.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            boundary.holds={('O','balance'),('C','book')}
            for key in boundary.holds:boundary.mode[key]='swallow'
            at(5);drive_until(lambda:all(('entered',oid,source,5) in boundary.events for oid,source in boundary.holds))
            scopes=runtime.status()['scopes']
            attempts={scope['key']:scope['attempt'] for scope in scopes}
            assert len(scopes)==2 and {key[3:5] for key in attempts}=={('financial','O'),('buy','C')}
            assert all(key[:3]==(WALLET,'p16-'+variant,live.state.snapshot().current.cycle_generation) for key in attempts)
            def unheld_sources_published():
                state=runtime.status();current=state['scopes']
                # Both held children remain pending. Two active parents plus
                # those children means every unheld source has published,
                # rather than merely returned from its external transport.
                return (business()==5 and state['managed_tasks']==4 and len(current)==2
                    and {scope['key']:scope['attempt'] for scope in current}==attempts
                    and all(scope['active'] and scope['started']==5 and not scope['retiring']
                        and not scope['cleanup_fault'] for scope in current))
            boundary.wait(unheld_sources_published)
            collect_at(10,0,11);collect_at(11,2,None)
            assert cancelled==[]
            if variant=='rejected_post_sql_tail':
                post_release.set();assert sql_entered.wait(4),(errors,responses)
                assert live.status()['native_inflight']==0 and caller.is_alive()
                # P14's early notice remains required; it can precede the SQL
                # barrier, so observe its actual owner eligibility rather than count callbacks.
                assert scheduler.snapshot()['next_check_at'] is not None
            state=runtime.status();snapshot=live.state.snapshot()
            assert len(state['scopes'])==2 and all(s['active'] and s['cleanup_fault'] for s in state['scopes'])
            assert state['ready_count']==0 and state['next_dependency_monotonic'] is None and state['waiting_for_event']
            assert state['pool_count']==4 and not state['new_buy_allowed'] and not snapshot.openings
            assert snapshot.new_capacity.state=='unknown'
            assert snapshot.read_failures[(WALLET,OTHER_CID,OTHER_TOKEN,'C')].reason_codes==('read_cleanup_fault',)
            before=list(boundary.events)
            gate=True;assert scheduler.run_due()  # actual CANCEL refusal releases real external SQL and lease
            assert not gate and denied==[11] and not caller.is_alive() and errors==[]
            assert all(not thread.is_alive() for thread in command_threads)
            assert live.status()['native_inflight']==0 and cancelled==[] and business()==11
            assert not any(p.cancel_targets for p in live.state.snapshot().progress.values())
            if variant=='rejected_post_sql_tail':
                result,=responses
                assert result['state']=='rejected' and result['reason']=='entry_submit_rejected' and result['submit_status']=='rejected'
                assert len(sdk.posts)==1
            else:
                assert responses==[dict(state='rejected',reason='preview_not_found')]
                assert sdk.posts==[] and not posted.is_set() and not native_returned.is_set()
                assert live.store.lp_manual_command(WALLET,'p16-missing-preview') is None
            with (tmp_path/'execution.lock').open('a+') as foreign:
                fcntl.flock(foreign.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                fcntl.flock(foreign.fileno(),fcntl.LOCK_UN)
            state=runtime.status()
            assert len(state['scopes'])==2 and state['ready_count']==0 and state['next_dependency_monotonic'] is None
            assert not any(s['key'][3]=='exact' for s in state['scopes']) and boundary.events==before
            assert scheduler.run_due(),'actual manual lease release must wake C without a command/time/read rescue'
            boundary.wait(lambda:cancelled==[('C',)])
            for thread in cancel_threads:thread.join(4);assert not thread.is_alive()
            assert scheduler.run_due()  # consume C's actual native completion
            drive_until(lambda:scheduler.snapshot()['next_check_at'] is None)
            snapshot=live.state.snapshot();state=runtime.status()
            order,=snapshot.active_orders
            assert order.key==(WALLET,OTHER_CID,OTHER_TOKEN,'C')
            assert (order.original_size.value,order.size_matched.value,order.remaining.value)==(20,3,17)
            target,=[t for p in snapshot.progress.values() for t in p.cancel_targets]
            assert target.order_key==order.key and target.request_id and target.local_outcome=='unknown_error'
            assert snapshot.new_capacity.state=='unknown' and not state['new_buy_allowed'] and not snapshot.openings
            assert state['cleanup_faults']==2 and state['pool_count']==4 and len(state['scopes'])==2
            assert state['ready_count']==0 and state['next_dependency_monotonic'] is None
            assert boundary.events==before and business()==11 and cancelled==[('C',)]
            assert all(not scheduler.run_due() for _ in range(100))
            assert execution.lp_submit_entry(dict(idempotency_key=''))['state']=='rejected'
            assert all(not scheduler.run_due() for _ in range(10)) and cancelled==[('C',)]
    finally:
        gate=False;post_release.set();sql_release.set();boundary.release.set()
        if caller:caller.join(4)
        scheduler.stop();assert live.close(4);live.trading.close()
