"""#482 captured pool evidence: real SQLite waits must not hold current state."""
import socket
import sqlite3
from datetime import timedelta
from decimal import Decimal
from threading import Event,Thread,current_thread

import pytest

from tests.test_lp_income_pool_ab_482 import _timed_rig,_protect_at


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('#482 SQL tests must not connect to a network')
    monkeypatch.setattr(socket.socket,'connect',forbidden)


@pytest.mark.parametrize('query,trigger',[('roles','partial'),('roles','measured')])
@pytest.mark.parametrize('change',['unchanged','close','changed-current'])
def test_pool_cancel_sql_wait_keeps_state_responsive_and_rechecks_current_facts(tmp_path,monkeypatch,query,trigger,change):
    rig=_timed_rig(tmp_path,monkeypatch)
    from tests.test_lp_observed_pools import _pool_test_clock
    dates=_pool_test_clock(monkeypatch)
    rig.date[0]+=timedelta(seconds=1);rig.now=rig.date[0];dates[0]=rig.date[0]
    entered,release=Event(),Event()
    dispatched,probed,changed=Event(),Event(),Event()
    handles=[];errors=[];observed=[];changes=[];ownership=[]
    dispatch=probe=mutation=None
    try:
        if trigger=='partial':
            rig.sdk.orders[0].update(size_matched=Decimal('5'),status='PARTIALLY_FILLED')
            rig.live.run_once()
            reason='buy_partial_fill'
        else:
            _protect_at(rig,0)
            rig.seconds[0]=600
            rig.live.run_once()
            reason='measured_yield_unknown_timeout'
        original=rig.live.state.snapshot()
        real_connect=sqlite3.connect
        class HeldPoolQuery(sqlite3.Connection):
            def execute(self,sql,*args,**kwargs):
                cursor=super().execute(sql,*args,**kwargs)
                selected=(sql=='SELECT payload FROM lp_auto_pool WHERE singleton=1' if query=='roles'
                    else 'FROM lp_market_observations' in sql)
                if current_thread().name=='lp-d2-independent-cancel' and selected and not entered.is_set():
                    ownership.append(rig.live.state._lock._is_owned())
                    entered.set()
                    assert release.wait(8),'actual native pool evidence SELECT watchdog'
                return cursor
        monkeypatch.setattr(sqlite3,'connect',lambda *a,**kw:real_connect(*a,**(kw|dict(factory=HeldPoolQuery))))
        def run():
            try:handles.extend(rig.live.run_protection_once())
            except BaseException as exc:errors.append(exc)
            finally:dispatched.set()
        dispatch=Thread(target=run,name='ab02-pool-protection');dispatch.start()
        assert entered.wait(3),'native pool evidence SELECT not reached'
        def inspect_state():
            try:observed.extend((rig.live.state.snapshot(),rig.live.status(),rig.live.diagnostics()))
            except BaseException as exc:errors.append(exc)
            finally:probed.set()
        probe=Thread(target=inspect_state,name='ab02-independent-status');probe.start()
        assert probed.wait(1),'slow SQL held CurrentLPState: snapshot/status/diagnostics blocked'
        assert errors==[] and len(observed)==3
        assert ownership==[False],'native role/sample SQL held CurrentLPState'
        assert rig.sdk.cancels==[] and rig.sdk.posts==[]
        def change_current():
            try:
                if change=='close':
                    changes.append(rig.live.close(timeout=.05))
                else:
                    # Real new account facts invalidate the original remaining quantity/source.
                    rig.sdk.orders[0]['size_matched']=Decimal('6' if trigger=='partial' else '1')
                    rig.seconds[0]+=5
                    rig.date[0]+=timedelta(seconds=5);rig.now=rig.date[0];dates[0]=rig.date[0]
                    changes.append(rig.live.run_once())
            except BaseException as exc:errors.append(exc)
            finally:changed.set()
        if change!='unchanged':
            mutation=Thread(target=change_current,name='ab02-independent-current');mutation.start()
            assert changed.wait(1),'slow pool SELECT blocked independent close/current account tick'
            assert errors==[]
            if change=='close':assert changes==[False] # native SQL remains owned, so not drained
            else:
                order,=rig.live.state.snapshot().active_orders
                assert order.remaining.value==Decimal('14' if trigger=='partial' else '19')
                assert rig.live.state.snapshot().observation.observation_id!=original.observation.observation_id
        release.set()
        dispatch.join(3);probe.join(3)
        if mutation:mutation.join(3)
        assert not dispatch.is_alive() and not probe.is_alive() and errors==[]
        assert len(handles)==1
        handle,=handles
        outcome=handle.result(3);handle._thread.join(3)
        assert not handle._thread.is_alive()
        assert outcome.status.value==('accepted' if change=='unchanged' else 'not_sent')
        assert rig.sdk.cancels==([('old-A',)] if change=='unchanged' else [])
        if change=='unchanged':
            targets=[t for p in rig.live.state.snapshot().progress.values() for t in p.cancel_targets]
            target,=targets
            assert target.order_key[3]=='old-A' and target.reason==reason
            assert target.request_id==handle.identity.request_id
            assert target.native_state=='ended' and target.local_outcome=='accepted'
            assert target.venue_terminal.matched.state=='unknown'
            for _ in range(3):assert rig.live.run_protection_once()==()
            assert rig.sdk.cancels==[('old-A',)]
        assert rig.sdk.posts==[] and rig.sdk.market_orders==[]
        if change!='close':
            from tests.test_lp_cancel_lock_regressions import drained
            drained(rig.live.writer)
            record,=rig.store.lp_actions('d2:'+handle.identity.request_id)
            assert record['order_id']=='old-A' and record['receipt']['request_id']==handle.identity.request_id
            assert record['receipt']['outcome']==outcome.status.value
    finally:
        release.set()
        for thread in (dispatch,probe,mutation):
            if thread:thread.join(3)
        for handle in handles:
            handle.result(3);handle._thread.join(3)
        rig.close()
