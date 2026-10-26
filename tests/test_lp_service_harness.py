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


def canonical_sources(value):
    # Fresh reads have independent UUIDs; preserve their equality relationships.
    identifiers={}
    def canonical(item):
        if isinstance(item,list):return [canonical(value) for value in item]
        if isinstance(item,dict):
            result={}
            for key,value in item.items():
                if key in ('source_id','observation_id') and value is not None:
                    assert isinstance(value,str) and value
                    value=identifiers.setdefault(value,len(identifiers))
                result[key]=canonical(value)
            return result
        return item
    return canonical(value)


@pytest.mark.parametrize('scenario',['nr-stale','nr-unknown','current-known','current-incomplete-orders',
    'current-incomplete-positions','current-foreign','current-restart','current-manual','current-pools',
    'optional-catalog','optional-history','optional-share','optional-metadata','optional-hourly',
    'prep-failure','prep-partial','prep-metadata','prep-absent','candidate','trial',
    'nleg-paused','manual-preserved','stale-report'])
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
        from tests.test_lp_auto_restart_preparation import _new_owner,_terminate_old,_assert_current_after_legacy
        build=request.getfixturevalue('runtime')
        old,_,_=request.getfixturevalue('dead_preparation');_terminate_old(old)
        owner,store,adapter,account,public=_new_owner(build,monkeypatch,tmp_path)
        owner.open();owner.close()
        mode=scenario.removeprefix('current-')
        results=[_assert_current_after_legacy(tmp_path/service.__name__,store,account,public,
            mode='known' if mode in ('restart','manual','pools') else mode, restart=mode=='restart',manual=mode=='manual',observed=mode=='pools',
            service=service) for service in (ServiceChild,InProcessService)]
        assert canonical_sources(results[0])==canonical_sources(results[1])
        assert results[0]['requests']['cancel']==[]
        assert len(results[0]['requests']['post'])==(1 if mode=='manual' else 0)
        assert (results[0]['state']['observed_pools'] is None) is (mode!='pools')
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
