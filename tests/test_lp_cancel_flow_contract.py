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
    from threading import Event
    from open_trader.lp_business_writer import ProgressRecord, ReceiptRecord
    from tests.test_lp_d2_integration import _d2_protection_runtime
    from tests.test_lp_order_registration_contract import WALLET,CONDITION_ID,TOKEN_ID,_open_order
    live,sdk,public,clock=_d2_protection_runtime(tmp_path,monkeypatch,quantity='20')
    key=(WALLET,CONDITION_ID,TOKEN_ID,'entry-A')
    delayed_entered, delayed_release = Event(), Event()
    try:
        live.run_once();drained(live.writer)
        buy=next(r for r in action_rows(live.store) if r['kind']=='BUY')
        def unknown(*,order_ids):sdk.cancels.append(order_ids);raise TimeoutError('offline')
        sdk.cancel_orders=unknown
        a=finished(live.cancel_independent(key))
        clock[0]=1;live.run_once()
        b=finished(live.cancel_independent(key))
        assert a.identity.request_id!=b.identity.request_id
        clock[0]=2;live.run_once()
        def delayed_old_ack(*, order_ids):
            sdk.cancels.append(order_ids)
            delayed_entered.set()
            assert delayed_release.wait(5), 'delayed original-ID receipt watchdog'
            return dict(canceled=order_ids, not_canceled={})
        sdk.cancel_orders=delayed_old_ack
        delayed=live.cancel_independent(key)
        assert delayed_entered.wait(2) and not delayed.done()
        clock[0]=4
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
        clock[0]=5
        sdk.orders=(_open_order('replacement','BUY',price='.30',original='20'),)
        live.run_once()
        replacement=next(o for o in live.state.snapshot().active_orders if o.key[3]=='replacement')
        before_replacement_audit={r['request_id']:r for r in action_rows(live.store) if r['order_id']=='replacement'}
        # Deliver a real delayed SDK ACK only AFTER the replacement exists.
        delayed_release.set()
        assert finished(delayed).status.value=='accepted'
        # Also publish late original-parent receipt/progress through the writer.
        live.writer.offer(ProgressRecord(WALLET,live.process_epoch,a.identity.request_id,
            original['session_id'],CONDITION_ID,TOKEN_ID,'entry-A',Decimal(20),Decimal(0),Decimal('.30'),'LIVE',late_at))
        live.writer.offer(ReceiptRecord(WALLET,live.process_epoch,a.identity.request_id,
            original['session_id'],CONDITION_ID,TOKEN_ID,'entry-A','unknown',live._utc_now()))
        drained(live.writer)
        current=live.state.snapshot()
        assert next(o for o in current.active_orders if o.key[3]=='replacement')==replacement
        old=next(o for o in current.orders if o.key==key)
        assert old.terminal_proof.matched.value==3 and old.remaining.value==17
        refreshed={r['request_id']:r for r in action_rows(live.store)}
        assert {k:r for k,r in refreshed.items() if r['order_id']=='replacement'}==before_replacement_audit
        cancels=[r for r in refreshed.values() if r['kind']=='CANCEL']
        assert len(cancels)==3 and all(r['order_id']=='entry-A' for r in cancels)
        assert refreshed[a.identity.request_id]['progress']==original['progress']
        for row in cancels:
            assert row['receipt']['order_id']=='entry-A'
            assert row['progress']['order_id']=='entry-A'
            assert (row['progress']['size_matched'],row['progress']['remaining'],row['progress']['reserved_usd'])==('3','17','0')
        assert refreshed[a.identity.request_id]['receipt']['outcome']=='unknown'
        assert refreshed[b.identity.request_id]['receipt']==latest[b.identity.request_id]['receipt']
        assert refreshed[delayed.identity.request_id]['receipt']['outcome']=='accepted'
        assert refreshed[buy['request_id']]==latest[buy['request_id']]
    finally:
        delayed_release.set()
        assert live.close(5);live.trading.close()


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


@pytest.mark.parametrize('kind', ['single_rejected', 'single_unknown', 'group_mixed'])
def test_completed_cancel_history_has_bounded_runtime_and_live_writes(tmp_path, monkeypatch, kind):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta
    from threading import Event
    rig = CancelRig(tmp_path, monkeypatch)
    rig.date[0] = max(rig.date[0], datetime.now(UTC)) + timedelta(seconds=1)
    from open_trader import polymarket_trading, polymarket_lp_market_reader
    class ClockMeta(type):
        def __instancecheck__(cls, value): return isinstance(value, datetime)
    class SourceDateTime(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return rig.date[0] if tz is not None else rig.date[0].replace(tzinfo=None)
    monkeypatch.setattr(polymarket_trading, 'datetime', SourceDateTime)
    monkeypatch.setattr(polymarket_lp_market_reader, 'datetime', SourceDateTime)
    names = 'AB' if kind == 'group_mixed' else 'A'
    entered, release = Event(), Event()
    held = []
    calls = [0]
    def cancel(*, order_ids):
        calls[0] += 1
        rig.sdk.cancels.append(tuple(order_ids))
        if calls[0] == 1:
            entered.set()
            assert release.wait(8), 'actual overlapping owner completion watchdog'
        if kind == 'single_unknown':
            raise TimeoutError('offline unknown')
        return dict(canceled=(), not_canceled={'A': 'safe fixture rejection'})
    def fresh():
        rig.date[0] += timedelta(seconds=1)
        rig.seconds[0] += 1
        rig.now = rig.date[0]
        rig.live.run_once()
    def dispatch():
        if kind == 'group_mixed':
            return [o.handle for o in group_request(rig, names).rotation_outcomes]
        key = next(o.key for o in rig.live.state.snapshot().active_orders if o.key[3] == 'A')
        return [rig.live.cancel_independent(key)]
    try:
        for name in names:
            rig.market(name)
            rig.add_order(name, '8', order_id=name)
        fresh()
        rig.sdk.cancel_orders = cancel
        held = dispatch()
        assert entered.wait(2)
        assert rig.live.status()['native_inflight'] == 1
        work, cardinalities = [], []
        for attempt in range(1, 10):
            fresh()
            for handle in dispatch():
                finished(handle)
            drained(rig.live.writer)
            if attempt in (3, 9):
                before = rig.live.writer.status()['saved_entries']
                fresh()  # equal normal LIVE observation, no new dispatch
                after = drained(rig.live.writer)['saved_entries']
                work.append(after - before)
                cardinalities.append((len(rig.live._cancel_records), len(rig.live._cancel_progress_sources)))
        assert all(delta <= len(names) for delta in work), work
        assert cardinalities[1] == cardinalities[0], cardinalities
        assert all(max(counts) <= 2 * len(names) for counts in cardinalities), cardinalities
        assert rig.live.status()['native_inflight'] == 1 and all(not h.done() for h in held)
        records = [r for r in action_rows(rig.store) if r['kind'] == 'CANCEL']
        assert len(records) == 10 * len(names)
        receipts = {r['request_id']: r.get('receipt') for r in records}
        # Seed unrelated durable audit parents through the real writer. They
        # must not receive a terminal broadcast even when some terms overlap.
        reference = next(r for r in rig.live._cancel_records.values() if r.order_id == 'A')
        bad = [dict(account_id='other-account'), dict(process_epoch='other-epoch'),
            dict(order_id='replacement'), dict(condition_id='other-condition'), dict(token_id='other-token'),
            dict(kind='BUY'), dict(quantity=Decimal(21)), dict(price=Decimal('.41')),
            dict(order_type='GTD'), dict(expiration=1), dict(venue='other-venue'), dict(), dict(), dict()]
        for index, changes in enumerate(bad):
            rig.live.writer.offer(replace(reference, request_id='unrelated-'+str(index),
                session_id='unrelated-session-'+str(index), **changes))
        drained(rig.live.writer)
        # Corrupt only the isolated final parent fixture to test the original
        # session root guard; the writer/store must never fan out through it.
        import json
        with rig.store._transaction() as connection:
            row = connection.execute('SELECT payload FROM lp_sessions WHERE session_id=?',
                ('unrelated-session-11',)).fetchone()
            parent = json.loads(row['payload']); parent['process_epoch'] = 'wrong-root-epoch'
            connection.execute('UPDATE lp_sessions SET payload=? WHERE session_id=?',
                (json.dumps(parent), 'unrelated-session-11'))
            for index, field, value in ((12, 'session_id', 'wrong-session'), (13, 'request_id', 'wrong-request')):
                row = connection.execute('SELECT payload FROM lp_actions WHERE action_key=?',
                    ('unrelated-'+str(index),)).fetchone()
                audit = json.loads(row['payload']); audit[field] = value
                connection.execute('UPDATE lp_actions SET payload=? WHERE action_key=?',
                    (json.dumps(audit), 'unrelated-'+str(index)))
        unrelated = {r['session_id']: r for r in action_rows(rig.store) if r['request_id'].startswith('unrelated-') or r['request_id']=='wrong-request'}
        rig.release(*names)
        for name in names:
            rig.history[name] = {**rig.history[name], 'size_matched': Decimal('3' if name == 'A' else '5')}
        fresh()
        rig.live.run_protection_once()
        drained(rig.live.writer)
        rows = {r['request_id']: r for r in action_rows(rig.store)}
        for original in records:
            row = rows[original['request_id']]
            assert row['order_id'] == original['order_id']
            assert row.get('receipt') == receipts[row['request_id']]
            progress = row['progress']
            assert progress['request_id'] == row['request_id'] and progress['session_id'] == row['session_id']
            assert (progress['size_matched'], progress['remaining'], progress['reserved_usd'], progress['status']) == (
                ('3', '17', '0', 'CANCELED') if row['order_id'] == 'A' else ('5', '15', '0', 'CANCELED'))
        assert {r['session_id']: r for r in action_rows(rig.store) if r['session_id'] in unrelated} == unrelated
        assert live_status_ok(drained(rig.live.writer))
        assert rig.live.close(timeout=0) is False
        assert rig.live.status()['native_inflight'] == 1
        release.set()
        for handle in held:
            finished(handle)
        drained(rig.live.writer)
        assert rig.live.close(timeout=3)
        assert rig.live.status()['native_inflight'] == 0
    finally:
        release.set()
        rig.close()


def live_status_ok(status):
    return all(status[key] == 0 for key in ('failed_entries', 'unknown_outcome_entries', 'dropped_parent',
        'dropped_overflow', 'dropped_inflight_capacity', 'dropped_oversize', 'dropped_shutdown', 'dropped_closed'))


@pytest.mark.parametrize('kind', ['single_protection', 'group_rotation'])
def test_gtd_cancel_from_real_parsed_facts_uses_epoch_expiration(tmp_path, monkeypatch, kind):
    from datetime import UTC, datetime, timedelta
    from dataclasses import replace
    from threading import Event
    import time, httpx
    from tests.test_lp_observed_pools import AsyncPoolRig
    from tests.test_lp_async_live_contract import sdk_wire
    from tests.test_lp_order_registration_contract import _open_order
    actual_http = AsyncPoolRig.http
    epoch = 1924992000  # independently fixed: 2031-01-01T00:00:00Z
    expiry = datetime(2031, 1, 1, tzinfo=UTC)
    assert int(expiry.timestamp()) == epoch
    async def sdk_http(self, req):
        source = req.headers['x-lp-source']
        if source in ('open_orders', 'order'):
            self.requests.append((source, req.headers['x-lp-order'], self.seconds))
            objects = self.sdk.orders if source == 'open_orders' else [self.history[req.url.path.rsplit('/', 1)[-1]]]
            rows = []
            for obj in objects:
                row = sdk_wire(obj)
                expiration = obj.expires_at
                row['expiration'] = str(int(expiration.timestamp())) if expiration is not None else '0'
                row['created_at'] = int(self.utc().timestamp())
                rows.append(row)
            return httpx.Response(200, json=dict(data=rows, next_cursor='LTE=') if source=='open_orders' else rows[0])
        return await actual_http(self, req)
    monkeypatch.setattr(AsyncPoolRig, 'http', sdk_http)
    rig = AsyncPoolRig(tmp_path, monkeypatch)
    names = 'A' if kind == 'single_protection' else 'AB'
    def observe(predicate):
        deadline = time.monotonic()+4
        while not predicate():
            rig.live.run_once()
            assert time.monotonic()<deadline, rig.live.diagnostics()
            Event().wait(.001)
    try:
        for name in names:
            rig.market(name)
        rig.sdk.orders = tuple(_open_order(name, 'BUY', price='.4', original='20', order_type='GTD',
            expiration=epoch, token_id='t'+name).model_copy(update={'condition_id':rig.cid(name)}) for name in names)
        assert all(o.expires_at == expiry for o in rig.sdk.orders)
        rig.history = {o.id:o for o in rig.sdk.orders}
        rig.tick()
        observe(lambda: len(rig.live.state.snapshot().active_orders)==len(names)
            and all(o.key in rig.live.state.snapshot().yields for o in rig.live.state.snapshot().active_orders))
        snapshot = rig.live.state.snapshot()
        assert all(isinstance(o.expiration.value, datetime) and o.expiration.value==expiry for o in snapshot.active_orders)
        observation = snapshot.observation
        decision = RotationDecision(rig.live.account_id, observation.cycle_generation,
            observation.observation_id, snapshot.active_orders)
        wrong = replace(snapshot.active_orders[0], key=(*snapshot.active_orders[0].key[:3], 'wrong-ID'))
        denied=rig.live.dispatch_rotation(RunResult(None, rotation=replace(decision, victims=(wrong,)))).rotation_outcomes
        assert denied and all(o.status=='not_sent' and o.handle is None for o in denied)
        assert rig.sdk.cancels == []
        assert rig.live.run_protection_once()==()
        if kind=='single_protection':
            actual_book = rig.public.get_order_book
            def low_book(**kwargs):
                book=actual_book(**kwargs)
                return book.model_copy(update={'bids':(book.bids[0].model_copy(update={'size':Decimal('40')}),)})
            rig.public.get_order_book=low_book
            rig.tick(10)
            observe(lambda:rig.live.state.snapshot().observation.accepted_at==rig.utc()
                and (source:=rig.live.state.snapshot().observation.source_frame.sources.get('market_book:'+rig.cid('A')+':tA')) is not None
                and source.checked_at==rig.utc())
        before = list(rig.requests)
        def cancel(*,order_ids):
            rig.sdk.cancels.append(tuple(order_ids))
            if kind=='single_protection':raise TimeoutError('offline unknown native result')
            return dict(canceled=['A'], not_canceled={})
        rig.sdk.cancel_orders=cancel
        if kind=='single_protection':
            handles=list(rig.live.run_protection_once())
        else:
            handles=[o.handle for o in rig.live.dispatch_rotation(RunResult(None,rotation=decision)).rotation_outcomes]
        assert len(handles)==len(names)
        results={h.identity.target.existing_order_id:finished(h).status.value for h in handles}
        assert results==({'A':'unknown_error'} if kind=='single_protection' else {'A':'accepted','B':'unknown_error'})
        assert rig.requests==before  # entering cancel adds no GET
        drained(rig.live.writer)
        rows=[r for r in action_rows(rig.store) if r['kind']=='CANCEL']
        assert len(rows)==len(names) and all(type(r['expiration']) is int and r['expiration']==epoch for r in rows)
        assert set(r['order_id'] for r in rows)==set(names)
        receipts={r['request_id']:r['receipt'] for r in rows}
        assert len(rig.sdk.cancels)==1
        assert rig.live.run_protection_once()==()
        from tests.test_lp_d2_integration import settle_control
        rig.live.request_running(False)
        settle_control(rig.live)
        if kind=='single_protection':
            rig.sdk.orders=tuple(o.model_copy(update={'expires_at':expiry+timedelta(seconds=60)}) for o in rig.sdk.orders)
            rig.tick(5)
            observe(lambda:any(o.key[3]=='A' and o.expiration.value==expiry+timedelta(seconds=60)
                for o in rig.live.state.snapshot().active_orders))
            assert rig.live.run_protection_once()==()
            drained(rig.live.writer)
            assert len(rig.sdk.cancels)==1
            assert len([r for r in action_rows(rig.store) if r['kind']=='CANCEL'])==1
        rig.sdk.orders=()
        for name in names:
            rig.history[name]=rig.history[name].model_copy(update={'status':'CANCELED', 'size_matched':Decimal('3')})
        rig.tick()
        rig.converge_current(lambda:all(any(r['request_id']==old['request_id'] and r.get('progress',{}).get('status')=='CANCELED'
            for r in action_rows(rig.store)) for old in rows))
        drained(rig.live.writer)
        final={r['request_id']:r for r in action_rows(rig.store)}
        for old in rows:
            row=final[old['request_id']]
            assert row['receipt']==receipts[row['request_id']]
            assert (row['progress']['size_matched'],row['progress']['remaining'],row['progress']['reserved_usd'])==('3','17','0')
        assert live_status_ok(drained(rig.live.writer))
    finally:rig.close()


@pytest.mark.parametrize('kind', ['register', 'not_sent_cleanup'])
def test_native_cancel_registration_cannot_interrupt_protection(tmp_path, monkeypatch, kind):
    import inspect, threading, sys
    from open_trader.lp_business_writer import RequestRecord
    owner=threading.current_thread()
    native_gate, native_entered, observed = threading.Event(), threading.Event(), threading.Event()
    arm_clock=[False]
    pending=None
    class ClockValues(list):
        def __getitem__(self,index):
            if arm_clock[0] and threading.current_thread().name=='lp-d2-independent-cancel':
                arm_clock[0]=False
                native_entered.set()
                assert native_gate.wait(4), 'native registration clock gate watchdog'
            return super().__getitem__(index)
    deny=[False]
    class ObservedLive(LPD2LiveExecution):
        def bind_mutation_admission(self,owner,acquire):
            def observed_acquire(kind):
                actual=acquire(kind)
                if actual is None:return None
                class Lease:
                    def allowed(self,action=None):
                        if deny[0] and threading.current_thread().name=='lp-d2-independent-cancel':
                            native_entered.set()
                            assert native_gate.wait(4), 'actual final admission cleanup gate watchdog'
                            return False
                        return actual.allowed(action)
                    def release(self):actual.release()
                return Lease()
            return super().bind_mutation_admission(owner,observed_acquire)
    monkeypatch.setattr(sys.modules[__name__], 'LPD2LiveExecution', ObservedLive)
    rig=CancelRig(tmp_path,monkeypatch)
    rig.date=ClockValues(rig.date)
    read_attribute=RequestRecord.__getattribute__
    def observation_gate(record,name):
        frame=inspect.currentframe().f_back
        if (name=='account_id' and threading.current_thread() is owner and not observed.is_set()
                and (frame.f_code.co_name=='_refresh_protection_evidence' or
                    frame.f_code.co_name=='<setcomp>' and frame.f_back is not None
                    and frame.f_back.f_code.co_name=='_refresh_protection_evidence')):
            # This diagnostic pauses a real immutable value read, never changes
            # a dictionary or a fact, and never creates a second business owner.
            observed.set()
            native_gate.set()
            finished(pending)  # actual native registration/removal and callback finish
        return read_attribute(record,name)
    try:
        for name in 'AB':
            rig.market(name);rig.add_order(name,'8',order_id=name)
        rig.live.run_once()
        def unknown(*,order_ids):
            rig.sdk.cancels.append(tuple(order_ids));raise TimeoutError('offline unknown')
        rig.sdk.cancel_orders=unknown
        keys={o.key[3]:o.key for o in rig.live.state.snapshot().active_orders}
        first=finished(rig.live.cancel_independent(keys['A']))
        if kind=='register':arm_clock[0]=True
        else:deny[0]=True
        pending=rig.live.cancel_independent(keys['B'])
        assert native_entered.wait(2)
        assert not pending.done()
        monkeypatch.setattr(RequestRecord,'__getattribute__',observation_gate)
        rig.live.run_protection_once()
        assert observed.is_set() and pending.done()
        result=pending.result(0)
        assert result.status.value==('unknown_error' if kind=='register' else 'not_sent')
        assert rig.live.status()['native_inflight']==0
        if kind=='not_sent_cleanup':
            assert rig.sdk.cancels==[('A',)]
            deny[0]=False
            retry=rig.live.cancel_independent(keys['B'])
            assert retry.identity.request_id!=pending.identity.request_id
            assert finished(retry).status.value=='unknown_error'
        assert rig.sdk.cancels==[('A',),('B',)]
        assert rig.live.run_protection_once()==()
        assert rig.sdk.cancels==[('A',),('B',)]  # consumed source cannot replay
        # Later normal facts still drive both exact protection targets.
        actual_book=rig.public.get_order_book
        def low_book(**kwargs):
            book=actual_book(**kwargs)
            return book.model_copy(update={'bids':(book.bids[0].model_copy(update={'size':Decimal('40')}),)})
        rig.public.get_order_book=low_book
        rig.live.run_once()
        handles=rig.live.run_protection_once()
        assert {h.identity.target.existing_order_id for h in handles}=={'A','B'}
        for handle in handles:assert finished(handle).status.value=='unknown_error'
        drained(rig.live.writer)
        rows=[r for r in action_rows(rig.store) if r['kind']=='CANCEL']
        assert len({r['request_id'] for r in rows})==len(rows)==(5 if kind=='not_sent_cleanup' else 4)
        assert all(r['receipt']['request_id']==r['request_id'] and r['receipt']['order_id']==r['order_id'] for r in rows)
        assert next(r for r in rows if r['request_id']==first.identity.request_id)['receipt']['outcome']=='unknown'
        assert live_status_ok(drained(rig.live.writer))
    finally:
        native_gate.set()
        rig.close()
