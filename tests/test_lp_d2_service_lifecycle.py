"""Offline whole-program D2 lifecycle at the real service/owner boundary."""
import importlib
import json
import time

from tests.lp_d2_service_harness import ServiceChild


def test_control_save_retry_failure_stops_program_after_real_io_drains(tmp_path):
    # This genuinely new public protocol is a startup prerequisite, never a
    # readiness flag or a substitute for claiming under the real Runtime lock.
    guard=importlib.import_module('open_trader.prediction_start_guard')
    assert callable(guard.arm_for_operator_start)
    # Reader and sender use truthful separate process variants; a blocked
    # source read cannot also have reached the planned send in that run.
    for scenario in ('reader','retry_failure'):
        _assert_retry_failure_drains(tmp_path/scenario,scenario)


def _assert_retry_failure_drains(root,scenario):
    with ServiceChild(root,scenario=scenario) as child:
        child.start()
        resources=('reader','metadata') if scenario=='reader' else ('business','metadata','post')
        for resource in resources:child.wait(resource+'-entered')
        if scenario=='reader':
            child.begin_settings_save()
            child.begin_manual_read()
            resources=('reader','settings','metadata','manual')
        child.wait('public-client-created')
        running=child.state()
        assert running['effective_running'] is True
        child.begin_held_http()
        child.wait('http-entered')
        paused=child.pause()
        assert paused['desired_running'] is False and paused['effective_running'] is False
        assert paused['pause_confirmed'] is False
        assert paused['requested_control_version']==1 and paused['saved_control_version']==0
        child.wait('control-failure-1')
        child.wait_state(lambda s:s['control_save_state']=='retry_wait')
        child.advance(.9)
        assert child.state()['control_save_state']=='retry_wait'
        assert not child.marker('control-failure-2').exists()
        child.advance(1)
        child.wait('control-failure-2')
        child.wait('metadata-admission-closed')
        child.assert_owned()
        assert not child.marker('public-client-closed').exists()
        assert not child.marker('process-finalizing').exists()
        # Completion of any one owner does not release another native job,
        # active HTTP handler, shared client or the whole process owner.
        for resource in resources:
            child.release(resource)
            child.wait(resource+'-ended')
            if resource=='manual':child.wait_manual()
            child.assert_owned()
            assert not child.marker('public-client-closed').exists()
            assert not child.marker('process-finalizing').exists()
        child.release('http')
        child.wait_http()
        assert child.wait_exit()==78
        child.assert_owner_available()
        assert child.marker('public-client-closed').read_text()==child.marker('public-client-created').read_text()
        assert child.marker('process-finalizing').exists()
        assert not child.marker('control-failure-3').exists()
        permit=json.loads(child.permit_path.read_text())
        assert permit['state']=='CLAIMED'
        assert child.actual_posts()==(0 if scenario=='reader' else 1)
        assert child.marker('legacy-bookkeeping-unavailable').exists()
        if scenario!='reader':assert child.marker('business-record-unavailable').exists()


def test_late_control_save_result_cannot_clear_fatal_shutdown(tmp_path):
    with ServiceChild(tmp_path,scenario='late_control') as child:
        child.start()
        for resource in ('post','business','metadata'):child.wait(resource+'-entered')
        child.begin_held_http()
        child.begin_held_http('http-late')
        paused=child.pause()
        assert paused['effective_running'] is False and paused['pause_confirmed'] is False
        child.wait('control-entered')
        child.advance(10.01)
        child.wait('metadata-admission-closed')
        child.assert_owned()
        assert not child.marker('public-client-closed').exists()
        assert not child.marker('control-call-2').exists()
        child.release('http');child.wait_http()
        status,payload=child.http_result[0]
        assert status==200
        unknown=json.loads(payload)
        assert unknown['control_save_state']=='unknown'
        assert unknown['control_native_inflight'] is True
        assert unknown['saved_control_version']==0
        assert unknown['effective_running'] is False and unknown['fatal']
        child.advance(12)
        child.release('control');child.wait('control-ended')
        child.wait('late-control-observation-ready')
        child.release('http-late')
        status,payload=child.wait_http('http-late')
        assert status==200
        late=json.loads(payload)
        assert late['saved_control_version']==late['requested_control_version']==1
        assert late['control_save_state']=='saved' and late['control_native_inflight'] is False
        assert late['effective_running'] is False and late['desired_running'] is False
        assert late['pause_confirmed'] is False and late['fatal']==unknown['fatal']
        # The actual committed durable result is observable through the public
        # Store port. It never authorizes clearing the process fatal or permit.
        from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
        import hashlib
        account=hashlib.sha256(('0x'+'a'*40).encode()).hexdigest()
        saved=PredictionArbitrageStore(child.data_dir).lp_load_current_control(account)
        assert saved['account_id']==account and saved['desired_running'] is False
        child.assert_owned()
        assert not child.marker('public-client-closed').exists()
        assert not child.marker('control-call-2').exists()
        for resource in ('business','metadata','post'):
            child.release(resource);child.wait(resource+'-ended')
        assert child.wait_exit()==78
        assert child.actual_posts()==1
        assert json.loads(child.permit_path.read_text())['state']=='CLAIMED'
        assert child.marker('public-client-closed').read_text()==child.marker('public-client-created').read_text()
        child.assert_owner_available()


def test_fatal_save_exit_cannot_relaunch_trading_from_stale_running_intent(tmp_path):
    import pytest,sys,hashlib,plistlib
    from pathlib import Path
    from open_trader import prediction_start_guard as guard
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from open_trader.prediction_cloud import CloudConfig,render_unit
    with ServiceChild(tmp_path/'relaunch',scenario='retry_failure') as child:
        child.start();child.wait('post-entered')
        identity=guard.identity_for_runtime(data_dir=child.data_dir,
            prediction_config_path=child.runtime_root/'prediction.json',code_root=child.release_root,port=child.port)
        with pytest.raises(guard.PredictionStartDenied):guard.arm_for_operator_start(identity,'cannot-steal-live-owner')
        child.pause();child.wait('control-failure-1')
        child.wait_state(lambda s:s['control_save_state']=='retry_wait')
        child.advance(1);child.wait('control-failure-2');child.wait('metadata-admission-closed')
        for name in ('business','metadata','post'):child.release(name)
        assert child.wait_exit()==78
        original=child.permit_path.read_bytes()
        assert json.loads(original)['state']=='CLAIMED'
        account=hashlib.sha256(('0x'+'a'*40).encode()).hexdigest()
        store=PredictionArbitrageStore(child.data_dir)
        assert store.lp_load_current_control(account)['desired_running'] is True
        # Every fresh bare entry must fail before reliable reads or a new POST.
        backup=child.runtime_root/'saved-permit';backup.write_bytes(original);backup.chmod(0o600)
        for variant in ('claimed','missing','malformed','wrong-target','permissions','symlink'):
            child.permit_path.unlink(missing_ok=True)
            child.permit_path.write_bytes(original);child.permit_path.chmod(0o600)
            if variant=='missing':child.permit_path.unlink()
            elif variant=='malformed':child.permit_path.write_text('{')
            elif variant=='wrong-target':
                changed=json.loads(original);changed['identity']['release_sha']='0'*40
                child.permit_path.write_text(json.dumps(changed))
            elif variant=='permissions':child.permit_path.chmod(0o644)
            elif variant=='symlink':
                child.permit_path.unlink();child.permit_path.symlink_to(backup)
            label='automatic-'+variant
            child.launch(label)
            assert child.wait_exit()==78
            assert not child.marker('startup-control-settings-read-'+label).exists()
            assert child.actual_posts()==1
            child.assert_owner_available()
        child.permit_path.unlink(missing_ok=True);child.permit_path.write_bytes(original);child.permit_path.chmod(0o600)
        # Human fault recovery removes only the start prohibition. The actual
        # persisted pause wins over the prior stale running state.
        from datetime import UTC,datetime
        store.lp_save_current_control(dict(account_id=account,process_epoch='operator',request_id='pause',
            request_version=1,desired_running=False,requested_at=datetime(2026,10,16,12,tzinfo=UTC),actor='operator'))
        child.launch('operator-paused',expected_running=False)
        paused=child.wait_state(lambda s:s['new_capacity_usd']=='40')
        assert paused['effective_running'] is False and paused['desired_running'] is False
        assert child.actual_posts()==1
        child.process.terminate();assert child.wait_exit()==0
        assert json.loads(child.permit_path.read_text())['state']=='ARMED'
        child.assert_owner_available()
    config=CloudConfig(Path('/srv/release'),Path('/var/lib/prediction'),Path('/srv/venv/bin/python'),
        'prediction','a'*40,'ap-hongkong','wallet','v1','reader','production',1)
    unit=render_unit(config)
    assert 'Restart=on-failure\n' in unit and 'RestartPreventExitStatus=78\n' in unit
    assert 'TimeoutStopSec=infinity\n' in unit and 'SendSIGKILL=no\n' in unit
    shadow=CloudConfig(**{**config.__dict__,'mode':'shadow','region':'','secret':'','version':'','role':''})
    assert hashlib.sha256(render_unit(shadow).encode()).hexdigest()=='5657e451941f97c920a51d446a2e37d2ac311c96ecdae4231264b4d2e746391f'
    template=plistlib.loads((Path(__file__).parents[1]/'ops/launchd/com.open-trader.prediction-service.plist.template').read_bytes())
    assert template['RunAtLoad'] is True and template['KeepAlive']=={'SuccessfulExit':False}
    assert type(template['ExitTimeOut']) is int and template['ExitTimeOut']==0
    # Actual child startup control failure, before HTTP READY, with controlled
    # external OS evidence. No installed launchctl/systemd operation is run.
    for scenario,code in (('launchd-context-valid',0),('launchd-context-wrong-pid',78),('launchd-context-unavailable',78)):
        with ServiceChild(tmp_path/scenario,scenario=scenario) as child:
            child.start(expected_running=None)
            assert child.wait_exit()==code,child.output.read_text()
            assert child.actual_posts()==0
            assert json.loads(child.permit_path.read_text())['state']=='CLAIMED'
            child.assert_owner_available()


def test_supported_rollback_uses_current_api_without_disposable_record_audit(tmp_path,monkeypatch):
    from decimal import Decimal
    from pathlib import Path
    import pytest,subprocess,sys,hashlib,shutil
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from open_trader.prediction_release import prepare_lp_target
    from open_trader import prediction_start_guard as guard
    from open_trader import prediction_cloud as cloud
    monkeypatch.setattr(cloud,'UNIT_PATH',tmp_path/'absent-installed-systemd.service')
    _assert_foreground_operator_entry(tmp_path/'foreground-operator')
    for scenario in ('rollback-current','rollback-current-incomplete'):
        with ServiceChild(tmp_path/scenario,scenario=scenario) as child:
            child.release('metadata')
            child.start(expected_running=False)
            original_selection=dict(path=str(child.runtime_root/'prediction.json'),
                sha256=hashlib.sha256((child.runtime_root/'prediction.json').read_bytes()).hexdigest())
            initial_record=json.loads((child.runtime_root/'prediction-service-runtime.json').read_text())
            assert initial_record['candidate']['lp_validation']['config_selection']==original_selection
            current=child.wait_state(lambda s:any(o['key'][-1]=='api-existing-buy' for o in s.get('account_buys',())))
            assert current['desired_running'] is False and current['effective_running'] is False
            assert Decimal(current['buy_reserved_usd'])==Decimal('3.20')
            if scenario.endswith('incomplete'):
                assert current['inventory_cost_usd'] is None and current['current_exposure_usd'] is None
                assert current['new_capacity_usd'] is None
            else:
                assert Decimal(current['inventory_cost_usd'])==Decimal('.60')
                assert Decimal(current['current_exposure_usd'])==Decimal('3.80')
                assert Decimal(current['new_capacity_usd'])==Decimal('36.20')
            reservations=child.request('/api/prediction-arbitrage/lp/auto/reservations')
            assert reservations['authority']=='current_api'
            assert Decimal(reservations['buy_reserved_usd'])==Decimal('3.20')
            assert reservations['reservations'][0]['order_id']=='api-existing-buy'
            assert reservations['reservations'][0]['releasable'] is False
            release=child.request('reservations/release',{'all_releasable':True,'confirm':True,'reason':'current API remains authoritative'})
            assert release['released']==[] and release['reason']=='current_api_reservations_not_releasable'
            assert release['state']['buy_reserved_usd']==current['buy_reserved_usd']
            assert child.actual_posts()==0
            if not scenario.endswith('incomplete'):
                deadline=time.monotonic()+8
                while True:
                    dashboard=child.request('/api/prediction-arbitrage/lp/dashboard')
                    if any(o.get('order_id')=='api-existing-buy' for o in dashboard.get('orders',())):break
                    assert time.monotonic()<deadline,json.dumps(dashboard,sort_keys=True)
                    import threading
                    threading.Event().wait(.01)
                assert dashboard['authenticated'] is True
                assert dashboard['open_orders_complete'] is True and dashboard['positions_complete'] is True
                assert dashboard['checked_at']=='2026-10-16T12:00:00.000000Z'
                position=next(p for p in dashboard['positions'] if p['token_id']=='0x'+'1'*64)
                assert Decimal(position['size'])==Decimal('3') and Decimal(position['average_price'])==Decimal('.20')
                visible=next(o for o in dashboard['orders'] if o['order_id']=='api-existing-buy')
                assert visible['side']=='BUY' and Decimal(str(visible['price']))==Decimal('.40')
                assert tuple(Decimal(visible[k]) for k in ('quantity','filled_quantity','remaining_quantity'))==(Decimal('10'),Decimal('2'),Decimal('8'))
            assert PredictionArbitrageStore(child.data_dir).lp_sessions()==[]
            assert child.marker('legacy-bookkeeping-unavailable').exists()
            # Before any old service disturbance, a reader-compatible release
            # lacking D2's tracked capability must be rejected as old authority.
            old=tmp_path/('old-d1-'+scenario);(old/'ops').mkdir(parents=True)
            shutil.copyfile(child.release_root/'ops/prediction-service-release.json',old/'ops/prediction-service-release.json')
            for args in (['init','-q'],['add','.'],['-c','user.name=D2 fixture','-c','user.email=d2@invalid','commit','-qm','old incompatible authority'],['checkout','--detach','-q']):
                subprocess.run(['git','-C',str(old),*args],check=True,capture_output=True)
            before=child.permit_path.read_bytes();record_path=child.runtime_root/'prediction-service-runtime.json'
            record_before=record_path.read_bytes()
            with pytest.raises((ValueError,OSError)):
                prepare_lp_target(runtime_root=child.runtime_root,release_root=old,python=sys.executable,
                    operation_nonce='must-reject-old-before-stop',port=child.port)
            child.assert_owned()
            assert child.permit_path.read_bytes()==before and record_path.read_bytes()==record_before
            child.release('metadata');child.process.terminate();assert child.wait_exit()==0
            if scenario.endswith('incomplete'):continue
            config=child.runtime_root/'prediction.json';original_config=config.read_bytes()
            config.write_bytes(original_config+b'\n')
            # An old ARMED permission cannot adopt even a valid whitespace-only
            # file change, nor read stale saved RUN/PAUSE before denial.
            child.launch('automatic-rollback-current-config-changed',expected_running=None)
            assert child.wait_exit()==78
            assert not child.marker('startup-control-settings-read-automatic-rollback-current-config-changed').exists()
            assert child.actual_posts()==0
            # The internally produced first verification is retained. Exact
            # compatible rollback rechecks the actual local runtime, without
            # needing expired forward CI artifacts or another backend gate.
            actual_run=subprocess.run
            def external(command,*a,**kw):
                if isinstance(command,list) and '--expected-sha' in command and any(str(v).endswith('deployment_preflight.py') for v in command):
                    raise AssertionError('retained rollback requested expired forward CI evidence')
                return actual_run(command,*a,**kw)
            with monkeypatch.context() as boundary:
                boundary.setattr(subprocess,'run',external)
                prepare_lp_target(runtime_root=child.runtime_root,release_root=child.release_root,python=sys.executable,
                    operation_nonce='retained-compatible-rollback',port=child.port)
            identity=guard.identity_for_runtime(data_dir=child.data_dir,prediction_config_path=child.runtime_root/'prediction.json',
                code_root=child.release_root,port=child.port)
            assert identity.config_digest==hashlib.sha256(config.read_bytes()).hexdigest()
            prospective=record_path.read_bytes();permit_before=child.permit_path.read_bytes()
            with pytest.raises(guard.PredictionStartDenied):guard.arm_for_operator_start(identity,'wrong-forward-nonce')
            assert record_path.read_bytes()==prospective and child.permit_path.read_bytes()==permit_before
            selected=config.read_bytes();config.write_bytes(selected+b'\n')
            with pytest.raises(ValueError):
                prepare_lp_target(runtime_root=child.runtime_root,release_root=child.release_root,python=sys.executable,
                    operation_nonce='retained-compatible-rollback',require_prepared=True,config_path=config,port=child.port)
            with pytest.raises(guard.PredictionStartDenied):guard.arm_for_operator_start(identity,'retained-compatible-rollback')
            assert record_path.read_bytes()==prospective and child.permit_path.read_bytes()==permit_before
            config.write_bytes(selected)
            guard.arm_for_operator_start(identity,'retained-compatible-rollback')
            child.launch('automatic-rollback-current',expected_running=False)
            same=child.wait_state(lambda s:s.get('current_exposure_usd') is not None)
            assert Decimal(same['current_exposure_usd'])==Decimal('3.80') and same['effective_running'] is False
            assert child.actual_posts()==0
            child.process.terminate();assert child.wait_exit()==0
            _assert_systemd_service_identity(child,monkeypatch)
    _assert_launchd_config_layouts(tmp_path/'launchd-config-layouts')
    _assert_superseded_target_drain(tmp_path/'superseded-target')




def _assert_foreground_operator_entry(root,*,caller_cwd=None):
    from decimal import Decimal
    import os,sys,subprocess
    with ServiceChild(root,scenario='rollback-current-foreground-cli') as child:
        if caller_cwd is not None:child.caller_cwd=caller_cwd
        child.release('metadata');child.start(expected_running=False)
        execution=json.loads(child.marker('foreground-native-exec').read_text())
        assert execution['pid']==child.process.pid
        assert not any(value.startswith('--operator-') for value in execution['arguments'])
        health=child.request('/healthz')
        assert health['pid']==child.process.pid and health['production_owner'] is True
        if caller_cwd is not None:
            assert health['cwd']==str(child.release_root)
            assert health['git_sha']==child.sha
        permit=json.loads(child.permit_path.read_text())
        assert permit['state']=='CLAIMED' and permit['pid']==child.process.pid
        state=child.wait_state(lambda s:s.get('current_exposure_usd') is not None)
        assert state['desired_running'] is False and state['effective_running'] is False
        assert Decimal(state['current_exposure_usd'])==Decimal('3.80')
        child.assert_owned();assert child.actual_posts()==0
        record=child.runtime_root/'prediction-service-runtime.json'
        saved_record=record.read_bytes();saved_permit=child.permit_path.read_bytes()
        def operation(action,*,wrong=False):
            output=child.root/('operator-'+action+('-wrong' if wrong else '')+'.log')
            environment=dict(os.environ,PYTHONPATH=str(child.release_root/'src')+os.pathsep+str(child.release_root),
                PYTHONDONTWRITEBYTECODE='1',OPEN_TRADER_NLEG_PAUSED='1',OPEN_TRADER_DISABLE_NLEG_BACKGROUND='1',
                D2_FIXTURE_FOREGROUND_OLD_PID=str(child.process.pid),
                D2_FIXTURE_FOREGROUND_OLD_ARGS=json.dumps(execution['arguments']))
            if wrong:environment['D2_FIXTURE_FOREGROUND_WRONG_ARGS']='1'
            with output.open('w') as handle:
                process=subprocess.Popen([sys.executable,'-m','tests.lp_d2_service_harness','child',
                    str(child.runtime_root),str(child.port),'operator-'+action+'-foreground-cli',
                    str(child.config_path)],cwd=caller_cwd or child.release_root,env=environment,
                    stdout=handle,stderr=subprocess.STDOUT)
            return process,output
        for wrong,expected in ((True,78),(False,0)):
            noop,output=operation('start',wrong=wrong)
            assert noop.wait(timeout=15)==expected,output.read_text()
            assert record.read_bytes()==saved_record and child.permit_path.read_bytes()==saved_permit
            assert json.loads(child.marker('foreground-native-exec').read_text())['pid']==execution['pid']
            child.assert_owned();assert child.actual_posts()==0
        child.begin_held_http()
        old=child.process
        replacement,output=operation('restart')
        child.process=replacement;child.output=output
        child.wait('foreground-term-sent')
        assert old.poll() is None
        assert child.permit_path.read_bytes()==saved_permit
        child.assert_owned();assert child.actual_posts()==0
        child.release('http');child.wait_http()
        assert old.wait(timeout=15)==78
        state=child.wait_state(lambda s:s.get('current_exposure_usd') is not None)
        assert state['desired_running'] is False and state['effective_running'] is False
        assert Decimal(state['current_exposure_usd'])==Decimal('3.80')
        new_execution=json.loads(child.marker('foreground-native-exec').read_text())
        new_permit=json.loads(child.permit_path.read_text())
        assert new_execution['pid']==replacement.pid!=old.pid
        assert new_permit['state']=='CLAIMED' and new_permit['pid']==replacement.pid
        assert new_permit['claim_id']!=permit['claim_id'] and new_permit['process_epoch']!=permit['process_epoch']
        child.assert_owned();assert child.actual_posts()==0
        child.process.terminate();assert child.wait_exit()==0
        child.assert_owner_available()


def _assert_launchd_config_layouts(root):
    import hashlib,sys,pytest
    from open_trader.prediction_release import prepare_lp_target
    for layout in ('nested','sibling'):
        with ServiceChild(root/layout,scenario='rollback-current-launchd-'+layout) as child:
            child.release('metadata');child.start(expected_running=False)
            record_path=child.runtime_root/'prediction-service-runtime.json'
            record=json.loads(record_path.read_text())
            assert record['manager']=='launchd'
            assert record['candidate']['lp_validation']['config_selection']==dict(
                path=str(child.config_path),sha256=hashlib.sha256(child.config_path.read_bytes()).hexdigest())
            assert not (child.runtime_root/'prediction-service-operation.lock').exists()
            assert child.actual_posts()==0
            if layout=='nested':_assert_launchd_pre_stop_selection(child)
            child.process.terminate();assert child.wait_exit()==0
            child.assert_owner_available()
            if layout=='nested':_assert_launchd_installer_arm(child)
            before_record=record_path.read_bytes();before_permit=child.permit_path.read_bytes()
            operation=child.runtime_root/'.com.open-trader.prediction-service.release.lock'
            with operation.open('a+') as unheld:
                with pytest.raises(ValueError):
                    prepare_lp_target(runtime_root=child.runtime_root,release_root=child.release_root,
                        python=sys.executable,operation_nonce='unheld-must-not-prepare',manager='launchd',
                        manager_identity=record['manager_identity'],operation_fd=unheld.fileno(),
                        config_path=child.config_path,port=child.port)
            assert record_path.read_bytes()==before_record and child.permit_path.read_bytes()==before_permit



def _assert_launchd_pre_stop_selection(child):
    """Change actual selected bytes at the last real old-owner observation."""
    import os,sys,subprocess
    fake=child.root/'pre-stop-external-os';fake.mkdir()
    changed=child.marker('pre-stop-config-changed');stopped=child.marker('pre-stop-attempted-bootout')
    plist=child.runtime_root/'com.open-trader.prediction-service.plist'
    record=child.runtime_root/'prediction-service-runtime.json'
    original=child.config_path.read_bytes();claimed=child.permit_path.read_bytes()
    script='#!'+sys.executable+'\n'+'''
import json,os,sys,plistlib
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
kind=Path(sys.argv[0]).name
pid=os.environ['D2_FIXTURE_PID'];root=Path(os.environ['D2_FIXTURE_ROOT'])
release=Path(os.environ['D2_FIXTURE_RELEASE']);plist=Path(os.environ['D2_FIXTURE_PLIST'])
if kind=='launchctl':
    if sys.argv[1]=='print':
        args=plistlib.loads(plist.read_bytes())['ProgramArguments']
        # The external launchd fixed-port view maps the real fixture listener.
        args[args.index('--port')+1]='8769'
        print('path = '+str(plist)+'\\nworking directory = '+str(release)+'\\npid = '+pid+'\\narguments = {\\n'+'\\n'.join(args)+'\\n}')
        raise SystemExit(0)
    if sys.argv[1]=='bootout':
        Path(os.environ['D2_FIXTURE_STOPPED']).touch()
        # Observe the wrong stop attempt without changing the actual child.
        raise SystemExit(1)
    raise SystemExit('unexpected external launchd action')
if kind=='lsof':
    if 'cwd' in sys.argv:print('p'+pid+'\\nfcwd\\nn'+str(release))
    elif '-iTCP:8769' in sys.argv:print('p'+pid+'\\nn127.0.0.1:8769')
    else:print('p'+pid+'\\nn'+str(root/'data/prediction_arbitrage/runtime.lock'))
    raise SystemExit(0)
if kind=='curl':
    assert sys.argv[-1]=='http://127.0.0.1:8769/healthz'
    with build_opener(ProxyHandler({})).open(os.environ['D2_FIXTURE_HEALTH'],timeout=4) as response:
        raw=response.read()
    record=json.loads(Path(os.environ['D2_FIXTURE_RECORD']).read_text())
    marker=Path(os.environ['D2_FIXTURE_CHANGED'])
    if record.get('pending_lp_validation') is not None and not marker.exists():
        config=Path(os.environ['D2_FIXTURE_CONFIG']);config.write_bytes(config.read_bytes()+b'\\n')
        marker.touch()
    sys.stdout.buffer.write(raw)
    raise SystemExit(0)
raise SystemExit('unexpected external OS boundary')
'''
    for name in ('launchctl','lsof','curl'):
        path=fake/name;path.write_text(script);path.chmod(0o700)
    environment=dict(os.environ,LAUNCHCTL_BIN=str(fake/'launchctl'),LSOF_BIN=str(fake/'lsof'),
        CURL_BIN=str(fake/'curl'),D2_FIXTURE_MANAGER_PORT=str(child.port),
        D2_FIXTURE_PID=str(child.process.pid),D2_FIXTURE_ROOT=str(child.runtime_root),
        D2_FIXTURE_RELEASE=str(child.release_root),D2_FIXTURE_PLIST=str(plist),
        D2_FIXTURE_STOPPED=str(stopped),D2_FIXTURE_CHANGED=str(changed),
        D2_FIXTURE_CONFIG=str(child.config_path),D2_FIXTURE_RECORD=str(record),
        D2_FIXTURE_HEALTH=child.base+'/healthz',PYTHONDONTWRITEBYTECODE='1')
    command=['bash',str(child.release_root/'scripts/install_prediction_service_launchd.sh'),
        '--mode','production','--restart','--repo-root',str(child.release_root),
        '--runtime-root',str(child.runtime_root),'--python',sys.executable,
        '--launch-agents-dir',str(plist.parent),'--expected-sha',child.sha,'--wait-seconds','1']
    try:
        result=subprocess.run(command,cwd=child.release_root,env=environment,
            capture_output=True,text=True,timeout=60)
        diagnostic=result.stdout+'\n'+result.stderr
        assert changed.exists(),diagnostic
        assert result.returncode!=0,diagnostic
        assert not stopped.exists(),diagnostic
        assert 'LP selected config does not match validation' in diagnostic,diagnostic
        child.assert_owned()
        assert child.permit_path.read_bytes()==claimed and child.actual_posts()==0
    finally:child.config_path.write_bytes(original)


def _assert_launchd_installer_arm(child):
    """Actual installer/producer/guard; external launchd stops before creation."""
    import fcntl,os,subprocess,sys,pytest
    from open_trader.prediction_release import prepare_lp_target
    plist=child.runtime_root/'com.open-trader.prediction-service.plist'
    lock=plist.parent/'.com.open-trader.prediction-service.release.lock'
    record_path=child.runtime_root/'prediction-service-runtime.json'
    saved_record=record_path.read_bytes();saved_permit=child.permit_path.read_bytes()
    # The producer is the pre-stop trust boundary. It cannot repair/adopt a
    # writable record and defer trust validation until the later guard arm.
    record_path.chmod(0o666)
    try:
        with lock.open('r+') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with pytest.raises(ValueError,match='record.*metadata'):
                prepare_lp_target(runtime_root=child.runtime_root,release_root=child.release_root,
                    python=sys.executable,operation_nonce='untrusted-record-must-not-select',manager='launchd',
                    manager_identity=str(plist),operation_fd=held.fileno(),config_path=child.config_path)
        assert record_path.read_bytes()==saved_record and child.permit_path.read_bytes()==saved_permit
    finally:record_path.chmod(0o600)
    fake=child.root/'external-os';fake.mkdir()
    bootstrap=child.marker('installer-before-service-creation')
    launchctl=fake/'launchctl'
    launchctl.write_text('#!'+sys.executable+'\n'+"""
import json,os,sys
from pathlib import Path
if sys.argv[1] in ('print','bootout'):
    print('Could not find service',file=sys.stderr);raise SystemExit(113)
if sys.argv[1]=='bootstrap':
    permit=json.loads(Path(os.environ['D2_FIXTURE_PERMIT']).read_text())
    assert permit['state']=='ARMED'
    expected=os.environ.get('D2_FIXTURE_EXPECTED_NONCE')
    if expected:assert permit['operation_nonce']==expected
    else:assert permit['operation_nonce']!=os.environ['D2_FIXTURE_PREVIOUS_NONCE']
    record=json.loads(Path(os.environ['D2_FIXTURE_RECORD']).read_text())
    assert record['candidate']['lp_validation']['operation_nonce']==permit['operation_nonce']
    assert record.get('pending_lp_validation') is None
    Path(os.environ['D2_FIXTURE_BEFORE_CREATION']).write_text(permit['operation_nonce'])
    # No service is created and no health/READY observation is invented.
    raise SystemExit(1)
raise SystemExit('unexpected external launchd operation')
""")
    launchctl.chmod(0o700)
    lsof=fake/'lsof';lsof.write_text('#!/bin/sh\nexit 1\n');lsof.chmod(0o700)
    curl=fake/'curl';curl.write_text('#!/bin/sh\nexit 7\n');curl.chmod(0o700)
    command=['bash',str(child.release_root/'scripts/install_prediction_service_launchd.sh'),
        '--mode','production','--repo-root',str(child.release_root),'--runtime-root',str(child.runtime_root),
        '--python',sys.executable,'--launch-agents-dir',str(plist.parent),
        '--expected-sha',child.sha,'--wait-seconds','1']
    environment=dict(os.environ,LAUNCHCTL_BIN=str(launchctl),LSOF_BIN=str(lsof),CURL_BIN=str(curl),
        D2_FIXTURE_MANAGER_PORT=str(child.port),D2_FIXTURE_PERMIT=str(child.permit_path),
        D2_FIXTURE_RECORD=str(record_path),D2_FIXTURE_BEFORE_CREATION=str(bootstrap),
        PYTHONDONTWRITEBYTECODE='1')
    prepared=subprocess.run([*command,'--prepare-target'],cwd=child.release_root,env=environment,
        capture_output=True,text=True,timeout=60)
    assert prepared.returncode==0,prepared.stdout+'\n'+prepared.stderr
    operation=json.loads(prepared.stdout)
    assert set(operation)=={'status','operation_nonce'} and operation['status']=='PREPARED'
    nonce=operation['operation_nonce'];assert type(nonce) is str and nonce
    assert json.loads(record_path.read_text())['pending_lp_validation']['operation_nonce']==nonce
    assert child.permit_path.read_bytes()==saved_permit and not bootstrap.exists()
    selected_record=record_path.read_bytes()
    rejected=subprocess.run([*command,'--operation-nonce','wrong-forwarded-nonce'],
        cwd=child.release_root,env=environment,capture_output=True,text=True,timeout=60)
    assert rejected.returncode!=0 and not bootstrap.exists()
    assert record_path.read_bytes()==selected_record and child.permit_path.read_bytes()==saved_permit
    result=subprocess.run([*command,'--operation-nonce',nonce],cwd=child.release_root,
        env=dict(environment,D2_FIXTURE_EXPECTED_NONCE=nonce),capture_output=True,text=True,timeout=60)
    assert bootstrap.exists(),result.stdout+'\n'+result.stderr
    assert result.returncode==1,result.stdout+'\n'+result.stderr
    child.assert_owner_available()
    _assert_launchd_forwarder(child,nonce,bootstrap,fake)


def _assert_launchd_forwarder(child,previous_nonce,bootstrap,fake):
    """Actual wrapper and native producer; external OS paths remain disposable."""
    import importlib.util,os,subprocess,sys,pytest
    spec=importlib.util.spec_from_file_location('actual_fixture_deploy_release',
        child.release_root/'scripts/deploy_release.py')
    wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
    environment=dict(os.environ,LAUNCHCTL_BIN=str(fake/'launchctl'),LSOF_BIN=str(fake/'lsof'),
        CURL_BIN=str(fake/'curl'),D2_FIXTURE_RECORD=str(child.runtime_root/'prediction-service-runtime.json'))
    gh_called=child.marker('unexpected-forward-CI-request')
    gh=fake/'gh'
    gh.write_text('#!'+sys.executable+'\n'+"""
import os,sys
from pathlib import Path
Path(os.environ['D2_FIXTURE_GH_CALLED']).touch()
print('fixture external GitHub request denied',file=sys.stderr)
raise SystemExit(1)
""")
    gh.chmod(0o700)
    bootstrap.unlink();calls=[];actual_run=subprocess.run
    def external_command(command,*a,**kw):
        command=list(command)
        if command[:2]==['bash',str(child.release_root/'scripts/install_prediction_service_launchd.sh')]:
            # Model only the platform-owned LaunchAgents location. The real
            # selected target/runtime/config/interpreter arguments stay exact.
            command.extend(['--launch-agents-dir',str(child.runtime_root),'--wait-seconds','1'])
            calls.append(command)
        env={**environment,**(kw.pop('env',None) or {}),
            'PATH':str(fake)+os.pathsep+os.environ['PATH'],
            'LAUNCHCTL_BIN':environment['LAUNCHCTL_BIN'],'LSOF_BIN':environment['LSOF_BIN'],
            'CURL_BIN':environment['CURL_BIN'],'D2_FIXTURE_MANAGER_PORT':str(child.port),
            'D2_FIXTURE_PREVIOUS_NONCE':previous_nonce,'D2_FIXTURE_GH_CALLED':str(gh_called),
            'D2_FIXTURE_PERMIT':str(child.permit_path),'D2_FIXTURE_RECORD':environment['D2_FIXTURE_RECORD'],
            'D2_FIXTURE_BEFORE_CREATION':str(bootstrap)}
        env.pop('D2_FIXTURE_EXPECTED_NONCE',None)
        return actual_run(command,*a,env=env,**kw)
    with pytest.MonkeyPatch.context() as os_boundary:
        os_boundary.setattr(subprocess,'run',external_command)
        result=wrapper.main(['--expected-sha',child.sha,'--release-root',str(child.release_root),
            '--runtime-root',str(child.runtime_root),'--python',sys.executable,
            'prediction-launchd','--mode','production','--config',str(child.config_path)])
    assert result==1,'wrapper must preserve the intercepted native bootstrap failure'
    assert not gh_called.exists(),'retained native producer must not rerun forward CI'
    assert len(calls)==2 and '--prepare-target' in calls[0]
    forwarded=calls[1][calls[1].index('--operation-nonce')+1]
    assert forwarded==bootstrap.read_text() and forwarded!=previous_nonce
    assert child.actual_posts()==0
    child.assert_owner_available()


def _assert_superseded_target_drain(root):
    """A trusted next selection never rearms the drained old claim."""
    import sys
    from open_trader.prediction_release import prepare_lp_target
    from open_trader import prediction_start_guard as guard
    with ServiceChild(root,scenario='rollback-current-superseded') as child:
        child.release('metadata');child.start(expected_running=False)
        child.begin_held_http()
        original=child.permit_path.read_bytes()
        assert json.loads(original)['state']=='CLAIMED'
        config=child.runtime_root/'prediction.json'
        config.write_bytes(config.read_bytes()+b'\n')
        prepare_lp_target(runtime_root=child.runtime_root,release_root=child.release_root,
            python=sys.executable,operation_nonce='selected-after-old-claim',
            config_path=config,port=child.port)
        assert child.permit_path.read_bytes()==original
        child.process.terminate();child.wait('metadata-admission-closed')
        child.assert_owned()
        assert child.permit_path.read_bytes()==original
        child.release('http');child.wait_http()
        assert child.wait_exit()==78
        child.assert_owner_available()
        assert child.permit_path.read_bytes()==original
        assert child.actual_posts()==0
        child.launch('automatic-rollback-current-superseded',expected_running=None)
        assert child.wait_exit()==78
        assert child.permit_path.read_bytes()==original
        assert not child.marker('startup-control-settings-read-automatic-rollback-current-superseded').exists()
        identity=guard.identity_for_runtime(data_dir=child.data_dir,
            prediction_config_path=config,code_root=child.release_root,port=child.port)
        guard.arm_for_operator_start(identity,'selected-after-old-claim')
        child.launch('automatic-rollback-current-selected',expected_running=False)
        paused=child.wait_state(lambda state:state.get('current_exposure_usd') is not None)
        assert paused['desired_running'] is False and paused['effective_running'] is False
        assert child.actual_posts()==0
        child.process.terminate();assert child.wait_exit()==0


def _assert_systemd_service_identity(child,monkeypatch,*,restart_recovery=None):
    """Actual manager/guard/files/flocks, with external OS ownership simulated.

    This never changes host ownership or invokes systemd. Metadata returned by
    stat and account lookup, and chown's native effect, are the OS boundaries.
    """
    import os,stat,pwd,grp,fcntl,sys,socket,shutil,subprocess,io
    from pathlib import Path
    from types import SimpleNamespace
    import pytest
    from open_trader import prediction_cloud as cloud,prediction_start_guard as guard
    from open_trader.prediction_release import load_prediction_runtime_record,write_prediction_runtime_record,prepare_lp_target
    root=child.runtime_root;config=root/'config/prediction_arbitrage.json'
    config.parent.mkdir(mode=0o700);(root/'prediction.json').rename(config)
    root.chmod(0o700);child.data_dir.chmod(0o700)
    foreground=root/'prediction-service-runtime.json'
    retained=load_prediction_runtime_record(foreground);foreground.unlink()
    child.assert_owner_available()
    shutil.rmtree(child.permit_path.parent)
    (child.data_dir/'prediction_arbitrage/runtime.lock').unlink()
    c=cloud.CloudConfig(child.release_root,root,Path(sys.executable),'prediction',child.sha,
        'ap-hongkong','wallet','v1','reader','production',1)
    unit=child.root/'installed.service';unit.write_text(cloud.render_unit(c));unit.chmod(0o644)
    operation=child.root/'systemd-operation.lock';operation.touch(mode=0o600)
    actor=[0,0];owners={};actual_stat=os.stat;actual_lstat=os.lstat;actual_fstat=os.fstat
    actual_connect=socket.socket.connect_ex;actual_open=os.open;actual_mkdir=os.mkdir;actual_replace=os.replace
    base_process=subprocess.run;native_io_open=io.open
    manager_state=dict(LoadState='loaded',ActiveState='inactive',SubState='dead',MainPID='0',
        FragmentPath=str(unit),DropInPaths='',NeedDaemonReload='no',User=c.user,Group=c.user,
        WorkingDirectory=str(c.release_root),Environment=' '.join(line.removeprefix('Environment=')
            for line in cloud.render_unit(c).splitlines() if line.startswith('Environment=')))
    def root_process(command,*a,**kw):
        if list(command)[:2]==['systemctl','show']:
            return subprocess.CompletedProcess(command,0,'\n'.join(k+'='+v for k,v in manager_state.items()),'')
        return base_process(command,*a,**kw)
    service=(9123,9124)
    def metadata(info,path=None):
        key=(info.st_dev,info.st_ino)
        if key in owners:uid,gid=owners[key]
        elif path is not None and (Path(path)==root or Path(path).is_relative_to(root)):
            uid,gid=((0,service[1]) if Path(path)==c.record else service)
        else:uid,gid=0,0
        values=list(info);values[4]=uid;values[5]=gid
        if path is not None and Path(path)==Path(sys.executable).parent.parent/'.lock':
            values[0]=(info.st_mode & ~0o777)|0o600
        result=os.stat_result(values)
        if (path is not None and Path(path).is_relative_to(child.root/'next-compatible-release')
                and (stat.S_ISLNK(result.st_mode) or result.st_uid!=0 or result.st_mode & 0o022)):
            print('STOPPED_TARGET_METADATA '+json.dumps(dict(path=str(path),
                native=dict(dev=info.st_dev,ino=info.st_ino,uid=info.st_uid,gid=info.st_gid,mode=oct(info.st_mode)),
                returned=dict(uid=result.st_uid,gid=result.st_gid,mode=oct(result.st_mode)),
                cached_owner=owners.get(key),actor=list(actor))),flush=True)
        return result
    def native_chown(fd,uid,gid):
        info=actual_fstat(fd);owners[(info.st_dev,info.st_ino)]=(uid,gid)
    def path_chown(path,uid,gid,**kw):
        info=actual_lstat(path);owners[(info.st_dev,info.st_ino)]=(uid,gid)
    def native_open(path,*a,**kw):
        try:actual_lstat(path);existed=True
        except FileNotFoundError:existed=False
        fd=actual_open(path,*a,**kw)
        info=actual_fstat(fd);key=(info.st_dev,info.st_ino)
        if not existed:owners[key]=tuple(actor)
        elif key not in owners:
            selected=metadata(info,path);owners[key]=(selected.st_uid,selected.st_gid)
        return fd
    def native_io(file,*a,**kw):
        tracked=isinstance(file,(str,os.PathLike)) and Path(file)==c.runtime_lock
        if tracked:
            try:actual_lstat(file);existed=True
            except FileNotFoundError:existed=False
        handle=native_io_open(file,*a,**kw)
        if tracked and not existed:
            info=actual_fstat(handle.fileno());owners[(info.st_dev,info.st_ino)]=tuple(actor)
        return handle
    def native_mkdir(path,*a,**kw):
        actual_mkdir(path,*a,**kw)
        info=actual_lstat(path);owners[(info.st_dev,info.st_ino)]=tuple(actor)
    def publish(source,destination):
        if Path(destination) in (c.record,child.permit_path):
            info=os.stat(source)
            expected=(0,9124,0o640) if Path(destination)==c.record else (9123,9124,0o600)
            assert (info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode))==expected
        actual_replace(source,destination)
    # Assemble native file timestamps before the isolated ownership/stat view.
    shutil.copytree(child.release_root,child.root/'next-compatible-release',
        ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    with monkeypatch.context() as boundary:
        boundary.setattr(os,'geteuid',lambda:actor[0]);boundary.setattr(os,'getuid',lambda:actor[0])
        boundary.setattr(os,'getegid',lambda:actor[1]);boundary.setattr(os,'getgid',lambda:actor[1])
        boundary.setattr(os,'lstat',lambda p,*a,**kw:metadata(actual_lstat(p,*a,**kw),p))
        boundary.setattr(os,'stat',lambda p,*a,**kw:metadata(actual_stat(p,*a,**kw),None if isinstance(p,int) else p))
        boundary.setattr(os,'fstat',lambda fd:metadata(actual_fstat(fd)))
        boundary.setattr(os,'open',native_open);boundary.setattr(os,'mkdir',native_mkdir)
        boundary.setattr(io,'open',native_io)
        boundary.setattr(os,'replace',publish)
        boundary.setattr(subprocess,'run',root_process)
        boundary.setattr(os,'fchown',native_chown);boundary.setattr(os,'chown',path_chown)
        boundary.setattr(pwd,'getpwnam',lambda name:SimpleNamespace(pw_name='prediction',pw_uid=service[0],pw_gid=service[1]) if name=='prediction' else (_ for _ in ()).throw(KeyError(name)))
        boundary.setattr(grp,'getgrnam',lambda name:SimpleNamespace(gr_gid=service[1]) if name=='prediction' else (_ for _ in ()).throw(KeyError(name)))
        boundary.setattr(socket.socket,'connect_ex',lambda sock,address:actual_connect(sock,('127.0.0.1',child.port)) if address==('127.0.0.1',8769) else actual_connect(sock,address))
        boundary.setattr(cloud,'UNIT_PATH',unit);boundary.setattr(cloud,'OPERATION_LOCK',operation)
        # A genuinely empty systemd record goes through the actual trusted
        # producer. Track native root creation, rather than assuming every
        # object under the service directory already has service ownership.
        import contextlib,importlib.util
        from tests.lp_d2_service_harness import offline_github_fixture
        initial_api=offline_github_fixture(child.release_root,child.root/'initial-systemd-github')
        captures=[]
        def initial_external(command,*a,**kw):
            command=list(command)
            if len(command)>1 and command[1]==str(child.release_root/'scripts/deployment_preflight.py'):
                captures.append(command)
                spec=importlib.util.spec_from_file_location('actual_initial_systemd_verifier',command[1])
                verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
                verifier.GitHub=lambda:initial_api
                out,err=io.StringIO(),io.StringIO()
                with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):status=verifier.main(command[2:])
                if kw.get('check') and status:raise subprocess.CalledProcessError(status,command,out.getvalue(),err.getvalue())
                return subprocess.CompletedProcess(command,status,out.getvalue(),err.getvalue())
            return root_process(command,*a,**kw)
        assert not c.record.exists() and not c.runtime_lock.exists()
        with operation.open('r+') as initial_operation:
            fcntl.flock(initial_operation,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with monkeypatch.context() as github_boundary:
                github_boundary.setattr(subprocess,'run',initial_external)
                prepare_lp_target(runtime_root=root,release_root=child.release_root,python=str(c.python),
                    operation_nonce='initial-systemd-producer',manager='systemd',systemd_config=c,
                    operation_fd=initial_operation.fileno())
            assert len(captures)==1
            initial=load_prediction_runtime_record(c.record)
            assert initial['pending_lp_validation']['source_sha']==child.sha
            assert (c.record.stat().st_uid,c.record.stat().st_gid,stat.S_IMODE(c.record.stat().st_mode))==(0,9124,0o640)
            assert (c.runtime_lock.stat().st_uid,c.runtime_lock.stat().st_gid,
                stat.S_IMODE(c.runtime_lock.stat().st_mode))==(9123,9124,0o600)
            assert not child.permit_path.exists() and child.actual_posts()==0
            with c.runtime_lock.open('r+') as native_owner:
                fcntl.flock(native_owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            # The installed unit reserves only its exact runtime. A missing
            # root record cannot convert that runtime to an ordinary manager.
            parked=c.record.with_suffix('.saved');c.record.rename(parked)
            actor[:]=service
            ordinary={**retained,'manager':'foreground','manager_identity':'foreground:9123',
                'candidate':{**retained['candidate'],'lp_validation':initial['pending_lp_validation']}}
            write_prediction_runtime_record(foreground,ordinary)
            ordinary_bytes=foreground.read_bytes();unit_bytes=unit.read_bytes()
            from dataclasses import replace
            unrelated=replace(c,runtime_root=child.root/'independent-systemd-runtime')
            try:
                unit.write_text(cloud.render_unit(unrelated))
                independent=guard.identity_for_runtime(data_dir=child.data_dir,
                    prediction_config_path=config,code_root=child.release_root,port=8769)
                assert independent.manager=='foreground' and independent.runtime_root==str(root)
                for reserved_text in (unit_bytes.decode(),
                        unit_bytes.decode().replace('Description=OpenTrader Prediction\n',''),
                        unit_bytes.decode().replace('ReadWritePaths='+str(root),
                            'ReadWritePaths='+str(unrelated.runtime_root))):
                    unit.write_text(reserved_text)
                    with pytest.raises(guard.PredictionStartDenied,match='systemd'):
                        guard.identity_for_runtime(data_dir=child.data_dir,
                            prediction_config_path=config,code_root=child.release_root,port=8769)
                assert foreground.read_bytes()==ordinary_bytes and parked.exists()
                assert not child.permit_path.exists() and child.actual_posts()==0
            finally:
                foreground.unlink();parked.rename(c.record);unit.write_bytes(unit_bytes);actor[:]=[0,0]
            # End this unclaimed disposable initial-producer phase under the
            # actual operation lock; the retained-record phase below also
            # independently exercises the guard's fresh owner-file creation.
            for temporary in (c.record,c.runtime_lock):
                info=actual_lstat(temporary);temporary.unlink();owners.pop((info.st_dev,info.st_ino),None)
        # Real management publication must retain validation and publish the
        # root-write/service-read metadata before the atomic replace.
        write_prediction_runtime_record(c.record,{**retained,'manager':'systemd'},owner=(0,service[1]),mode=0o640)
        with operation.open('r+') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            cloud.record(c,'stopped',operation_fd=held.fileno())
        saved=load_prediction_runtime_record(c.record)
        assert saved['service_identity']==dict(user='prediction',uid=9123,gid=9124)
        assert saved['candidate']['lp_validation']==retained['candidate']['lp_validation']
        assert stat.S_IMODE(c.record.stat().st_mode)==0o640
        assert (c.record.stat().st_uid,c.record.stat().st_gid)==(0,9124)
        with operation.open('r+') as unheld:
            with pytest.raises(ValueError,match='retained flock'):
                prepare_lp_target(runtime_root=root,release_root=child.release_root,python=sys.executable,
                    operation_nonce='unlocked-descriptor',manager='systemd',systemd_config=c,
                    operation_fd=unheld.fileno(),port=8769)
        with operation.open('r+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            # Retained producer adopts this actual descriptor; another open
            # descriptor on the same inode must not recursively flock.
            prepare_lp_target(runtime_root=root,release_root=child.release_root,python=sys.executable,
                operation_nonce='systemd-root-operation',manager='systemd',systemd_config=c,
                operation_fd=lock.fileno(),port=8769)
            with monkeypatch.context() as root_process_identity:
                root_process_identity.setattr(sys,'executable','/external/root-manager-python')
                identity=guard.identity_for_runtime(data_dir=child.data_dir,prediction_config_path=config,
                    code_root=child.release_root,port=8769)
                assert identity.uid==9123 and identity.gid==9124 and identity.manager=='systemd'
                assert identity.interpreter==str(c.python)
                for field,value in (('MainPID','999999'),('ActiveState','active')):
                    original=manager_state[field];manager_state[field]=value
                    with pytest.raises(guard.PredictionStartDenied):
                        guard.arm_for_operator_start(identity,'systemd-root-operation',operation_fd=lock.fileno())
                    assert not child.permit_path.exists()
                    manager_state[field]=original
                guard.arm_for_operator_start(identity,'systemd-root-operation',operation_fd=lock.fileno())
        permit=child.permit_path;before=c.record.read_bytes()
        assert (permit.stat().st_uid,permit.stat().st_gid,stat.S_IMODE(permit.stat().st_mode))==(9123,9124,0o600)
        assert (permit.parent.stat().st_uid,stat.S_IMODE(permit.parent.stat().st_mode))==(9123,0o700)
        owner_path=child.data_dir/'prediction_arbitrage/runtime.lock'
        assert (owner_path.stat().st_uid,owner_path.stat().st_gid)==service
        actor[:]=service
        with (child.data_dir/'prediction_arbitrage/runtime.lock').open('r+') as owner:
            fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for uid,gid in ((9125,9124),(9123,9125),(0,0)):
                actor[:]=uid,gid
                with pytest.raises(guard.PredictionStartDenied):guard.claim_for_start(identity,'wrong-actor')
            actor[:]=service
            unclaimed=permit.read_bytes()
            with monkeypatch.context() as service_process_identity:
                service_process_identity.setattr(sys,'executable','/external/wrong-service-python')
                with pytest.raises(guard.PredictionStartDenied):guard.claim_for_start(identity,'wrong-interpreter')
            assert permit.read_bytes()==unclaimed and c.record.read_bytes()==before
            claim=guard.claim_for_start(identity,'actual-service-owner')
            assert json.loads(permit.read_text())['state']=='CLAIMED'
            claimed_bytes=permit.read_bytes()
            actual_path_lstat=Path.lstat
            # External filesystem facts only: each stable metadata mutation
            # remains uncertainty. The exact claimed permission and actual OS
            # owner survive every rejected public drain attempt.
            for index in (0,1,2,3,4,5,6,8,9):
                calls=[0]
                def changed_metadata(path,*a,**kw):
                    info=actual_path_lstat(path,*a,**kw)
                    if path==c.record:
                        calls[0]+=1
                        if calls[0]>=2:
                            values=list(info);values[index]+=1
                            return os.stat_result(values)
                    return info
                with monkeypatch.context() as stat_boundary:
                    stat_boundary.setattr(Path,'lstat',changed_metadata)
                    with pytest.raises(guard.PredictionStartDenied,match='selection changed during validation'):
                        guard.rearm_after_clean_drain(claim)
                assert permit.read_bytes()==claimed_bytes and c.record.read_bytes()==before
                with owner_path.open('r+') as independent_owner:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(independent_owner.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            def unavailable_metadata(path,*a,**kw):
                if path==c.record:raise OSError('external management metadata unavailable')
                return actual_path_lstat(path,*a,**kw)
            with monkeypatch.context() as stat_boundary:
                stat_boundary.setattr(Path,'lstat',unavailable_metadata)
                with pytest.raises(OSError,match='metadata unavailable'):
                    guard.rearm_after_clean_drain(claim)
            assert permit.read_bytes()==claimed_bytes and c.record.read_bytes()==before
            with owner_path.open('r+') as independent_owner:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(independent_owner.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            calls=[0]
            def read_updates_atime(path,*a,**kw):
                info=actual_path_lstat(path,*a,**kw)
                if path==c.record:
                    calls[0]+=1
                    if calls[0]>=2:
                        values=list(info);values[7]+=1
                        return os.stat_result(values)
                return info
            with monkeypatch.context() as stat_boundary:
                stat_boundary.setattr(Path,'lstat',read_updates_atime)
                guard.rearm_after_clean_drain(claim)
            assert calls[0]>=2
            assert json.loads(permit.read_text())['state']=='ARMED'
            assert c.record.read_bytes()==before
        actor[:]=0,0
        import subprocess
        actual_run=subprocess.run;actual_read_text=Path.read_text
        starts=[]
        class BeforeExternalServiceCreation(RuntimeError):pass
        def kernel_text(path,*a,**kw):
            if str(path)=='/proc/locks':return ''
            return actual_read_text(path,*a,**kw)
        def manager_process(command,*a,**kw):
            args=list(command)
            if args[:2]==['systemctl','show']:
                facts=dict(LoadState='loaded',ActiveState='inactive',SubState='dead',MainPID='0',
                    FragmentPath=str(unit),DropInPaths='',NeedDaemonReload='no',User=c.user,Group=c.user,
                    WorkingDirectory=unit.read_text().split('WorkingDirectory=',1)[1].splitlines()[0],
                    Environment=' '.join(line.removeprefix('Environment=')
                        for line in unit.read_text().splitlines() if line.startswith('Environment=')))
                return subprocess.CompletedProcess(command,0,'\n'.join(k+'='+v for k,v in facts.items()),'')
            if args[0]=='ss':return subprocess.CompletedProcess(command,0,'','')
            if args[0]=='runuser':
                if 'git' in args:return actual_run(args[args.index('git'):],*a,**kw)
                return subprocess.CompletedProcess(command,0,'','')
            if args[0]==str(c.python) and args[1]=='-c':return subprocess.CompletedProcess(command,0,'','')
            if args[:2]==['systemctl','start']:
                armed=json.loads(permit.read_text())
                assert armed['state']=='ARMED' and armed['operation_nonce']!='systemd-root-operation'
                assert load_prediction_runtime_record(c.record)['candidate']['lp_validation']['operation_nonce']==armed['operation_nonce']
                starts.append(armed['operation_nonce'])
                # This external OS boundary deliberately stops before creating
                # an installed service. It cannot fabricate READY/health proof.
                raise BeforeExternalServiceCreation('offline boundary before actual systemd start')
            if args[:2]==['systemd-analyze','verify']:
                assert Path(args[2]).read_text().startswith('[Unit]')
                return subprocess.CompletedProcess(command,0,'','')
            if args[:2]==['systemctl','daemon-reload']:
                return subprocess.CompletedProcess(command,0,'','')
            if args[0]=='systemctl':raise AssertionError('unmodeled disposable systemd command')
            return actual_run(command,*a,**kw)
        with monkeypatch.context() as manager_os:
            manager_os.setattr(Path,'read_text',kernel_text)
            manager_os.setattr(subprocess,'run',manager_process)
            if restart_recovery is not None:
                # Reuse the real files/producer/guard/flocks and external OS
                # view for the accepted explicit absent-restart regression.
                restart_recovery(c,starts,BeforeExternalServiceCreation)
                return
            _assert_systemd_stopped_target_transition(child,c,starts,manager_process,monkeypatch)
            unarmed=permit.read_bytes()
            prepared=cloud.operate(c,'prepare-target',prepare_for='start')
            assert set(prepared)=={'status','operation_nonce'} and prepared['status']=='PREPARED'
            assert type(prepared['operation_nonce']) is str and prepared['operation_nonce']
            assert load_prediction_runtime_record(c.record)['pending_lp_validation']['operation_nonce']==prepared['operation_nonce']
            assert permit.read_bytes()==unarmed and starts==[]
            selected=c.record.read_bytes()
            with pytest.raises(ValueError):cloud.operate(c,'start',operation_nonce='wrong-systemd-forward')
            assert c.record.read_bytes()==selected and permit.read_bytes()==unarmed and starts==[]
            with pytest.raises(BeforeExternalServiceCreation):
                cloud.operate(c,'start',operation_nonce=prepared['operation_nonce'])
            assert starts==[prepared['operation_nonce']]
            with pytest.raises(BeforeExternalServiceCreation):cloud.operate(c,'restart')
            assert len(starts)==2 and len(set(starts))==2
            _assert_systemd_forwarder(child,c,starts,manager_process,monkeypatch)
        actor[:]=service
        # Each mutation is rejected by actual validation, before reliable or
        # SDK startup. No ownership repair or fallback to foreground occurs.
        good=c.record.read_bytes()
        for mode in (0o600,0o660):
            c.record.chmod(mode)
            with pytest.raises(guard.PredictionStartDenied):guard.identity_for_runtime(data_dir=child.data_dir,prediction_config_path=config,code_root=child.release_root,port=8769)
        c.record.chmod(0o640)
        info=actual_lstat(c.record);key=(info.st_dev,info.st_ino)
        for bad in ((9123,9124),(0,9125)):
            owners[key]=bad
            with pytest.raises(guard.PredictionStartDenied):guard.identity_for_runtime(data_dir=child.data_dir,prediction_config_path=config,code_root=child.release_root,port=8769)
        owners[key]=(0,9124)
        foreground.write_bytes(good)
        with pytest.raises(guard.PredictionStartDenied):guard.identity_for_runtime(data_dir=child.data_dir,prediction_config_path=config,code_root=child.release_root,port=8769)
        foreground.unlink()
        wrong=root/'wrong.json';wrong.write_bytes(config.read_bytes());wrong.chmod(0o600)
        with pytest.raises(guard.PredictionStartDenied):guard.identity_for_runtime(data_dir=child.data_dir,prediction_config_path=wrong,code_root=child.release_root,port=8769)
        unit.write_text(unit.read_text()+'\n# changed unit\n')
        with pytest.raises(guard.PredictionStartDenied):guard.identity_for_runtime(data_dir=child.data_dir,prediction_config_path=config,code_root=child.release_root,port=8769)
        actor[:]=0,0
        shadow=cloud.CloudConfig(**{**c.__dict__,'runtime_root':child.root/'shadow-runtime',
            'mode':'shadow','region':'','secret':'','version':'','role':''})
        cloud.record(shadow,'stopped')
        legacy=load_prediction_runtime_record(shadow.record)
        assert stat.S_IMODE(shadow.record.stat().st_mode)==0o600
        assert 'service_identity' not in legacy and 'manager_identity' not in legacy
        assert legacy['candidate']==dict(checkout=str(child.release_root),git_sha=child.sha)


def _assert_systemd_forwarder(child,c,starts,manager_process,monkeypatch):
    """Actual thin/native entrypoints; OS process execution stays disposable."""
    import contextlib,importlib.util,io,os,subprocess,sys
    from pathlib import Path
    from open_trader import prediction_cloud as cloud
    spec=importlib.util.spec_from_file_location('actual_systemd_deploy_release',
        child.release_root/'scripts/deploy_release.py')
    wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
    config=child.root/'cloud-private.json'
    config.write_text(json.dumps({k:str(v) if isinstance(v,Path) else v for k,v in c.__dict__.items()}))
    config.chmod(0o600)
    fake=child.root/'root-external-github';fake.mkdir()
    gh_called=child.marker('unexpected-systemd-forward-CI-request')
    gh=fake/'gh';gh.write_text('#!'+sys.executable+'\n'+
        "from pathlib import Path\nPath("+repr(str(gh_called))+").touch()\nraise SystemExit(1)\n")
    gh.chmod(0o700);calls=[];before=list(starts)
    def external(command,*a,**kw):
        command=list(command)
        if command[:2]==['bash',str(child.release_root/'scripts/prediction-systemd.sh')]:
            calls.append(command)
            # The actual bash bridge only execs this native entry. Model its
            # root OS process boundary here so stat/UID/systemd stay isolated.
            output=io.StringIO()
            with contextlib.redirect_stdout(output):status=cloud.main(command[2:])
            return subprocess.CompletedProcess(command,status,output.getvalue(),'')
        env={**os.environ,**(kw.pop('env',None) or {}),'PATH':str(fake)+os.pathsep+os.environ['PATH']}
        return manager_process(command,*a,env=env,**kw)
    with monkeypatch.context() as boundary:
        boundary.setattr(subprocess,'run',external)
        result=wrapper.main(['--expected-sha',child.sha,'--release-root',str(child.release_root),
            '--runtime-root',str(child.runtime_root),'--python',sys.executable,
            'prediction-systemd','--action','start','--config',str(config)])
    assert result==2,'actual native entry must preserve blocked pre-service-creation result'
    assert not gh_called.exists(),'systemd retained producer must not request fresh forward CI'
    assert len(calls)==2 and calls[0][2]=='prepare-target' and '--prepare-for' in calls[0]
    forwarded=calls[1][calls[1].index('--operation-nonce')+1]
    assert starts==[*before,forwarded] and forwarded not in before
    assert child.actual_posts()==0
    child.assert_owner_available()


def _assert_systemd_stopped_target_transition(child,c,starts,manager_process,monkeypatch):
    import contextlib,dataclasses,importlib.util,io,shutil,subprocess,sys
    from pathlib import Path
    from open_trader import prediction_cloud as cloud
    from open_trader.prediction_release import load_prediction_runtime_record
    from tests.lp_d2_service_harness import offline_github_fixture
    assert cloud.operate(c,'stop')['status']=='STOPPED'
    before_permit=child.permit_path.read_bytes();before_starts=list(starts)
    target=child.root/'next-compatible-release'
    capability=target/'ops/lp-runtime-contract.json'
    capability.write_bytes(capability.read_bytes()+b'\n')
    # Explicitly authorized disposable fixture Git; never the task worktree.
    subprocess.run(['git','-C',str(target),'add','ops/lp-runtime-contract.json'],check=True,capture_output=True)
    subprocess.run(['git','-C',str(target),'-c','user.name=D2 fixture','-c','user.email=d2@invalid',
        'commit','-qm','next compatible disposable release'],check=True,capture_output=True)
    sha=subprocess.check_output(['git','-C',str(target),'rev-parse','HEAD'],text=True).strip()
    selected=dataclasses.replace(c,release_root=target,expected_sha=sha)
    api=offline_github_fixture(target,child.root/'next-source-github')
    captures=[]
    def external(command,*a,**kw):
        command=list(command)
        if len(command)>1 and command[1]==str(target/'scripts/deployment_preflight.py'):
            captures.append(command)
            spec=importlib.util.spec_from_file_location('actual_next_systemd_verifier',command[1])
            verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
            verifier.GitHub=lambda:api
            out,err=io.StringIO(),io.StringIO()
            with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):status=verifier.main(command[2:])
            if kw.get('check') and status:raise subprocess.CalledProcessError(status,command,out.getvalue(),err.getvalue())
            return subprocess.CompletedProcess(command,status,out.getvalue(),err.getvalue())
        if len(command)>1 and command[1]==str(c.release_root/'scripts/deployment_preflight.py'):
            raise AssertionError('compatible retained previous release requested expired CI')
        return manager_process(command,*a,**kw)
    with monkeypatch.context() as boundary:
        boundary.setattr(subprocess,'run',external)
        assert cloud.operate(selected,'install')['status']=='INSTALLED_STOPPED'
        installed=load_prediction_runtime_record(c.record)
        assert installed['candidate']['git_sha']==sha
        assert installed['candidate']['lp_validation']['source_sha']==sha
        assert installed['previous_release']['lp_validation']['source_sha']==c.expected_sha
        assert installed.get('pending_lp_validation') is None
        assert child.permit_path.read_bytes()==before_permit and starts==before_starts
        assert cloud.UNIT_PATH.read_text()==cloud.render_unit(selected)
        assert cloud.operate(c,'install')['status']=='INSTALLED_STOPPED'
    restored=load_prediction_runtime_record(c.record)
    assert restored['candidate']['lp_validation']['source_sha']==c.expected_sha
    assert restored['previous_release']['lp_validation']['source_sha']==sha
    assert restored.get('pending_lp_validation') is None
    assert len(captures)==1 and starts==before_starts
    assert child.permit_path.read_bytes()==before_permit and child.actual_posts()==0
    assert cloud.UNIT_PATH.read_text()==cloud.render_unit(c)
    child.assert_owner_available()
