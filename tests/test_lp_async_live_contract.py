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
            for _ in range(3):live.run_once();live.run_protection_once()
            assert trading.cancels==[('A',)]
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
        admitted=[False]
        class Lease:
            def allowed(self,action=None):return admitted[0]
            def release(self):pass
        live.bind_mutation_admission(trading,lambda action:Lease() if admitted[0] else None)
        try:
            if trigger=='eleven':
                for number in range(1,12):
                    advance(live,boundary,clock,(number-1)*5,lambda:count(live)==number)
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
            end=time.monotonic()+4
            while len(trading.cancels)<3:
                live.run_protection_once();assert time.monotonic()<end
                trading.cancelled.wait(.01)
            assert sorted(trading.cancels)==[('A',),('manual',),('partial',)],repr([(k.kind,p.reason_codes,[t.order_key[3] for t in p.cancel_targets]) for k,p in live.state.snapshot().progress.items()])
            for _ in range(3):live.run_protection_once()
            assert len(trading.cancels)==3
            partial=next(o for o in live.state.snapshot().orders if o.key[3]=='partial')
            assert partial.remaining.value==17 and partial.size_matched.value==3
            assert all('peer' not in ids and 'sell' not in ids for ids in trading.cancels)
        finally:boundary.release.set();assert live.close(4)

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
        live.read_runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
        start=clock()
        def position_ready():
            observation=live.state.snapshot().observation
            return any(k.startswith('market_position:') and v.complete and v.checked_at==clock.utc()
                for k,v in observation.source_frame.sources.items())
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
        rows=[]
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
                if stamp==10:qualify('F')
                while True:
                    started=time.monotonic();result=execution.lp_auto_scheduled_check()
                    assert time.monotonic()-started<1, 'scheduled owner waited for held HTTP'
                    y=next((y for k,y in live.state.snapshot().yields.items() if k[3]=='B'),None)
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
                for outcome in result.rotation_outcomes:outcome.handle.result(4)
                assert sdk.cancels==[('B',)],(result,live.rotation_diagnostics(),live.status())
            else:
                assert 'B' not in result.selection.victim_order_ids
                assert sdk.cancels==[]
            live.dispatch_rotation(result);live.dispatch_rotation(result)
            assert sdk.cancels==([('B',)] if case=='replace-zero' else [])
            assert auth and wake.wait(1)
        finally:boundary.release.set();assert live.close(4);live.trading.close()


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
