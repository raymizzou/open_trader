"""LPCANCEL-FLOW-P2: offline public runtime, SDK and SQLite contracts."""
from decimal import Decimal
import socket
import pytest
from open_trader.polymarket_lp_live_execution import RotationDecision, RunResult
from tests.test_lp_observed_pools import PoolRig
from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
from open_trader.polymarket_lp_market_reader import MarketReader
from open_trader.polymarket_lp_market_calculation import CalculationPolicy
from open_trader.polymarket_lp import PolymarketLPService
from tests.test_lp_d2_execution_bridge import attach
from tests.test_lp_cancel_lock_regressions import finished, drained, action_rows


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('cancel-flow tests must not connect to a network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


class CancelRig(PoolRig):
    def __init__(self, tmp_path, monkeypatch):
        from datetime import UTC, datetime, timedelta
        self.date = [datetime.now(UTC)]
        self.seconds = [0.]
        super().__init__(tmp_path, monkeypatch, clock=lambda:self.date[0], build_owner=False)
        self.live = LPD2LiveExecution(self.store, self.adapter,
            account_id=self.adapter.config.wallet_address, venue='polymarket', process_epoch='cancel-flow',
            policy=CalculationPolicy(None), load_settings_on_start=True,
            reader=MarketReader(self.adapter.lp_reader_source_port()),
            utc_now=lambda:self.date[0], clock=lambda:self.seconds[0])
        self.producer = PolymarketLPService(self.store,self.adapter,clock=lambda:self.date[0])
        self.execution = attach(self.live,self.producer,tmp_path)
        self.live.initialize(ValidatedStartAdmission('cancel-flow','offline-permit'))
        self.live.run_once()


def group_request(rig, names):
    snapshot = rig.live.state.snapshot()
    orders = tuple(o for o in snapshot.active_orders if o.key[3] in names)
    assert len(orders) == len(names)
    observation = snapshot.observation
    return rig.live.dispatch_rotation(RunResult(None, rotation=RotationDecision(
        rig.live.account_id, observation.cycle_generation, observation.observation_id, orders)))


@pytest.mark.parametrize('case', ['group_mixed', 'single_omitted'])
def test_cancel_response_uses_exact_per_id_result(tmp_path, monkeypatch, case):
    rig = CancelRig(tmp_path, monkeypatch)
    names = 'ABCD' if case == 'group_mixed' else 'A'
    try:
        for name in names:
            rig.market(name)
            rig.add_order(name, '8', order_id=name)
        rig.live.run_once()
        def cancel(*, order_ids):
            rig.sdk.cancels.append(order_ids)
            return dict(canceled=['A', 'D'] if case == 'group_mixed' else [],
                not_canceled={'B': 'order not found; secret venue payload', 'D': 'conflict'})
        rig.sdk.cancel_orders = cancel
        if case == 'group_mixed':
            result = group_request(rig, names)
            outcomes = {o.order_key[3]: finished(o.handle) for o in result.rotation_outcomes}
            assert {k: v.status.value for k,v in outcomes.items()} == {
                'A':'accepted', 'B':'rejected', 'C':'unknown_error', 'D':'unknown_error'}
            assert outcomes['B'].reason_code == 'cancel_rejected'
        else:
            key = next(o.key for o in rig.live.state.snapshot().active_orders if o.key[3]=='A')
            assert finished(rig.live.cancel_independent(key)).status.value == 'unknown_error'
        drained(rig.live.writer)
        rows = {r['order_id']:r for r in action_rows(rig.store) if r['kind']=='CANCEL'}
        assert set(rows)==set(names)
        for oid,row in rows.items():
            assert row['receipt']['request_id']==row['request_id']
            assert row['receipt']['order_id']==oid
            assert 'secret venue payload' not in str(row)
            assert 'progress' not in row  # ACK alone cannot invent final quantities
        if case=='group_mixed':
            assert rows['B']['receipt']['outcome']=='rejected'
            assert rows['B']['receipt']['reason_code']=='cancel_rejected'
    finally:
        rig.close()


@pytest.mark.parametrize('kind', ['trial_read_failure', 'income_queue'])
def test_fresh_facts_allow_new_decision_after_protection_unknown(tmp_path, monkeypatch, kind):
    if kind=='trial_read_failure':
        from tests.test_lp_async_live_contract import live_owner, advance, count
        clock,boundary,trading,live=live_owner(tmp_path)
        boundary.book_depth=1000
        boundary.mode[('A','native_rewards')]='failure'
        try:
            for number in range(1,12):
                advance(live,boundary,clock,(number-1)*5,lambda:count(live)==number)
            # A rejected durable registration never enters the SDK and must not
            # consume the raw fact's send eligibility or retain an audit parent.
            with monkeypatch.context() as denied:
                denied.setattr(live.store, 'lp_log_business_record', lambda record: False)
                refused, = live.run_protection_once()
                assert finished(refused).status.value == 'not_sent'
                assert trading.cancels == []
            handles=live.run_protection_once()
            assert len(handles)==1
            first=finished(handles[0])
            assert first.status.value=='unknown_error'
            drained(live.writer)
            original=next(r for r in action_rows(live.store) if r['request_id']==first.identity.request_id)
            boundary.mode[('A','native_rewards')]='success'
            advance(live,boundary,clock,55,lambda:count(live)==0)
            handles=live.run_protection_once()
            assert len(handles)==1
            second=finished(handles[0])
            assert second.identity.request_id!=first.identity.request_id
            assert trading.cancels==[('A',),('A',)]
            saved=next(s for s in live.store.lp_sessions() if 'A' in s.get('lp_read_protection',{}))
            assert saved['lp_read_protection']['A']['cancel_request_id']==second.identity.request_id
            targets=[t for p in live.state.snapshot().progress.values() for t in p.cancel_targets if t.order_key[3]=='A']
            assert [t.request_id for t in targets]==[second.identity.request_id]
            drained(live.writer)
            assert next(r for r in action_rows(live.store) if r['request_id']==first.identity.request_id)['receipt']==original['receipt']
            assert 'read_failure_limit' in live.state.snapshot().read_failures[('account',__import__('tests.test_lp_async_live_contract',fromlist=['CID']).CID,'yes','A')].reason_codes
        finally:
            boundary.release.set();assert live.close(4)
    else:
        from tests.test_lp_d2_integration import _d2_protection_runtime
        live,sdk,public,clock=_d2_protection_runtime(tmp_path,monkeypatch)
        try:
            # Current settings select income; protection itself remains per exact BUY.
            live.store.lp_save_current_settings(live._settings_account(),expected_config_version=0,
                changes=dict(trial_budget_usd='10000',income_budget_usd='10000'))
            live.load_settings()
            live.run_once();assert live.run_protection_once()==()
            def unknown(*,order_ids):
                sdk.cancels.append(order_ids);raise TimeoutError('offline')
            sdk.cancel_orders=unknown
            clock[0]=1;public.level=Decimal('4000')
            live.run_once(pool='income')
            first,=live.run_protection_once();a=finished(first)
            assert a.status.value=='unknown_error'
            clock[0]=2;public.level=Decimal('10000')
            live.run_once(pool='income')
            second,=live.run_protection_once();b=finished(second)
            assert a.identity.request_id!=b.identity.request_id
            assert sdk.cancels==[('entry-A',),('entry-A',)]
            drained(live.writer)
            rows={r['request_id']:r for r in action_rows(live.store) if r['kind']=='CANCEL'}
            assert rows[a.identity.request_id]['receipt']['outcome']=='unknown'
        finally:
            assert live.close(5);live.trading.close()


@pytest.mark.parametrize('kind', ['single', 'group'])
def test_terminal_progress_reaches_original_cancel_audit(tmp_path, monkeypatch, kind):
    rig=CancelRig(tmp_path,monkeypatch)
    names='A' if kind=='single' else 'AB'
    try:
        for name in names:
            rig.market(name);rig.add_order(name,'8',order_id=name)
        rig.live.run_once()
        if kind=='single':
            key=rig.live.state.snapshot().active_orders[0].key
            handles=[rig.live.cancel_independent(key)]
        else:
            handles=[o.handle for o in group_request(rig,names).rotation_outcomes]
        outcomes=[finished(h) for h in handles]
        assert all(o.status.value=='accepted' for o in outcomes)
        drained(rig.live.writer)
        before={r['order_id']:r for r in action_rows(rig.store) if r['kind']=='CANCEL'}
        assert all('progress' not in r for r in before.values())
        rig.release(*names)
        for name in names:
            rig.history[name]={**rig.history[name],'size_matched':Decimal('3' if name=='A' else '5')}
        rig.live.run_once()
        rig.live.run_protection_once()
        drained(rig.live.writer)
        rows={r['order_id']:r for r in action_rows(rig.store) if r['kind']=='CANCEL'}
        for name in names:
            row=rows[name];progress=row.get('progress')
            assert progress is not None, row
            assert progress['size_matched']==('3' if name=='A' else '5')
            assert progress['remaining']==('17' if name=='A' else '15')
            assert progress['reserved_usd']=='0'
            assert progress['status']=='CANCELED'
            assert row['receipt']==before[name]['receipt']
        status=drained(rig.live.writer)
        assert status['failed_entries']==status['dropped_parent']==status['dropped_overflow']==0
    finally:rig.close()


def test_new_request_and_late_facts_preserve_original_audit_identity(tmp_path,monkeypatch):
    from datetime import timedelta
    from open_trader.lp_business_writer import ProgressRecord, ReceiptRecord
    from tests.test_lp_d2_integration import _d2_protection_runtime
    from tests.test_lp_order_registration_contract import WALLET,CONDITION_ID,TOKEN_ID,_open_order
    live,sdk,public,clock=_d2_protection_runtime(tmp_path,monkeypatch,quantity='20')
    key=(WALLET,CONDITION_ID,TOKEN_ID,'entry-A')
    try:
        live.run_once();drained(live.writer)
        buy=next(r for r in action_rows(live.store) if r['kind']=='BUY')
        def unknown(*,order_ids):sdk.cancels.append(order_ids);raise TimeoutError('offline')
        sdk.cancel_orders=unknown
        a=finished(live.cancel_independent(key))
        clock[0]=1;live.run_once()
        b=finished(live.cancel_independent(key))
        assert a.identity.request_id!=b.identity.request_id
        clock[0]=2
        sdk.orders=(_open_order('entry-A','BUY',price='.30',original='20',matched='3').model_copy(update={'status':'CANCELED'}),)
        live.run_once();live.run_protection_once();drained(live.writer)
        rows={r['request_id']:r for r in action_rows(live.store)}
        for request in (a.identity.request_id,b.identity.request_id):
            assert rows[request]['progress']['size_matched']=='3'
            assert rows[request]['receipt']['outcome']=='unknown'
        original=rows[a.identity.request_id]
        late_at=live._utc_now()-timedelta(seconds=1)
        live.writer.offer(ProgressRecord(WALLET,live.process_epoch,a.identity.request_id,
            original['session_id'],CONDITION_ID,TOKEN_ID,'entry-A',Decimal(20),Decimal(0),Decimal('.30'),'LIVE',late_at))
        live.writer.offer(ReceiptRecord(WALLET,live.process_epoch,a.identity.request_id,
            original['session_id'],CONDITION_ID,TOKEN_ID,'entry-A','unknown',live._utc_now()))
        drained(live.writer)
        latest={r['request_id']:r for r in action_rows(live.store)}
        assert latest[a.identity.request_id]['progress']==original['progress']
        assert latest[buy['request_id']]['kind']=='BUY'
        assert latest[buy['request_id']]['receipt']==buy['receipt']
        clock[0]=3
        sdk.orders=(_open_order('replacement','BUY',price='.30',original='20'),)
        live.run_once()
        assert any(o.key[3]=='replacement' for o in live.state.snapshot().active_orders)
        assert all(r['order_id']=='entry-A' for r in latest.values() if r['kind']=='CANCEL')
    finally:assert live.close(5);live.trading.close()


@pytest.mark.parametrize('state', ['live','absent','partial_fill'])
def test_next_api_facts_continue_without_old_unknown_gate(tmp_path,monkeypatch,state):
    from tests.test_lp_d2_integration import _d2_protection_runtime
    from tests.test_lp_order_registration_contract import WALLET,CONDITION_ID,TOKEN_ID,_open_order
    live,sdk,public,clock=_d2_protection_runtime(tmp_path,monkeypatch,quantity='20')
    try:
        live.run_once();live.run_protection_once()
        def unknown(*,order_ids):sdk.cancels.append(order_ids);raise TimeoutError('offline')
        sdk.cancel_orders=unknown
        public.level=Decimal('40');clock[0]=1;live.run_once()
        first,=live.run_protection_once();first_result=finished(first)
        assert first_result.status.value=='unknown_error'
        clock[0]=2;public.level=Decimal('10000')
        if state=='absent':sdk.orders=()
        if state=='partial_fill':
            sdk.orders=(_open_order('entry-A','BUY',price='.30',original='20',matched='3'),)
            sdk.positions=(dict(user=WALLET,condition_id=CONDITION_ID,token_id=TOKEN_ID,size='3',average_price='.30',redeemable=False),)
        live.run_once();handles=live.run_protection_once()
        if state=='absent':
            assert handles==()
            assert not any(o.key[3]=='entry-A' for o in live.state.snapshot().active_orders)
        else:
            second,=handles;result=finished(second)
            assert result.identity.request_id!=first_result.identity.request_id
            target=next(t for p in live.state.snapshot().progress.values() for t in p.cancel_targets if t.request_id==result.identity.request_id)
            assert target.requested_remaining.value==Decimal('17' if state=='partial_fill' else '20')
        before=list(sdk.cancels)
        assert live.run_protection_once()==()
        assert sdk.cancels==before
        assert not any(p['side']=='SELL' for p in sdk.posts)
        drained(live.writer)
        row=next(r for r in action_rows(live.store) if r['request_id']==first_result.identity.request_id)
        assert row['receipt']['outcome']=='unknown'
        assert row.get('progress',{}).get('status')!='CANCELED'
        if state=='partial_fill':
            assert live.state.snapshot().positions[0].quantity.value==3
    finally:assert live.close(5);live.trading.close()


@pytest.mark.parametrize('pool',['trial','income'])
def test_shared_facts_and_cancel_dispatch_add_no_reads(tmp_path,monkeypatch,pool):
    from tests.test_lp_observed_pools import AsyncPoolRig
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for name in 'AB':rig.market(name);rig.add_order(name,'8',order_id=name)
        rig.tick()
        rig.converge_current(lambda: len(rig.live.state.snapshot().active_orders)==2
            and rig.live.read_runtime.status()['ready_count']==0
            and not any(s['active'] for s in rig.live.read_runtime.status()['scopes']))
        before=tuple(rig.requests)
        if pool=='trial':
            handles=[rig.live.cancel_independent(o.key) for o in rig.live.state.snapshot().active_orders]
        else:handles=[o.handle for o in group_request(rig,'AB').rotation_outcomes]
        assert all(finished(h).status.value=='accepted' for h in handles)
        assert tuple(rig.requests)==before
        # Both pools consumed one canonical account observation and one market read per BUY.
        financial=[(source,when) for source,oid,when in before if source in ('balance','open_orders','positions')]
        assert len(financial)==len(set(financial))
        book=[(oid,when) for source,oid,when in before if source=='book' and oid in 'AB']
        assert len(book)==len(set(book))
        assert set(oid for batch in rig.sdk.cancels for oid in batch)==set('AB')
    finally:rig.close()


def triggered_read_owner(tmp_path):
    from tests.test_lp_async_live_contract import live_owner,advance,count
    clock,boundary,trading,live=live_owner(tmp_path)
    boundary.book_depth=1000
    boundary.mode[('A','native_rewards')]='failure'
    for number in range(1,12):
        advance(live,boundary,clock,(number-1)*5,lambda:count(live)==number)
    first,=live.run_protection_once();finished(first)
    return clock,boundary,trading,live,first


def test_no_new_cancel_without_new_eligible_facts(tmp_path,monkeypatch):
    from tests.test_lp_async_live_contract import advance,count
    clock,boundary,trading,live,first=triggered_read_owner(tmp_path)
    try:
        for _ in range(3):
            live.run_once();assert live.run_protection_once()==()
        assert trading.cancels==[('A',)]
        drained(live.writer)
        assert len([r for r in action_rows(live.store) if r.get('kind')=='CANCEL'])==1
        # Change one immutable term at a time. Wait for the actual new
        # complete financial source before testing the old protection intent.
        import time
        original=dict(boundary.orders[0])
        boundary.mode[('A','native_rewards')]='success'
        for stamp,change in ((55,{'price':'.39'}),(60,{'original_size':'21'}),
                (65,{'order_type':'GTD','expiration':'1940000000'})):
            boundary.orders[0]={**original,**change}
            clock.value=stamp;live.read_runtime.wake()
            deadline=time.monotonic()+4
            while True:
                live.run_once()
                observation=live.state.snapshot().observation
                if (observation is not None and observation.orders_complete
                        and observation.order_view.orders_source.checked_at==clock.utc()):break
                assert time.monotonic()<deadline,'fresh changed-terms source publication watchdog'
                with boundary.changed:boundary.changed.wait(.001)
            assert live.run_protection_once()==()
            assert trading.cancels==[('A',)]
            drained(live.writer)
            assert len([r for r in action_rows(live.store) if r.get('kind')=='CANCEL'])==1
        boundary.orders[0]={**boundary.orders[0],'maker_address':'different-owner'}
        clock.value=70;live.read_runtime.wake()
        for _ in range(3):live.run_once();live.run_protection_once()
        assert trading.cancels==[('A',)]
        drained(live.writer)
        assert len([r for r in action_rows(live.store) if r.get('kind')=='CANCEL'])==1
        clock.value=160
        for _ in range(3):live.run_once();live.run_protection_once()
        assert trading.cancels==[('A',)]
        drained(live.writer)
        assert len([r for r in action_rows(live.store) if r.get('kind')=='CANCEL'])==1
    finally:boundary.release.set();assert live.close(4)


def test_restart_uses_fresh_facts_without_replaying_old_cancel(tmp_path):
    from tests.test_lp_async_live_contract import live_owner,advance,count,wire_row
    clock,boundary,trading,live,first=triggered_read_owner(tmp_path)
    drained(live.writer)
    old={r['request_id']:r for r in action_rows(live.store) if r.get('kind')=='CANCEL'}
    boundary.release.set();assert live.close(4)
    clock,boundary,trading,live=live_owner(tmp_path)
    boundary.book_depth=1000
    boundary.orders.append(wire_row('manual-sell',side='SELL'))
    try:
        assert trading.cancels==[]
        assert live.run_protection_once()==()
        advance(live,boundary,clock,0,lambda:count(live)==0)
        handle,=live.run_protection_once();result=finished(handle)
        assert result.identity.request_id not in old
        assert trading.cancels==[('A',)]
        drained(live.writer)
        rows={r['request_id']:r for r in action_rows(live.store) if r.get('kind')=='CANCEL'}
        for request,row in old.items():assert rows[request]['receipt']==row['receipt']
        assert all(o.key[3]!='manual-sell' for o in live.state.snapshot().active_orders if o.side=='BUY')
    finally:boundary.release.set();assert live.close(4)


@pytest.mark.parametrize('pool',['trial','income'])
def test_fresh_live_facts_allow_overlapping_cancel_without_kill(tmp_path,monkeypatch,pool):
    import threading
    from datetime import timedelta
    if pool=='trial':
        from types import SimpleNamespace
        from tests.test_lp_d2_integration import _d2_protection_runtime
        live,sdk,public,clock=_d2_protection_runtime(tmp_path,monkeypatch)
        live.run_once();assert live.run_protection_once()==()
        def close():
            assert live.close(5)
            live.trading.close()
        rig=SimpleNamespace(live=live,sdk=sdk,store=live.store,close=close)
    else:
        rig=CancelRig(tmp_path,monkeypatch)
    release=[threading.Event(),threading.Event()]
    entered=[threading.Event(),threading.Event()]
    handles=[];names='A' if pool=='trial' else 'AB'
    lock=threading.Lock()
    def cancel(*,order_ids):
        with lock:
            index=len(rig.sdk.cancels);rig.sdk.cancels.append(order_ids)
        assert index<2,'same raw facts must never cause a third HTTP request'
        entered[index].set()
        assert release[index].wait(8),'external cancel release watchdog'
        return dict(canceled=order_ids,not_canceled={})
    try:
        if pool=='income':
            for name in names:rig.market(name);rig.add_order(name,'8',order_id=name)
            rig.live.run_once()
        else:
            public.level=Decimal('4000');clock[0]=1;live.run_once()
        rig.sdk.cancel_orders=cancel
        def dispatch():
            if pool=='income':
                result=group_request(rig,names)
                return result.rotation_outcomes[0].handle if result.rotation_outcomes else None
            attempts=live.run_protection_once()
            return attempts[0] if attempts else None
        first=dispatch();handles.append(first);assert entered[0].wait(3)
        if pool=='income':
            rig.date[0]+=timedelta(seconds=1);rig.seconds[0]+=1
        else:
            public.level=Decimal('10000');clock[0]=2
        rig.live.run_once()
        second=dispatch()
        assert second is not None,'new facts must create a request while old native cancel is alive'
        handles.append(second);assert entered[1].wait(3)
        assert not first.done() and not second.done()
        assert first.identity.request_id!=second.identity.request_id
        assert rig.live.status()['native_inflight']==2
        if pool=='trial':
            targets=[t for p in live.state.snapshot().progress.values() for t in p.cancel_targets]
            assert len(targets)==2 and all(t.native_state=='running' for t in targets)
        assert dispatch() is None
        assert len(rig.sdk.cancels)==2
        release[1].set();finished(second)
        assert rig.live.status()['native_inflight']==1
        assert not first.done()
        release[0].set();finished(first)
        assert rig.live.status()['native_inflight']==0
        drained(rig.live.writer)
        rows=[r for r in action_rows(rig.store) if r['kind']=='CANCEL']
        assert len(rows)==2*len(names)
        assert len({r['request_id'] for r in rows})==len(rows)
        assert all(r['receipt']['outcome']=='accepted' for r in rows)
    finally:
        for event in release:event.set()
        for handle in handles:
            if handle is not None:handle._thread.join(3)
        rig.close()


def test_cancel_dispatch_is_independent_of_two_placement_slots(tmp_path,monkeypatch):
    import threading
    rig=CancelRig(tmp_path,monkeypatch)
    release=threading.Event();entered={n:threading.Event() for n in 'ABC'};handles=[]
    def cancel(*,order_ids):
        rig.sdk.cancels.append(order_ids);entered[order_ids[0]].set()
        assert release.wait(8),'external cancel capacity watchdog'
        return dict(canceled=order_ids,not_canceled={})
    try:
        for name in 'ABC':rig.market(name);rig.add_order(name,'8',order_id=name)
        rig.live.run_once();rig.sdk.cancel_orders=cancel
        for name in 'ABC':
            key=next(o.key for o in rig.live.state.snapshot().active_orders if o.key[3]==name)
            handles.append(rig.live.cancel_independent(key));assert entered[name].wait(3)
        assert rig.live.status()['native_inflight']==3
        assert not any(h.done() for h in handles)
        release.set()
        assert all(finished(h).status.value=='accepted' for h in handles)
    finally:
        release.set()
        for handle in handles:handle._thread.join(3)
        rig.close()

    # Two real placement reservations remain occupied while external SDK signing
    # is held. A third placement cannot enter signing; CANCEL remains independent.
    from tests.test_lp_d2_manual_bridge import manual_runtime,entry
    from tests.test_lp_cancel_lock_regressions import PermissionPort
    from tests.test_lp_order_registration_contract import _open_order
    execution,live,sdk,public,clock=manual_runtime(tmp_path/'placements',monkeypatch)
    entered=[threading.Event(),threading.Event()];release=threading.Event()
    signing=[];results=[];errors=[];threads=[];lock=threading.Lock()
    def sign(**kwargs):
        with lock:index=len(signing);signing.append(kwargs)
        assert index<2,'third placement must not enter the native SDK'
        entered[index].set();assert release.wait(8),'placement SDK signing watchdog'
        return dict(kwargs,order_type='GTD')
    sdk.create_limit_order=sign
    def submit(index):
        try:
            results.append(live.manual_entry(execution._lp,entry(clock),f'capacity-{index}',
                PermissionPort().acquire('MANUAL_BUY')))
        except BaseException as exc:errors.append(exc)
    try:
        for index in range(2):
            thread=threading.Thread(target=submit,args=(index,));threads.append(thread);thread.start()
            assert entered[index].wait(3)
        assert live.status()['native_inflight']==2
        third=live.manual_entry(execution._lp,entry(clock),'capacity-third',PermissionPort().acquire('MANUAL_BUY'))
        assert third['submit_status']=='not_sent',third
        assert len(signing)==2 and sdk.posts==[]
        sdk.orders=(_open_order('cancel-with-placements','BUY',price='.30',original='20'),)
        live.run_once()
        # Fresh current facts, after resuming, authorize the exact cleanup cancel.
        live.request_running(True)
        from tests.test_lp_d2_integration import settle_control
        settle_control(live);live.run_once()
        assert sdk.cancels==[('cancel-with-placements',)]
        assert live.status()['native_inflight']==2
        assert len(signing)==2 and sdk.posts==[]
    finally:
        release.set()
        for thread in threads:thread.join(3)
        assert not any(thread.is_alive() for thread in threads) and not errors
        assert live.close(5);live.trading.close()


@pytest.mark.parametrize('case',['funded','insufficient_balance','unknown_buy'])
def test_current_account_authority_ignores_only_pending_cancels(tmp_path,monkeypatch,case):
    from datetime import timedelta
    from threading import Event,Thread
    from types import SimpleNamespace
    from tests.test_lp_candidate_current_authority import current_runtime,produce,expected_candidate
    from tests.test_lp_order_registration_contract import _open_order
    cancel_entered,cancel_release=Event(),Event()
    buy_entered,buy_release=Event(),Event();worker=None;errors=[]
    with current_runtime(tmp_path,monkeypatch) as (_,producer,live,execution,sdk,clock):
        def cancel(*,order_ids):
            sdk.cancels.append(order_ids);cancel_entered.set()
            assert cancel_release.wait(8),'external pending cancel watchdog'
            return dict(canceled=order_ids,not_canceled={})
        def unknown_post(signed):
            sdk.posts.append(signed);buy_entered.set()
            assert buy_release.wait(8),'external unknown BUY watchdog'
            raise TimeoutError('unknown BUY')
        try:
            sdk.orders=(_open_order('old-A','BUY',price='.40',original='20'),)
            live.run_once();sdk.cancel_orders=cancel
            handle=live.cancel_independent(live.state.snapshot().active_orders[0].key)
            assert cancel_entered.wait(3)
            sdk.orders=();clock[0]+=timedelta(seconds=1)
            if case=='insufficient_balance':
                sdk.get_balance_allowance=lambda **kw:SimpleNamespace(balance='0',allowances={sdk.environment.standard_exchange:'0'})
            live.run_once()
            produce(producer)
            if case=='insufficient_balance':
                execution.lp_auto_scheduled_check()
                assert sdk.posts==[]
            else:
                expected_candidate(producer)
                if case=='unknown_buy':
                    sdk.post_order=unknown_post
                    def schedule():
                        try:execution.lp_auto_scheduled_check()
                        except BaseException as exc:errors.append(exc)
                    worker=Thread(target=schedule);worker.start();assert buy_entered.wait(3)
                    assert live.candidate_current_reservations()[0]['amount'] is None
                    produce(producer)
                    assert len(sdk.posts)==1
                    assert not handle.done()
                    buy_release.set();worker.join(3);assert not worker.is_alive() and not errors
                else:
                    execution.lp_auto_scheduled_check()
                    assert len(sdk.posts)==1
                    assert sdk.posts[0]['side']=='BUY'
            assert not handle.done()
            assert live.status()['native_inflight']>=1
        finally:
            cancel_release.set();buy_release.set()
            if worker is not None:worker.join(3)
            if 'handle' in locals():finished(handle)


def test_close_tracks_every_overlapping_cancel_until_actual_exit(tmp_path,monkeypatch):
    from threading import Event,Lock
    from datetime import timedelta
    rig=CancelRig(tmp_path,monkeypatch)
    entered=[Event() for _ in range(3)];release=[Event() for _ in range(3)];lock=Lock();handles=[]
    def cancel(*,order_ids):
        with lock:index=len(rig.sdk.cancels);rig.sdk.cancels.append(order_ids)
        entered[index].set();assert release[index].wait(8),'actual exit watchdog'
        return dict(canceled=order_ids,not_canceled={})
    try:
        for name in 'AB':rig.market(name);rig.add_order(name,'8',order_id=name)
        rig.live.run_once();rig.sdk.cancel_orders=cancel
        key=next(o.key for o in rig.live.state.snapshot().active_orders if o.key[3]=='A')
        for index in range(3):
            if index:
                rig.date[0]+=timedelta(seconds=1);rig.seconds[0]+=1;rig.live.run_once()
            handle=(rig.live.cancel_independent(key) if index<2 else group_request(rig,'AB').rotation_outcomes[0].handle)
            handles.append(handle);assert entered[index].wait(3)
        assert rig.live.status()['native_inflight']==3
        assert rig.live.close(timeout=0) is False
        for index,remaining in ((1,2),(2,1),(0,0)):
            release[index].set();finished(handles[index])
            assert rig.live.status()['native_inflight']==remaining
            if remaining:assert rig.live.close(timeout=0) is False
        assert rig.live.close(timeout=3) is True
    finally:
        for event in release:event.set()
        for handle in handles:handle._thread.join(3)
        rig.close()


def test_pending_cancels_wait_for_new_facts_or_completion_without_busy_loop(tmp_path,monkeypatch):
    from threading import Event
    from tests.test_lp_observed_pools import AsyncPoolRig
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    entered,release=Event(),Event();handle=None
    def cancel(*,order_ids):
        rig.sdk.cancels.append(order_ids);entered.set()
        assert release.wait(8),'pending scheduler watchdog'
        raise TimeoutError('offline outcome unknown')
    try:
        rig.market('A');rig.add_order('A','8',order_id='A')
        rig.tick();rig.converge_current(lambda:len(rig.live.state.snapshot().active_orders)==1
            and rig.live.read_runtime.status()['ready_count']==0
            and not any(s['active'] for s in rig.live.read_runtime.status()['scopes']))
        rig.sdk.cancel_orders=cancel
        handle=rig.live.cancel_independent(rig.live.state.snapshot().active_orders[0].key)
        assert entered.wait(3)
        # Consume any prior completion; only the pending native call remains.
        rig.live.run_protection_once()
        rig.pump(lambda:rig.live.status()['plan_wait'] is not None)
        status=rig.live.status()
        assert status['plan_wait'] is not None,status
        calls=tuple(rig.requests);before=rig.live.rounds.snapshot().round_number
        for _ in range(20):rig.scheduler.run_due()
        assert rig.live.rounds.snapshot().round_number==before
        assert tuple(rig.requests)==calls and rig.sdk.cancels==[('A',)]
        release.set();finished(handle)
        assert rig.live.status()['plan_wait'] is None
        rig.scheduler.run_due()
        rig.tick();rig.converge_current(lambda:rig.live.state.snapshot().observation.order_view.orders_source.checked_at==rig.utc())
        assert len(rig.requests)>len(calls)
    finally:
        release.set()
        if handle is not None:handle._thread.join(3)
        rig.close()
