"""Public regressions for accepted default-D2 service cutover blockers."""
from decimal import Decimal
import json
import signal
import threading
import time
import fcntl

import pytest

from open_trader.lp_auto_cli import _Client
from tests.lp_d2_service_harness import ServiceChild


def test_reservations_and_settings_configuration_complete_concurrently(tmp_path):
    threads=[]
    class ObservedLockChild(ServiceChild):
        def request(self,path,payload=None):
            try:return super().request(path,payload)
            except (OSError,ValueError) as exc:
                with (self.events/'repair-http-errors.jsonl').open('a') as out:
                    out.write(json.dumps(dict(path=path,error_type=type(exc).__name__,
                        error=str(exc),status=getattr(exc,'code',None),
                        errno=getattr(exc,'errno',None),observed_at=time.monotonic()))+'\n')
                raise
    with ObservedLockChild(tmp_path,scenario='rollback-current-repair-lock-order') as child:
        try:
            child.release('metadata')
            child.start(expected_running=False)
            initial=child.wait_state(lambda value:any(order['key'][-1]=='api-existing-buy'
                for order in value.get('account_buys',())))
            assert initial['effective_settings']['config_version']==1
            assert initial['settings_pending'] is False
            client=_Client(child.base,10)
            bootstrap=client.request('/api/prediction-arbitrage/venues')
            results={};errors=[]
            def request(name,path,payload=None):
                try:results[name]=client.request(path,payload,csrf=bootstrap['csrf_token'] if payload is not None else None)
                except BaseException as exc:errors.append((name,type(exc).__name__,str(exc)))
            child.marker('repair-lock-arm').touch()
            reservations=threading.Thread(target=request,args=('reservations',
                '/api/prediction-arbitrage/lp/auto/reservations'),daemon=True)
            threads.append(reservations);reservations.start()
            child.wait('repair-reservations-admitted',timeout=5)
            configure=threading.Thread(target=request,args=('configure',
                '/api/prediction-arbitrage/lp/auto/config',
                {'api_retry_interval_seconds':61,'expected_config_version':1}),daemon=True)
            threads.append(configure);configure.start()
            child.wait('repair-reservations-state-held',timeout=5)
            child.wait('repair-configure-settings-held',timeout=5)
            deadline=time.monotonic()+5
            for thread in threads:thread.join(max(0,deadline-time.monotonic()))
            if any(thread.is_alive() for thread in threads):
                child.process.send_signal(signal.SIGUSR1)
                raise AssertionError('real public reservations/configuration requests deadlocked under watchdog')
            assert not errors,errors
            assert set(results)=={'reservations','configure'}
            view=results['reservations']
            assert view['authority']=='current_api' and view['desired_running'] is False
            assert len(view['reservations'])==1
            order=view['reservations'][0]
            from tests.test_lp_order_registration_contract import CONDITION_ID,TOKEN_ID,WALLET
            import hashlib
            assert view['account_id']==WALLET
            assert (order['condition_id'],order['token_id'],order['order_id'])==(
                CONDITION_ID,TOKEN_ID,'api-existing-buy')
            assert Decimal(order['remaining_quantity'])==Decimal('8')
            assert Decimal(order['price'])==Decimal('.40')
            assert Decimal(order['amount_usd'])==Decimal('3.20')
            assert order['releasable'] is False
            assert order['reason']=='current_api_reservations_not_releasable'
            for name,expected in (('buy_reserved_usd','3.20'),('inventory_cost_usd','.60'),
                    ('current_exposure_usd','3.80'),('new_capacity_usd','36.80')):
                assert Decimal(view[name])==Decimal(expected)
            settled=child.wait_state(lambda value:not value['settings_pending']
                and value['effective_settings']['config_version']==2)
            assert settled['effective_settings']['api_retry_interval_seconds']==61
            assert settled['fatal'] is None
            assert len(list(child.events.glob('repair-settings-commit-*')))==1
            paused=child.pause()
            assert paused['pause_confirmed'] is True
            assert paused['effective_running'] is False
            assert child.actual_posts()==0
            child.process.terminate()
            assert child.wait_exit()==0
            child.assert_owner_available()
            assert json.loads(child.permit_path.read_text())['state']=='ARMED'
        except BaseException:
            if child.process.poll() is None:
                child.process.send_signal(signal.SIGUSR1)
            (child.events/'repair-state-failure.json').write_text(json.dumps(dict(
                child_pid=child.process.pid,returncode=child.process.poll(),
                native_commit_files=[f.name for f in child.events.glob('repair-settings-commit-*')],
                native_join=(json.loads(child.marker('repair-settings-native-join.json').read_text())
                    if child.marker('repair-settings-native-join.json').exists() else None))))
            raise
        finally:
            # A real deadlock cannot obey SIGTERM. The independent watchdog
            # owns this disposable child PID only; never target a host service.
            if any(thread.is_alive() for thread in threads) and child.process.poll() is None:
                child.process.kill();child.process.wait(timeout=5)
            for thread in threads:thread.join(5)


@pytest.mark.parametrize('prior_state',('ready','failed'))
def test_explicit_systemd_restart_recovers_absent_ready_or_failed_record(tmp_path,monkeypatch,prior_state):
    from open_trader import prediction_cloud as cloud
    from open_trader.prediction_release import load_prediction_runtime_record
    from tests.test_lp_d2_service_lifecycle import _assert_systemd_service_identity
    with ServiceChild(tmp_path,scenario='rollback-current') as child:
        child.release('metadata')
        child.start(expected_running=False)
        child.wait_state(lambda value:any(order['key'][-1]=='api-existing-buy'
            for order in value.get('account_buys',())))
        assert child.actual_posts()==0
        child.process.terminate()
        assert child.wait_exit()==0
        child.assert_owner_available()
        def restart(c,starts,before_service_creation):
            # Historical saved labels are the given input after an actual
            # READY child exits; no current READY response is manufactured.
            with cloud.OPERATION_LOCK.open('r+') as operation:
                fcntl.flock(operation,fcntl.LOCK_EX|fcntl.LOCK_NB)
                cloud.record(c,prior_state,operation_fd=operation.fileno())
            saved=load_prediction_runtime_record(c.record)
            original_ci=saved['candidate']['lp_validation']['ci']
            old_permit=json.loads(child.permit_path.read_text())
            assert saved['state']==prior_state
            assert cloud.unit_state()['MainPID']=='0'
            with pytest.raises(before_service_creation,match='before actual systemd start'):
                cloud.operate(c,'restart')
            assert len(starts)==1
            armed=json.loads(child.permit_path.read_text())
            assert armed['state']=='ARMED' and armed['operation_nonce']==starts[0]
            assert armed['operation_nonce']!=old_permit['operation_nonce']
            current=load_prediction_runtime_record(c.record)
            assert current['state']=='maintenance'
            assert current['candidate']['git_sha']==child.sha
            assert current['candidate']['lp_validation']['ci']==original_ci
            assert current['candidate']['lp_validation']['operation_nonce']==starts[0]
            assert 'pending_lp_validation' not in current
            assert child.actual_posts()==0
        _assert_systemd_service_identity(child,monkeypatch,restart_recovery=restart)


def test_foreground_operator_from_other_cwd_remains_manageable(tmp_path):
    from tests.test_lp_d2_service_lifecycle import _assert_foreground_operator_entry
    caller=tmp_path/'outside-release';caller.mkdir()
    _assert_foreground_operator_entry(tmp_path/'managed-child',caller_cwd=caller)


def test_post_ready_failure_keeps_claim_until_actual_drain_and_human_recovery(tmp_path):
    import hashlib
    from datetime import UTC,datetime
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    with ServiceChild(tmp_path,scenario='repair-owner-loop-fault') as child:
        child.start()
        for resource in ('post','business','metadata'):child.wait(resource+'-entered')
        healthy=child.state()
        assert healthy['effective_running'] is True and healthy['desired_running'] is True
        assert healthy['fatal'] is None
        claimed=child.permit_path.read_bytes()
        assert json.loads(claimed)['state']=='CLAIMED'
        child.begin_held_read_http()
        child.marker('owner-loop-fault-arm').touch()
        child.wait('owner-loop-fault-fired')
        child.wait('metadata-admission-closed')
        child.assert_owned()
        assert not child.marker('public-client-closed').exists()
        deadline=time.monotonic()+5
        while True:
            try:child.request('/healthz')
            except OSError:break
            assert time.monotonic()<deadline,'fault must close new HTTP admission'
            threading.Event().wait(.01)
        assert child.actual_posts()==1
        for resource in ('post','business','metadata'):
            child.release(resource);child.wait(resource+'-ended')
            child.assert_owned()
            assert not child.marker('public-client-closed').exists()
            assert not child.marker('process-finalizing').exists()
            assert child.permit_path.read_bytes()==claimed
        child.release('http')
        status,payload=child.wait_http()
        assert status==503
        held_health=json.loads(payload)
        assert held_health['status']=='unavailable' and held_health['runtime_state']=='STOPPING'
        assert child.wait_exit()==78,child.output.read_text()
        child.assert_owner_available()
        assert child.permit_path.read_bytes()==claimed
        assert child.marker('public-client-closed').read_text()==child.marker('public-client-created').read_text()
        assert child.marker('process-finalizing').exists()
        assert 'OSError: offline owner-loop selector failure' in child.output.read_text()
        account=hashlib.sha256(('0x'+'a'*40).encode()).hexdigest()
        store=PredictionArbitrageStore(child.data_dir)
        assert store.lp_load_current_control(account)['desired_running'] is True
        assert not child.marker('control-failure-1').exists()
        child.launch('automatic-after-owner-loop-fault')
        assert child.wait_exit()==78
        assert not child.marker('startup-control-settings-read-automatic-after-owner-loop-fault').exists()
        assert child.actual_posts()==1 and child.permit_path.read_bytes()==claimed
        child.assert_owner_available()
        store.lp_save_current_control(dict(account_id=account,process_epoch='operator',request_id='pause',
            request_version=1,desired_running=False,requested_at=datetime(2026,10,16,12,tzinfo=UTC),actor='operator'))
        child.launch('operator-paused',expected_running=False)
        paused=child.wait_state(lambda value:value['new_capacity_usd']=='40')
        assert paused['desired_running'] is False and paused['effective_running'] is False
        assert child.actual_posts()==1
        child.process.terminate();assert child.wait_exit()==0
        assert json.loads(child.permit_path.read_text())['state']=='ARMED'
        child.assert_owner_available()
