"""Compare the same offline current assembly across interpreter boundaries."""
import json
import threading

import pytest

from tests.lp_d2_service_harness import ServiceChild, InProcessService
from tests.test_lp_account_reservation_reconciliation import runtime
from tests.test_lp_auto_restart_preparation import dead_preparation,preparation_snapshots


def test_inprocess_restores_globals_and_drains_after_assertion(tmp_path):
    import os,signal,sqlite3,sys
    from websockets.asyncio import client
    before=(os.getcwd(),list(sys.path),dict(os.environ),sqlite3.connect,signal.signal,
        threading.gettrace(),threading.getprofile(),client.connect_http_proxy)
    modules={name:module for name,module in sys.modules.items()
        if name=='open_trader' or name.startswith('open_trader.')}
    with pytest.raises(AssertionError,match='deliberate test assertion'):
        with InProcessService(tmp_path,scenario='nr-stale') as child:
            child.release('metadata');child.start(expected_running=None)
            child.wait('nr-cycle-known-completed')
            raise AssertionError('deliberate test assertion')
    child.assert_owner_available()
    after=(os.getcwd(),list(sys.path),dict(os.environ),sqlite3.connect,signal.signal,
        threading.gettrace(),threading.getprofile(),client.connect_http_proxy)
    globals_restored = after == before
    assert globals_restored, "in-process service did not restore global state"
    assert {name:module for name,module in sys.modules.items()
        if name=='open_trader' or name.startswith('open_trader.')}==modules


def canonical_sources(value, *, full_state=False):
    # Fresh reads have independent UUIDs; preserve their equality relationships.
    identifiers={}
    def canonical(item, path=()):
        if isinstance(item,list):return [canonical(value,path) for value in item]
        if isinstance(item,dict):
            result={}
            for key,value in item.items():
                diagnostic=full_state and path[:2]==('state','diagnostics')
                if full_state and path==('state','round') and key=='applied_transition_id':
                    epoch,transition=json.loads(value)
                    assert isinstance(epoch,str) and epoch
                    value=[identifiers.setdefault(epoch,len(identifiers)),transition]
                elif (diagnostic and key in ('generation','capture_id','attempt_id','order_source_id',
                        'reason_observation_id') or diagnostic and path[-1]=='yield_rounds' and key=='instance_id'
                        or full_state and path==('state','round') and key=='cycle_generation') and value is not None:
                    assert isinstance(value,str) and value
                    if ':read:' in value:
                        epoch,read=value.rsplit(':read:',1)
                        value=[identifiers.setdefault(epoch,len(identifiers)),read]
                    else:value=identifiers.setdefault(value,len(identifiers))
                elif key in ('source_id','observation_id') and value is not None:
                    assert isinstance(value,str) and value
                    value=identifiers.setdefault(value,len(identifiers))
                result[key]=canonical(value,path+(key,))
            return result
        return item
    return canonical(value)


@pytest.mark.parametrize('scenario',['nr-stale','nr-unknown','current-known','current-incomplete-orders',
    'current-incomplete-positions','current-foreign','current-restart','current-manual','current-pools',
    'optional-catalog','optional-history','optional-share','optional-metadata','optional-hourly',
    'prep-failure','prep-partial','prep-metadata','prep-absent','candidate','trial',
    'nleg-paused','manual-preserved','stale-report','current-awaiting-receipt'])
def test_inprocess_matches_child(tmp_path,scenario,request,monkeypatch):
    if scenario in ('nleg-paused','manual-preserved','stale-report'):
        from tests import test_prediction_runtime as cases
        check={'nleg-paused':cases._assert_current_nleg_pause,
            'manual-preserved':cases._assert_current_manual_preserved,
            'stale-report':cases._assert_current_stale_report}[scenario]
        results=[check(tmp_path/service.__name__,service=service) for service in (ServiceChild,InProcessService)]
        assert canonical_sources(results[0])==canonical_sources(results[1])
        assert results[0]['requests']==dict(post=[],cancel=[])
        return
    if scenario.startswith(('optional-','prep-')) or scenario in ('candidate','trial'):
        from tests import test_prediction_runtime as cases
        def execute(service):
            root=tmp_path/service.__name__
            if scenario.startswith('optional-'):
                return cases._assert_current_progress_with_blocked_optional_source(root,scenario.removeprefix('optional-'),service=service)
            if scenario.startswith('prep-'):
                profile=scenario.removeprefix('prep-')
                expected={'failure':'waiting_retry','partial':'partial','metadata':'partial','absent':'ready'}[profile]
                return cases._assert_actual_preparation_result(root,profile,expected,service=service)
            if scenario=='candidate':return cases._assert_actual_candidate_queue(root,service=service)
            return cases._assert_actual_trial_maintenance(root,service=service)
        results=[execute(service) for service in (ServiceChild,InProcessService)]
        assert canonical_sources(results[0])==canonical_sources(results[1])
        assert results[0]['requests']==dict(post=[],cancel=[])
        return
    if scenario.startswith('current-'):
        from contextlib import contextmanager, nullcontext
        from types import SimpleNamespace
        from decimal import Decimal
        from tests.test_lp_auto_restart_preparation import _new_owner,_terminate_old,_assert_current_after_legacy,_open_order
        build=request.getfixturevalue('runtime')
        awaiting=scenario=='current-awaiting-receipt'
        preparation=(contextmanager(dead_preparation.__wrapped__)(tmp_path,build,
            SimpleNamespace(param='awaiting-receipt'),request.getfixturevalue('preparation_snapshots'))
            if awaiting else nullcontext(request.getfixturevalue('dead_preparation')))
        with preparation as (old,original,_):
            _terminate_old(old)
            owner,store,adapter,account,public=_new_owner(build,monkeypatch,tmp_path)
            if awaiting:
                public.prices[5]='.89'
                # Fresh account: 4 * 20 * .30 + 20 * .89 = 41.80; old audit stays unchanged.
                account.orders=tuple(order.model_copy(update={'price':Decimal('.30')}) for order in account.orders)
                account.orders+=(_open_order('late-post-buy','BUY',price='.89',original='20',
                    token_id=original['token_id']).model_copy(update={
                        'market':original['condition_id'],'condition_id':original['condition_id']}),)
            owner.open();owner.close()
            mode=scenario.removeprefix('current-')
            results=[_assert_current_after_legacy(tmp_path/service.__name__,store,account,public,
                mode='known' if mode in ('restart','manual','pools','awaiting-receipt') else mode,
                restart=mode=='restart',manual=mode=='manual',observed=mode=='pools',full_state=awaiting,
                service=service) for service in (ServiceChild,InProcessService)]
            assert canonical_sources(results[0],full_state=awaiting)==canonical_sources(results[1],full_state=awaiting)
            assert results[0]['requests']['cancel']==[]
            assert len(results[0]['requests']['post'])==(1 if mode=='manual' else 0)
            if awaiting:
                assert results[0]['state'].get('observed_pools') is None
            else:
                assert (results[0]['state']['observed_pools'] is None) is (mode!='pools')
            if awaiting:
                assert results[0]['requests']==dict(post=[],cancel=[])
                assert Decimal(results[0]['state']['buy_reserved_usd'])==Decimal('41.80')
                assert {row['key'][3] for row in results[0]['state']['account_buys']}=={
                    'manual-1','manual-2','manual-3','manual-4','late-post-buy'}
        return
    results=[]
    for service in (ServiceChild,InProcessService):
        with service(tmp_path/service.__name__,scenario=scenario) as child:
            child.release('metadata');child.start(expected_running=None)
            child.wait('nr-start-completed')
            kind='unknown' if scenario=='nr-unknown' else 'known'
            child.wait('nr-cycle-'+kind+'-completed')
            state=json.loads(child.marker('nr-cycle-'+kind+'.json').read_text())
            result={key:state[key] for key in ('state','production_owner','posts','cancels','orders_complete',
                'reserved_state','sell_ids','reconcile_calls','legacy_ticks','scheduler_starts')}
            assert result['state']=='RUNNING' and result['production_owner'] is True
            assert result['posts']==0 and result['reconcile_calls']==result['legacy_ticks']==0
            assert result['scheduler_starts']==1
            assert result['orders_complete'] is (kind=='known')
            assert result['cancels']==([] if kind=='unknown' else [['A']])
            assert result['reserved_state']==kind
            result['requests']=child.sdk_requests()
            assert result['requests']==dict(post=[],cancel=[] if kind=='unknown' else [dict(order_ids=['A'])])
            results.append(result)
        child.assert_owner_available()
    assert results[0]==results[1]


@pytest.mark.parametrize('phase',['before-start','template-failure','isolation-failure'])
def test_inprocess_setup_failure_preserves_caller(tmp_path,phase):
    import subprocess,sys
    # A real interpreter protects the parent diagnostic daemon if cleanup regresses.
    probe=r'''
import os,signal,sqlite3,sys,threading
from pathlib import Path
from unittest.mock import patch
from websockets.asyncio import client
from open_trader import polymarket_trading
from tests.lp_d2_service_harness import InProcessService
root=Path(sys.argv[1]);phase=sys.argv[2]
caller=polymarket_trading._lp_read_log_thread
assert caller.is_alive(), 'caller diagnostic thread must exist'
def snapshot():
    return (os.getcwd(),list(sys.path),dict(os.environ),sqlite3.connect,signal.signal,
        threading.gettrace(),threading.getprofile(),client.connect_http_proxy,
        sys.dont_write_bytecode,sys.pycache_prefix)
before=snapshot()
modules={k:v for k,v in sys.modules.items() if k=='open_trader' or k.startswith('open_trader.') or k=='tests' or k.startswith('tests.')}
expected=RuntimeError if phase=='before-start' else PermissionError
message='original '+phase+' failure'
try:
    with InProcessService(root/'failed',scenario='nr-stale') as child:
        if phase=='before-start':raise RuntimeError(message)
        if phase=='template-failure':
            with patch('shutil.copytree',side_effect=PermissionError(message)):child.start(expected_running=None)
        else:
            with patch('os.chdir',side_effect=PermissionError(message)):child.start(expected_running=None)
except BaseException as error:
    assert type(error) is expected, 'setup exception type was replaced: '+type(error).__name__
    assert str(error)==message, 'setup exception message was replaced'
else:raise AssertionError('setup failure was lost')
assert caller.is_alive(), 'caller diagnostic thread was stopped'
restored=snapshot()==before
assert restored, 'caller globals were changed'
restored_modules=modules=={k:v for k,v in sys.modules.items() if k=='open_trader' or k.startswith('open_trader.') or k=='tests' or k.startswith('tests.')}
assert restored_modules, 'caller modules were changed'
# A subsequent public launch also proves the isolation lock and runtime owner are usable.
with InProcessService(root/'recovery',scenario='nr-stale') as child:
    child.release('metadata');child.start(expected_running=None)
    child.wait('nr-cycle-known-completed')
child.assert_owner_available()
assert caller.is_alive(), 'successful isolated service stopped caller diagnostics'
'''
    result=subprocess.run([sys.executable,'-c',probe,str(tmp_path),phase],capture_output=True,text=True,timeout=60)
    (tmp_path/'setup-probe.log').write_text(result.stdout+result.stderr)
    assert result.returncode==0, result.stdout+result.stderr


@pytest.mark.parametrize('scenario',['nr-stale','nr-unknown'])
def test_runtime_template_needs_no_socket_bind(tmp_path,monkeypatch,scenario):
    import socket
    from tests.lp_d2_service_harness import release_template
    from tests import test_prediction_runtime as cases
    release_template.cache_clear()
    attempted=[]
    def deny_bind(sock,address):
        attempted.append(address)
        raise PermissionError('offline test denies socket bind')
    check=(cases.test_d2_paused_restart_ignores_stale_lp_execution_bookkeeping_without_reconcile
        if scenario=='nr-stale' else cases.test_d2_account_unknown_stays_online_without_buy_until_fresh_complete_facts)
    denied=False
    with monkeypatch.context() as patch:
        patch.setattr(socket.socket,'bind',deny_bind)
        try:check(tmp_path)
        except PermissionError:denied=True
    assert not denied, 'Runtime-only path attempted a socket bind'
    assert attempted==[]
