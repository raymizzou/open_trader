"""LP410 public manager regressions with isolated OS/GitHub observations."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from tests.test_prediction_release_launchd import ReleaseCheckout, ReleaseHarness


def tracked_custom(harness):
    checkout = harness.candidate
    manifest = checkout.path / 'nested/custom/release.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(harness.manifest.read_bytes())
    subprocess.run(['git', '-C', str(checkout.path), 'add', str(manifest)], check=True)
    subprocess.run(['git', '-C', str(checkout.path), '-c', 'user.name=test', '-c',
                    'user.email=test@example.com', 'commit', '-qm', 'custom manifest'], check=True)
    sha = subprocess.check_output(['git', '-C', str(checkout.path), 'rev-parse', 'HEAD'], text=True).strip()
    harness.candidate = ReleaseCheckout(checkout.path, sha, checkout.reader_generation, checkout.contract_generation)
    return manifest


@pytest.mark.parametrize('custom', [False, True])
def test_installer_retains_verified_manifest_after_ready_and_refresh(tmp_path, monkeypatch, custom):
    harness = ReleaseHarness(tmp_path)
    manifest = tracked_custom(harness) if custom else harness.manifest
    manifest_bytes = manifest.read_bytes()
    # Capture what the real installer publishes at the external start boundary,
    # before health/READY can repair or conceal an incomplete self-context.
    dispatcher = harness.fake_bin/'fake-command'
    dispatcher.write_text(dispatcher.read_text().replace('    if action == "bootstrap":', """    if action == "bootstrap":
        state['manifest_at_start'] = json.loads(Path(os.environ['FAKE_RECORD_PATH']).read_text())['candidate'].get('manifest')
"""))
    result = harness.install(release_manifest=manifest)
    assert result.returncode == 0, result.stderr
    assert harness.runtime_record['candidate']['manifest'] == str(manifest.resolve())
    assert harness.state['manifest_at_start'] == str(manifest.resolve())
    assert manifest.read_bytes() == manifest_bytes
    # An old READY record with the historical omission must refresh from the
    # independently loaded manifest, without restarting or issuing permission.
    record_path=harness.runtime_root/'prediction-service-runtime.json'
    legacy=json.loads(record_path.read_text());legacy['candidate'].pop('manifest')
    record_path.write_text(json.dumps(legacy))
    permit_before=(harness.runtime_root/'lp-start-guard/permit.json').read_bytes()
    harness.calls.clear()
    legacy_refresh=harness.install(release_manifest=manifest)
    assert legacy_refresh.returncode==0,legacy_refresh.stderr
    assert harness.runtime_record['candidate']['manifest']==str(manifest.resolve())
    assert (harness.runtime_root/'lp-start-guard/permit.json').read_bytes()==permit_before
    assert_no_mutation(harness)
    harness.mutate_live_restart()
    refreshed = harness.install(release_manifest=manifest)
    assert refreshed.returncode == 0, refreshed.stderr
    assert harness.runtime_record['candidate']['manifest'] == str(manifest.resolve())
    assert harness.runtime_record['candidate']['git_sha'] == harness.sha
    if custom:
        # Source identity comparisons intentionally ignore the manifest path.
        # A no-restart refresh must retain the actually loaded tracked path,
        # even when the caller supplies the same release's default manifest.
        permit_before=(harness.runtime_root/'lp-start-guard/permit.json').read_bytes()
        harness.calls.clear()
        compatible_refresh=harness.install()
        assert compatible_refresh.returncode==0,compatible_refresh.stderr
        assert harness.runtime_record['candidate']['manifest']==str(manifest.resolve())
        assert (harness.runtime_root/'lp-start-guard/permit.json').read_bytes()==permit_before
        assert_no_mutation(harness)

    # Real current-process flock; only external Darwin observations are modeled.
    from open_trader.prediction_start_guard import identity_for_runtime, verify_launchd_self_context
    identity = identity_for_runtime(data_dir=harness.runtime_root/'data',
        prediction_config_path=harness.config, code_root=harness.candidate.path)
    notifier = harness.runtime_root/'config/daily_premarket.env'
    notifier.parent.mkdir(exist_ok=True)
    notifier.write_text(''); notifier.chmod(0o600)
    args = harness.state['arguments']
    lock = harness.runtime_root/'data/prediction_arbitrage/runtime.lock'
    native_run = subprocess.run
    wrong = {'pid': False, 'cwd': False}
    def external(command, *pos, **kw):
        if command[0] == '/bin/launchctl':
            pid = os.getpid()+1 if wrong['pid'] else os.getpid()
            text = f'path = {harness.plist}\nworking directory = {identity.code_root}\npid = {pid}\narguments = {{\n'+'\n'.join(args)+'\n}\n'
        elif command[0] == '/bin/ps':
            text = f'{os.getpid()} {os.getuid()} {shlex.join(args)}\n'
        elif command[0] == '/usr/sbin/lsof':
            if 'cwd' in command:
                text = 'n'+('/wrong' if wrong['cwd'] else identity.code_root)+'\n'
            else:
                text = f'p{os.getpid()}\nn{lock}\n'
        else:
            return native_run(command, *pos, **kw)
        return subprocess.CompletedProcess(command, 0, text, '')
    with lock.open('a+') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX|fcntl.LOCK_NB)
        monkeypatch.setattr(sys, 'platform', 'darwin')
        monkeypatch.setattr(subprocess, 'run', external)
        monkeypatch.chdir(harness.candidate.path)
        assert verify_launchd_self_context(identity)
        wrong['pid'] = True
        assert not verify_launchd_self_context(identity)
        wrong.update(pid=False, cwd=True)
        assert not verify_launchd_self_context(identity)


def failed_service(tmp_path, *, legacy=False):
    harness = ReleaseHarness(tmp_path)
    installed = harness.install()
    assert installed.returncode == 0, installed.stderr
    # Claim through the public service boundary under an actual child-held lock.
    child = subprocess.run([sys.executable, '-B', '-c', '''
import fcntl,sys
from pathlib import Path
from open_trader.prediction_start_guard import identity_for_runtime,claim_for_start
root,config,code=sys.argv[1:]
identity=identity_for_runtime(data_dir=Path(root)/'data',prediction_config_path=config,code_root=code)
with (Path(root)/'data/prediction_arbitrage/runtime.lock').open('a+') as owner:
 fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
 claim_for_start(identity,'fixture-startup-epoch')
''', str(harness.runtime_root), str(harness.config), str(harness.candidate.path)],
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]/'src')},
        capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stderr
    state = harness.state
    state.update(pid=0, process_cwd='', listener=False, owner_available=True,
                 health={}, lock_owner_pids=[], lock_owner_pid=0)
    harness.state_path.write_text(json.dumps(state))
    if legacy:
        record_path=harness.runtime_root/'prediction-service-runtime.json'
        record=json.loads(record_path.read_text())
        record['candidate'].pop('manifest')
        record_path.write_text(json.dumps(record))
    install_process_inventory(harness)
    harness.calls.clear()
    return harness


def protected_bytes(harness):
    return {str(path): path.read_bytes() for path in (
        harness.runtime_root/'prediction-service-runtime.json',
        harness.runtime_root/'lp-start-guard/permit.json', harness.plist,
        harness.config, harness.runtime_root/'data/prediction_arbitrage/runtime.lock')}


def assert_no_mutation(harness):
    assert not any(' bootout ' in f' {call} ' or ' bootstrap ' in f' {call} '
                   for call in harness.calls.all())


def use_native_lock_observation(harness):
    # Read only this disposable lock. Darwin uses real lsof; the portable OS
    # boundary models its parent observer while independently checking the
    # actual kernel flock. No host service or business path is inspected.
    dispatcher=harness.fake_bin/'fake-command'
    dispatcher.write_text(dispatcher.read_text().replace('if command == "lsof":', """if command == "lsof" and any("runtime.lock" in item for item in sys.argv):
    if Path("/usr/sbin/lsof").is_file():
        os.execv("/usr/sbin/lsof", ["/usr/sbin/lsof", *sys.argv[1:]])
    import fcntl
    lock=Path(os.environ['FAKE_DATA_DIR'])/'prediction_arbitrage/runtime.lock'
    with lock.open('rb') as probe:
        try:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('p'+str(os.getppid())+'\\nf3\\nn'+str(lock))
            raise SystemExit(0)
    raise SystemExit(1)
if command == "lsof":"""))


@pytest.mark.parametrize('legacy', [False, True])
def test_failed_loaded_preflight_is_read_only_recoverable(tmp_path, legacy):
    harness = failed_service(tmp_path, legacy=legacy)
    if legacy:
        use_native_lock_observation(harness)
    before = protected_bytes(harness)
    result = harness.install(preflight=True, owner_probe=False)
    assert result.returncode == 0, result.stdout+result.stderr
    report = json.loads(result.stdout)
    assert report['status'] == 'RECOVERABLE'
    assert 'failed' in report['reason']
    assert protected_bytes(harness) == before
    assert_no_mutation(harness)


def manager_command(harness, *flags):
    return [str(harness.candidate.path/'scripts/install_prediction_service_launchd.sh'),
        '--mode', 'production', '--repo-root', str(harness.candidate.path),
        '--runtime-root', str(harness.runtime_root), '--python', sys.executable,
        '--config', str(harness.config), '--launch-agents-dir', str(harness.agents),
        '--wait-seconds', '1', *flags]


def run_manager(harness, *flags):
    from timing_support import supervised_process
    return supervised_process(manager_command(harness, *flags), timeout=45,
        combine_stderr=False, env=harness._env(harness.candidate))


def startup_process_observation(harness):
    dispatcher = harness.fake_bin/'fake-command'
    source = dispatcher.read_text()
    source = source.replace('if command == "ps":\n', '''if command == "ps" and "-o" in sys.argv:
    view = observation_state()
    if str(view["pid"]) not in sys.argv: raise SystemExit(1)
    import shlex
    print(str(view["pid"])+" "+str(os.getuid())+" "+shlex.join(view["arguments"]))
    raise SystemExit(0)

if command == "ps":
''')
    dispatcher.write_text(source)
    state = harness.state
    state.update(pid=5252, process_cwd=str(harness.candidate.path))
    harness.state_path.write_text(json.dumps(state))


@pytest.mark.parametrize('phase', ['exited', 'startup_exits', 'forwarded', 'native_owner'])
def test_explicit_restart_recovers_only_verified_failed_loaded_service(tmp_path, phase):
    import hashlib
    import plistlib
    import time
    from datetime import datetime, UTC
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    harness = failed_service(tmp_path, legacy=phase=='forwarded')
    permit_path = harness.runtime_root/'lp-start-guard/permit.json'
    old = json.loads(permit_path.read_text())
    if phase=='native_owner':use_native_lock_observation(harness)
    account = hashlib.sha256(('0x'+'a'*40).encode()).hexdigest()
    store = PredictionArbitrageStore(harness.runtime_root/'data')
    store.lp_save_current_control(dict(account_id=account,process_epoch='operator',request_id='pause',
        request_version=1,desired_running=False,requested_at=datetime(2026,10,16,12,tzinfo=UTC),actor='operator'))
    saved = store.lp_load_current_control(account)
    data_before = harness.database.read_bytes()
    payload = plistlib.loads(harness.plist.read_bytes())
    payload['EnvironmentVariables']['OPEN_TRADER_NLEG_PAUSED'] = '1'
    harness.plist.write_bytes(plistlib.dumps(payload))
    if phase == 'startup_exits':
        startup_process_observation(harness)
        marker, release = tmp_path/'observed', tmp_path/'continue'
        state = harness.state
        state.update(print_barrier_count=3, print_barrier_marker=str(marker), print_barrier_release=str(release))
        harness.state_path.write_text(json.dumps(state))
        process = subprocess.Popen(manager_command(harness, '--restart'),
            env=harness._env(harness.candidate), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline=time.monotonic()+30
            while not marker.exists() and process.poll() is None and time.monotonic()<deadline:
                time.sleep(.01)
            if marker.exists():
                state=harness.state; state.update(pid=0,process_cwd='')
                harness.state_path.write_text(json.dumps(state)); release.touch()
            out,err=process.communicate(timeout=15)
            result=subprocess.CompletedProcess(process.args,process.returncode,out,err)
            assert marker.exists(), result.stdout+result.stderr
        finally:
            release.touch()
            if process.poll() is None:process.terminate();process.wait(timeout=5)
    elif phase == 'forwarded':
        prepared=run_manager(harness,'--restart','--prepare-target')
        assert prepared.returncode==0,prepared.stdout+prepared.stderr
        nonce=json.loads(prepared.stdout)['operation_nonce']
        assert json.loads(permit_path.read_text())==old
        assert_no_mutation(harness)
        result=run_manager(harness,'--restart','--operation-nonce',nonce)
    else:
        result = run_manager(harness, '--restart')
    assert result.returncode == 0, result.stdout+result.stderr
    new=json.loads(permit_path.read_text())
    assert new['operation_nonce'] != old['operation_nonce'] and new['state']=='ARMED'
    assert harness.runtime_record['state']=='ready'
    assert harness.runtime_record['ready']['pid']==4242
    assert harness.state['bootout_pids']==[0]
    mutations=[call for call in harness.calls.all() if call.startswith('launchctl bootout') or call.startswith('launchctl bootstrap')]
    assert mutations==[f'launchctl bootout gui/{os.getuid()}/com.open-trader.prediction-service',
                       f'launchctl bootstrap gui/{os.getuid()} {harness.plist}']
    assert harness.database.read_bytes()==data_before
    assert store.lp_load_current_control(account)==saved and saved['desired_running'] is False
    assert plistlib.loads(harness.plist.read_bytes())['EnvironmentVariables']['OPEN_TRADER_NLEG_PAUSED']=='1'


NEGATIVE_VARIANTS = [
    'missing_record', 'malformed_record', 'record_permissions', 'missing_permit',
    'malformed_permit', 'permit_permissions', 'permit_nonce', 'wrong_plist',
    'wrong_argv', 'wrong_config', 'dirty_source', 'unexpected_pid', 'listener',
    'held_owner', 'owner_inspection_unknown', 'claimed_pid_reused',
    'before_stop_record', 'before_stop_identity', 'before_stop_config',
    'before_arm_record', 'before_arm_identity', 'before_arm_config',
]


def run_at_print_barrier(harness, count, mutation):
    import time
    marker,release=harness.root/'race-observed',harness.root/'race-release'
    state=harness.state
    state.update(print_barrier_count=count,print_barrier_marker=str(marker),print_barrier_release=str(release))
    harness.state_path.write_text(json.dumps(state))
    process=subprocess.Popen(manager_command(harness,'--restart'),env=harness._env(harness.candidate),
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    try:
        deadline=time.monotonic()+30
        while not marker.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
        assert marker.exists(), 'manager did not reach controlled external observation'
        mutation();release.touch()
        out,err=process.communicate(timeout=15)
        return subprocess.CompletedProcess(process.args,process.returncode,out,err)
    finally:
        release.touch()
        if process.poll() is None:
            process.terminate();process.wait(timeout=5)


@pytest.mark.parametrize('variant', NEGATIVE_VARIANTS)
def test_failed_loaded_recovery_rejects_uncertain_or_changed_identity(tmp_path, variant):
    from contextlib import ExitStack
    harness=failed_service(tmp_path)
    permit=harness.runtime_root/'lp-start-guard/permit.json'
    record=harness.runtime_root/'prediction-service-runtime.json'
    original_permit=permit.read_bytes()
    original_plist=harness.plist.read_bytes()
    state=harness.state
    with ExitStack() as cleanup:
        if variant=='missing_record':record.unlink()
        elif variant=='malformed_record':record.write_text('{')
        elif variant=='record_permissions':record.chmod(0o644)
        elif variant=='missing_permit':permit.unlink()
        elif variant=='malformed_permit':permit.write_text('{')
        elif variant=='permit_permissions':permit.chmod(0o644)
        elif variant=='permit_nonce':
            value=json.loads(permit.read_text());value['operation_nonce']='unselected';permit.write_text(json.dumps(value))
        elif variant=='wrong_plist':state['plist']=str(tmp_path/'unrelated.plist')
        elif variant=='wrong_argv':state['arguments']=[*state['arguments'],'--config',str(tmp_path/'other.json')]
        elif variant=='wrong_config':harness.config.write_bytes(harness.config.read_bytes()+b'\n')
        elif variant=='dirty_source':(harness.candidate.path/'unexpected').write_text('dirty')
        elif variant=='unexpected_pid':state.update(pid=9999,process_cwd='/unknown')
        elif variant=='listener':state.update(pid=9999,listener=True)
        elif variant=='held_owner':
            owner=cleanup.enter_context((harness.runtime_root/'data/prediction_arbitrage/runtime.lock').open('rb'))
            fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        elif variant=='owner_inspection_unknown':
            dispatcher=harness.fake_bin/'fake-command'
            dispatcher.write_text(dispatcher.read_text().replace('if command == "lsof":', '''if command == "lsof" and any("runtime.lock" in item for item in sys.argv):
    print("inspection unavailable",file=sys.stderr)
    raise SystemExit(2)
if command == "lsof":'''))
        elif variant=='claimed_pid_reused':state['pid']=json.loads(original_permit)['pid']
        harness.state_path.write_text(json.dumps(state))
        if variant.startswith('before_'):
            def mutate():
                kind=variant.rsplit('_',1)[1]
                if kind=='record':
                    value=json.loads(record.read_text());value['external_change']='retained'
                    record.write_text(json.dumps(value))
                elif kind=='config':harness.config.write_bytes(harness.config.read_bytes()+b'\n')
                else:
                    value=harness.state;value['plist']=str(tmp_path/'unrelated.plist')
                    # At pre-arm the label is already absent; change the actual
                    # retained plist so static identity must still reject it.
                    if 'arm' in variant:
                        import plistlib
                        payload=plistlib.loads(harness.plist.read_bytes());payload['WorkingDirectory']='/changed'
                        harness.plist.write_bytes(plistlib.dumps(payload))
                    harness.state_path.write_text(json.dumps(value))
            result=run_at_print_barrier(harness,5 if 'arm' in variant else 3,mutate)
            assert result.returncode!=0, result.stdout+result.stderr
            assert permit.read_bytes()==original_permit
            assert not any(call.startswith('launchctl bootstrap') for call in harness.calls.all())
            if 'stop' in variant:
                assert_no_mutation(harness)
                assert harness.plist.read_bytes()==original_plist
            if variant.endswith('record'):assert json.loads(record.read_text())['external_change']=='retained'
        else:
            paths=[path for path in (record,permit,harness.plist,harness.config) if path.exists()]
            before={path:path.read_bytes() for path in paths}
            preflight=harness.install(preflight=True,owner_probe=False)
            assert preflight.returncode!=0,preflight.stdout+preflight.stderr
            assert json.loads(preflight.stdout)['status']=='BLOCKED'
            restarted=run_manager(harness,'--restart')
            assert restarted.returncode!=0,restarted.stdout+restarted.stderr
            assert {path:path.read_bytes() for path in paths}==before
            assert_no_mutation(harness)


@pytest.mark.parametrize('resource', ['process', 'startup_appears', 'listener', 'native_owner', 'inspection_unknown', 'released'])
def test_failed_restart_never_rearms_before_complete_teardown(tmp_path, resource):
    from contextlib import ExitStack
    harness=failed_service(tmp_path)
    permit=harness.runtime_root/'lp-start-guard/permit.json'
    original=permit.read_bytes()
    if resource in ('process','startup_appears'):
        startup_process_observation(harness)
        state=harness.state;state['case']='pid_still_present'
        if resource=='startup_appears':state['pid']=0
        harness.state_path.write_text(json.dumps(state))
    with ExitStack() as cleanup:
        def hold_teardown():
            # This is the observed label-absence boundary after bootout. While
            # it is held, the consumed permit and no-bootstrap facts are literal.
            assert permit.read_bytes()==original
            assert not any(call.startswith('launchctl bootstrap') for call in harness.calls.all())
            state=harness.state
            if resource=='startup_appears':
                assert not state.get('bootout_seen')
                state.update(pid=5252,process_cwd=str(harness.candidate.path))
            else:
                assert state['bootout_seen'] is True
            if resource=='listener':state.update(listener=True,pid=5252)
            elif resource=='inspection_unknown':state['listener_inspection_error_after_bootout']=True
            elif resource in ('native_owner','released'):
                owner=cleanup.enter_context((harness.runtime_root/'data/prediction_arbitrage/runtime.lock').open('rb'))
                fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
                assert permit.read_bytes()==original
                if resource=='released':fcntl.flock(owner,fcntl.LOCK_UN)
            harness.state_path.write_text(json.dumps(state))
        result=run_at_print_barrier(harness,2 if resource=='startup_appears' else 4,hold_teardown)
        if resource=='released':
            assert result.returncode==0,result.stdout+result.stderr
            assert json.loads(permit.read_text())['operation_nonce']!=json.loads(original)['operation_nonce']
            assert len([call for call in harness.calls.all() if call.startswith('launchctl bootstrap')])==1
        else:
            assert result.returncode!=0,result.stdout+result.stderr
            assert permit.read_bytes()==original
            assert not any(call.startswith('launchctl bootstrap') for call in harness.calls.all())
            assert harness.runtime_record['state']=='failed'
            assert harness.runtime_record['failure_reason'] in {
                'candidate_cleanup_not_proven','failed_managed_evidence_changed_before_arm'}
        assert not any('kill ' in call for call in harness.calls.all())



def install_process_inventory(harness):
    """Model only the external whole-process observation for failed recovery."""
    dispatcher = harness.fake_bin/'fake-command'
    dispatcher.write_text(dispatcher.read_text().replace('if command == "ps":', r'''if command == "ps" and "-axww" in sys.argv:
    import shlex
    assert state.get('bootout_seen') and not state['loaded']
    if state.get('late_variant') == 'exited':
        Path(state['inventory_marker']).touch()
        deadline=time.monotonic()+20
        while not Path(state['inventory_release']).exists():
            if time.monotonic()>deadline: raise RuntimeError('inventory watchdog')
            time.sleep(.01)
        state=json.loads(state_path.read_text())
    state.setdefault('process_inventory', []).append({'late_alive':state.get('late_alive',False),
        'permit':json.loads((Path(os.environ['FAKE_DATA_DIR']).parent/'lp-start-guard/permit.json').read_text())})
    save()
    if state.get('late_variant') == 'inspection_unknown':
        print('unavailable process inventory',file=sys.stderr)
        raise SystemExit(2)
    print('1 0 /sbin/launchd')
    if state.get('late_alive'):
        print('6363 '+str(os.getuid())+' '+shlex.join(state['late_arguments']))
    raise SystemExit(0)
if command == "ps":'''))


@pytest.mark.parametrize('variant', ['survives', 'exited', 'inspection_unknown'])
def test_failed_restart_checks_process_started_after_final_observation(tmp_path, variant):
    import hashlib
    import time
    from datetime import datetime, UTC
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    harness=failed_service(tmp_path)
    permit=harness.runtime_root/'lp-start-guard/permit.json'
    original=permit.read_bytes()
    account=hashlib.sha256(('0x'+'a'*40).encode()).hexdigest()
    store=PredictionArbitrageStore(harness.runtime_root/'data')
    store.lp_save_current_control(dict(account_id=account,process_epoch='operator',request_id='pause',
        request_version=1,desired_running=False,requested_at=datetime(2026,10,16,12,tzinfo=UTC),actor='operator'))
    saved=store.lp_load_current_control(account)
    data_before=harness.database.read_bytes()
    boot_marker,boot_release=tmp_path/'bootout-entered',tmp_path/'bootout-continue'
    inventory_marker,inventory_release=tmp_path/'inventory-entered',tmp_path/'inventory-continue'
    state=harness.state
    # Independent literal startup arguments and PID (6363), distinct from the
    # removed label's PID, listener, and native owner facts.
    late_arguments=[sys.executable,'-m','open_trader','prediction-service',
        '--mode','production','--data-dir',str(harness.runtime_root/'data'),
        '--config',str(harness.config),'--host','127.0.0.1','--port','8769',
        '--notifier-config',str(harness.runtime_root/'config/daily_premarket.env'),
        '--release-manifest',str(harness.manifest)]
    assert state['arguments']==late_arguments
    state.update(late_variant=variant,late_arguments=late_arguments,late_alive=False,
        boot_marker=str(boot_marker),boot_release=str(boot_release),
        inventory_marker=str(inventory_marker),inventory_release=str(inventory_release))
    harness.state_path.write_text(json.dumps(state))
    dispatcher=harness.fake_bin/'fake-command'
    dispatcher.write_text(dispatcher.read_text().replace('    if action == "bootout":', r'''    if action == "bootout":
        # This external boundary is strictly after every pre-stop observation.
        assert state['pid']==0 and not state['listener'] and not state['lock_owner_pids']
        Path(state['boot_marker']).touch()
        deadline=time.monotonic()+20
        while not Path(state['boot_release']).exists():
            if time.monotonic()>deadline: raise RuntimeError('bootout watchdog')
            time.sleep(.01)
        state=json.loads(state_path.read_text())
        assert state['late_alive']
''').replace('    if action == "bootstrap":', '''    if action == "bootstrap":
        state['at_bootstrap']={'late_alive':state.get('late_alive'),
            'inventories':state.get('process_inventory',[]),
            'permit':json.loads((Path(os.environ['FAKE_DATA_DIR']).parent/'lp-start-guard/permit.json').read_text())}
        save()
'''))
    process=subprocess.Popen(manager_command(harness,'--restart'),env=harness._env(harness.candidate),
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    try:
        deadline=time.monotonic()+30
        while not boot_marker.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
        assert boot_marker.exists(),'manager did not reach bootout boundary'
        assert permit.read_bytes()==original
        assert not any(call.startswith('launchctl bootstrap') for call in harness.calls.all())
        state=harness.state
        state['late_alive']=True
        harness.state_path.write_text(json.dumps(state))
        boot_release.touch()
        if variant=='exited':
            while not inventory_marker.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
            assert inventory_marker.exists(),'late process never observed after label removal'
            assert permit.read_bytes()==original
            state=harness.state
            assert state['late_alive'] and not state['loaded'] and not state['listener'] and not state['lock_owner_pids']
            state['late_alive']=False
            harness.state_path.write_text(json.dumps(state));inventory_release.touch()
        out,err=process.communicate(timeout=15)
        result=subprocess.CompletedProcess(process.args,process.returncode,out,err)
    finally:
        boot_release.touch();inventory_release.touch()
        if process.poll() is None:process.terminate();process.wait(timeout=5)
    bootstrap=[call for call in harness.calls.all() if call.startswith('launchctl bootstrap')]
    if variant=='exited':
        assert result.returncode==0,result.stdout+result.stderr
        assert len(bootstrap)==1
        assert json.loads(permit.read_text())['operation_nonce']!=json.loads(original)['operation_nonce']
        assert harness.runtime_record['state']=='ready'
        observed=harness.state['at_bootstrap']
        assert observed['late_alive'] is False
        assert observed['inventories'] and observed['inventories'][-1]['late_alive'] is False
        assert observed['inventories'][-1]['permit']==json.loads(original)
        assert observed['permit']['operation_nonce']==json.loads(permit.read_text())['operation_nonce']
    else:
        assert result.returncode!=0,result.stdout+result.stderr
        assert permit.read_bytes()==original
        assert not bootstrap
        assert harness.runtime_record['state']=='failed'
        assert harness.runtime_record['failure_reason']=='failed_managed_evidence_changed_before_arm'
        assert harness.state['late_alive'] is True
    assert not any('kill ' in call for call in harness.calls.all())
    assert harness.database.read_bytes()==data_before
    assert store.lp_load_current_control(account)==saved
