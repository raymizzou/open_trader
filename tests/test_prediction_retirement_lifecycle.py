"""Offline public operational consumers of permanent N-leg retirement."""
from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
from types import SimpleNamespace

import pytest


def cloud_host_fixture(tmp_path, monkeypatch, mode, requested):
    """Literal host/process/HTTP evidence; actual cloud identity owners execute."""
    import open_trader.prediction_cloud as cloud
    release, runtime = tmp_path/'release', tmp_path/'runtime'
    (release/'ops').mkdir(parents=True)
    (release/'src').mkdir()
    (release/'ops/prediction-service-release.json').write_text(json.dumps(dict(
        schema_version='open_trader.prediction_service.release.v1', reader_generation=2, contract_generation=2)))
    (runtime/'config').mkdir(parents=True)
    (runtime/'data/prediction_arbitrage').mkdir(parents=True)
    (runtime/'config/prediction_arbitrage.json').write_text('{}')
    (runtime/'config/prediction_arbitrage.json').chmod(0o600)
    (runtime/'data/prediction_arbitrage/runtime.lock').touch()
    python = tmp_path/'python'; python.touch()
    data = dict(release_root=str(release), runtime_root=str(runtime), python=str(python),
        user='prediction', expected_sha='a'*40, mode=mode, n_leg_paused=requested,
        region='region', secret='wallet', version='v1', role='reader')
    config_path = tmp_path/'cloud.json'; config_path.write_text(json.dumps(data))
    cfg = cloud.load_config(config_path)
    unit = tmp_path/'prediction.service'; unit.write_text(cloud.render_unit(cfg))
    monkeypatch.setattr(cloud, 'UNIT_PATH', unit)  # External installed-unit path only.
    pid, uid, gid = 12345, 1001, 1001
    metadata = dict(manager='systemd', manager_identity=str(unit), runtime_root=str(runtime),
        config=str(runtime/'config/prediction_arbitrage.json'), python=str(python),
        service_identity=dict(user='prediction', uid=uid, gid=gid))
    cfg.record.write_text(json.dumps(dict(schema_version='open_trader.prediction_service.runtime.v1',
        state='ready', candidate=dict(checkout=str(release), git_sha='a'*40), unit_text=unit.read_text(), **metadata)))
    cfg.record.chmod(0o640)
    for directory in (runtime, runtime/'config', runtime/'data', runtime/'data/prediction_arbitrage'):
        directory.chmod(0o700)
    monkeypatch.setattr(cloud.pwd, 'getpwnam', lambda user: SimpleNamespace(pw_uid=uid, pw_gid=gid))
    monkeypatch.setattr(cloud.grp, 'getgrnam', lambda user: SimpleNamespace(gr_gid=gid))
    native_stat, native_lstat = Path.stat, Path.lstat
    def account_view(path, original):
        fields = list(original)
        fields[4], fields[5] = (0, gid) if path == cfg.record else ((uid, gid) if path == runtime or path.is_relative_to(runtime) else (0, 0))
        return os.stat_result(fields)
    monkeypatch.setattr(Path, 'stat', lambda path, *a, **kw: account_view(path, native_stat(path, *a, **kw)))
    monkeypatch.setattr(Path, 'lstat', lambda path, *a, **kw: account_view(path, native_lstat(path, *a, **kw)))
    native_resolve = Path.resolve
    monkeypatch.setattr(Path, 'resolve', lambda path, *a, **kw: release if str(path)==f'/proc/{pid}/cwd' else native_resolve(path,*a,**kw))
    native_text, native_bytes = Path.read_text, Path.read_bytes
    # Freeze external evidence at deployment; callers cannot rewrite observed identity.
    deployed_unit = unit.read_text()
    state = dict(active=False, health=dict(
        module='prediction_service', schema_version='open_trader.prediction_service.health.v1',
        pid=pid, git_sha='a'*40, source_state='clean', cwd=str(release), code_root=str(release/'src'),
        status='running', mode=mode, production_owner=mode=='production', mutations='enabled' if mode=='production' else 'prohibited',
        first_violation=None, started_at='process-start', release_schema_version='open_trader.prediction_service.release.v1',
        reader_generation=2, contract_generation=2, n_leg=dict(status='paused',code='N_LEG_PAUSED')),
        lp=dict(state='ready', authenticated=True, stale=False, checked_at=datetime.now(UTC).isoformat(),
                orders=[], positions=[], recommendations=[], market_rewards={}, preparation=None))
    state.update(loaded_unit=deployed_unit, process_unit=deployed_unit)
    lock_info = native_stat(cfg.runtime_lock)
    def read_text(path, *a, **kw):
        if path == Path('/proc/locks'):
            return (f'1: FLOCK ADVISORY WRITE {pid} {os.major(lock_info.st_dev):x}:{os.minor(lock_info.st_dev):x}:{lock_info.st_ino} 0 EOF\n' if state['active'] else '')
        if str(path)==f'/proc/{pid}/status':
            return f'Uid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t{gid}\t{gid}\t{gid}\t{gid}\n'
        return native_text(path,*a,**kw)
    def read_bytes(path, *a, **kw):
        if str(path)==f'/proc/{pid}/cmdline':
            return ('\0'.join(deployed_unit.split('ExecStart=',1)[1].splitlines()[0].split())+'\0').encode()
        if str(path)==f'/proc/{pid}/environ':
            return ('\0'.join(line.removeprefix('Environment=') for line in state['process_unit'].splitlines() if line.startswith('Environment='))+'\0').encode()
        return native_bytes(path,*a,**kw)
    monkeypatch.setattr(Path,'read_text',read_text)
    monkeypatch.setattr(Path,'read_bytes',read_bytes)
    monkeypatch.setattr(os,'statvfs',lambda path: SimpleNamespace(f_bavail=2,f_frsize=1024**3))
    commands, requests = [], []
    def external(command, **kwargs):
        args=list(command);commands.append(args)
        if args[:2]==['systemctl','show']:
            env=' '.join(shlex.quote(line.removeprefix('Environment=')) for line in state['loaded_unit'].splitlines() if line.startswith('Environment='))
            output=f'LoadState=loaded\nActiveState={"active" if state["active"] else "inactive"}\nMainPID={pid if state["active"] else 0}\nFragmentPath={unit}\nDropInPaths=\nNeedDaemonReload=no\nUser=prediction\nGroup=prediction\nWorkingDirectory={release}\nEnvironment={env}\nExecMainStartTimestamp=systemd-start\n'
        elif args[0]=='ss':
            output=f'LISTEN 0 100 127.0.0.1:8769 0.0.0.0:* users:(("python",pid={pid},fd=3))' if state['active'] else ''
        elif args[0]=='git':
            output='HEAD' if args[-1]=='HEAD' and '--abbrev-ref' in args else ('a'*40 if 'rev-parse' in args else '')
        elif args[0]=='runuser':
            output='a'*40 if 'git' in args else json.dumps(dict(
                result='PASS', account_read=dict(status='ready',complete=True),
                guard=dict(mutation_attempts=0,notification_attempts=0),
                trading_region=dict(status='allowed',blocked=False,country='US',region='CA',
                                    policy='strict',startup_allowed=True)))
        elif args[0]==str(python): output=''
        elif args[0]=='journalctl': output='prediction_runtime_state state=RUNNING'
        else: raise AssertionError('uncontrolled external command: '+str(args))
        return subprocess.CompletedProcess(command,0,output,'')
    monkeypatch.setattr(subprocess,'run',external)
    class HTTP:
        def __init__(self, host, port, **kwargs):
            assert (host,port)==('127.0.0.1',8769)
        def request(self, method, path):
            assert method=='GET';self.path=path;requests.append(path)
        def getresponse(self):
            payload=state['health'] if self.path=='/healthz' else state['lp'] if self.path=='/api/prediction-arbitrage/lp/dashboard' else None
            return SimpleNamespace(status=200 if payload is not None else 503, read=lambda:json.dumps(payload).encode())
        def close(self): pass
    monkeypatch.setattr(cloud.http.client,'HTTPConnection',HTTP)
    return cfg, state, commands, requests


@pytest.mark.parametrize('scenario',['production-zero','production-one','shadow-zero','shadow-one'])
def test_cloud_lifecycle_binds_effective_pause(tmp_path, monkeypatch, scenario):
    import open_trader.prediction_cloud as cloud
    from dataclasses import replace
    mode, value = scenario.split('-');requested = 0 if value=='zero' else 1
    cfg, state, commands, requests = cloud_host_fixture(tmp_path,monkeypatch,mode,requested)
    unit = cloud.render_unit(cfg)
    assert 'Environment=OPEN_TRADER_NLEG_PAUSED=1\n' in unit
    assert 'Environment=OPEN_TRADER_NLEG_PAUSED=0\n' not in unit
    assert ('MemoryMax=805306368\n' in unit) is (mode=='shadow')
    preflight = cloud.operate(cfg,'preflight')
    assert preflight['status']=='PRECHECK_OK'
    assert preflight['n_leg_paused']==1 and preflight['configured_n_leg_paused']==requested
    # Authentication stays required; only production opts into known-JP startup observation.
    auth = next(command for command in commands if 'read-auth' in command)
    assert ('--require-trading-region' in auth) is (mode=='production' or requested==0)
    assert ('--observe-jp-api-region' in auth) is (mode=='production')
    state['active']=True
    status = cloud.operate(cfg,'status')
    assert status['n_leg_paused']==1 and status['configured_n_leg_paused']==requested
    smoke = cloud.operate(cfg,'smoke')
    assert smoke['status']=='BACKEND_SMOKE_OK' and smoke['n_leg_paused']==1
    assert smoke['configured_n_leg_paused']==requested
    assert '/api/prediction-arbitrage/lp/dashboard' in requests
    assert '/api/prediction-arbitrage/state' not in requests
    spec=importlib.util.spec_from_file_location('retirement_cloud_gate',Path(__file__).resolve().parents[1]/'scripts/prediction-cloud-gate.py')
    gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
    for action, result in [('preflight',preflight),('smoke',smoke)]:
        remote=dict(result,release_root=str(cfg.release_root),runtime_root=str(cfg.runtime_root))
        assert gate.remote_identity(remote,cfg,action)['configured_n_leg_paused']==requested
        for field,bad in [('git_sha','b'*40),('runtime_root','/other'),('configured_n_leg_paused',1-requested),('n_leg_paused',0)]:
            with pytest.raises(ValueError): gate.remote_identity(dict(remote,**{field:bad}),cfg,action)
        if action=='smoke':
            with pytest.raises(ValueError):gate.remote_identity(dict(remote,pid=0),cfg,action)
            if mode=='shadow':
                with pytest.raises(ValueError):gate.remote_identity({k:v for k,v in remote.items() if k!='display_snapshot'},cfg,action)
    for bad in ({'status':'running','code':'N_LEG_RUNNING'},{}):
        state['health']['n_leg']=bad
        with pytest.raises(ValueError,match='pause contract'):cloud.operate(cfg,'smoke')
    state['health']['n_leg']={'status':'paused','code':'N_LEG_PAUSED'}
    state['lp']=None
    with pytest.raises(ValueError):cloud.operate(cfg,'smoke')
    state['health']['git_sha']='b'*40
    with pytest.raises(ValueError,match='identity mismatch'):cloud.operate(cfg,'status')
    with pytest.raises(ValueError):cloud.operate(replace(cfg,expected_sha='b'*40),'status')
    assert all(command[0] not in {'ssh','systemd-analyze'} and command[:2] not in [['systemctl','start'],['systemctl','stop']] for command in commands)



@pytest.mark.parametrize(('original_raw', 'layer'), [
    pytest.param(0, 'record', id='zero-to-one-record'),
    pytest.param(0, 'installed', id='zero-to-one-installed'),
    pytest.param(0, 'loaded', id='zero-to-one-loaded'),
    pytest.param(0, 'process', id='zero-to-one-process'),
    pytest.param(1, 'record', id='one-to-zero-record'),
    pytest.param(1, 'installed', id='one-to-zero-installed'),
    pytest.param(1, 'loaded', id='one-to-zero-loaded'),
    pytest.param(1, 'process', id='one-to-zero-process'),
])
def test_cloud_lifecycle_rejects_configured_pause_substitution(tmp_path, monkeypatch, original_raw, layer):
    from dataclasses import replace
    import open_trader.prediction_cloud as cloud

    deployed, host, commands, _ = cloud_host_fixture(tmp_path, monkeypatch, 'shadow', original_raw)
    host['active'] = True
    original_record = json.loads(deployed.record.read_text())
    original_unit = cloud.UNIT_PATH.read_text()
    original_loaded = host['loaded_unit']
    original_process = host['process_unit']
    original_health = json.loads(json.dumps(host['health']))
    original_config = (tmp_path/'cloud.json').read_bytes()
    caller_raw = 1 - original_raw
    caller = replace(deployed, n_leg_paused=caller_raw)
    caller_unit = cloud.render_unit(caller)

    def artifacts():
        return (deployed.record.read_bytes(), cloud.UNIT_PATH.read_bytes(),
                host['loaded_unit'], host['process_unit'], json.dumps(host['health'], sort_keys=True))

    def install_evidence(record_unit, installed, loaded, process):
        record = dict(original_record, unit_text=record_unit)
        deployed.record.write_text(json.dumps(record))
        cloud.UNIT_PATH.write_text(installed)
        host.update(loaded_unit=loaded, process_unit=process)

    def positive(config, raw):
        before = artifacts()
        for action in ('status', 'smoke'):
            result = cloud.operate(config, action)
            assert result['configured_n_leg_paused'] == raw
            assert result['n_leg_paused'] == 1 and result['pid'] == 12345
        assert artifacts() == before

    def rejected(config, expected_error):
        before = artifacts()
        errors = []
        for action in ('status', 'smoke'):
            try:
                cloud.operate(config, action)
            except ValueError as error:
                errors.append(str(error))
            else:
                errors.append(None)
        assert artifacts() == before
        assert all(error is not None for error in errors), (layer, errors)
        assert all(expected_error in error for error in errors), errors

    positive(deployed, original_raw)
    # Every other identity layer matches the substituted caller. Only the selected
    # frozen deployment artifact can reject it, so earlier checks cannot mask it.
    selected = dict(record=caller_unit, installed=caller_unit, loaded=caller_unit, process=caller_unit)
    selected[layer] = dict(record=original_unit, installed=original_unit,
                           loaded=original_loaded, process=original_process)[layer]
    install_evidence(**dict(record_unit=selected['record'], installed=selected['installed'],
                            loaded=selected['loaded'], process=selected['process']))
    errors = dict(record='record', installed='managed unit', loaded='loaded unit',
                  process='running credential/runtime environment')
    rejected(caller, errors[layer])

    # The whole original deployment cannot be attested under the opposite raw value.
    install_evidence(original_unit, original_unit, original_loaded, original_process)
    rejected(caller, 'record')

    # Legacy evidence missing this non-secret marker is ambiguous at each layer.
    legacy_unit = ''.join(line for line in caller_unit.splitlines(keepends=True)
                          if not line.startswith('Environment=OPEN_TRADER_CONFIGURED_NLEG_PAUSED='))
    selected = dict(record=caller_unit, installed=caller_unit, loaded=caller_unit, process=caller_unit)
    selected[layer] = legacy_unit
    install_evidence(selected['record'], selected['installed'], selected['loaded'], selected['process'])
    rejected(caller, errors[layer])
    install_evidence(caller_unit, caller_unit, caller_unit, caller_unit)
    positive(caller, caller_raw)
    install_evidence(original_unit, original_unit, original_loaded, original_process)
    positive(deployed, original_raw)

    # Shape assertions follow public rejection, preserving a behavioral baseline RED.
    assert f'Environment=OPEN_TRADER_CONFIGURED_NLEG_PAUSED={original_raw}\n' in original_unit
    assert f'Environment=OPEN_TRADER_CONFIGURED_NLEG_PAUSED={caller_raw}\n' in caller_unit
    assert original_unit != caller_unit
    for unit in (original_unit, caller_unit):
        assert 'Environment=OPEN_TRADER_NLEG_PAUSED=1\n' in unit
        assert 'Environment=OPEN_TRADER_NLEG_PAUSED=0\n' not in unit
    assert host['health'] == original_health and host['health']['pid'] == 12345
    assert (tmp_path/'cloud.json').read_bytes() == original_config
    assert all(command[0] in {'systemctl', 'ss', 'git', 'runuser', 'journalctl'} for command in commands)
    assert all(command[:2] != ['systemctl', 'start'] and command[:2] != ['systemctl', 'stop'] for command in commands)


@pytest.mark.parametrize('setting',['unset','zero','one'])
def test_capped_shadow_guard_uses_effective_pause(monkeypatch, setting):
    import threading
    from open_trader.prediction_shadow_resources import shadow_resource_guard
    monkeypatch.setenv('OPEN_TRADER_SHADOW_MEMORY_MAX_BYTES','805306368')
    if setting=='unset':monkeypatch.delenv('OPEN_TRADER_NLEG_PAUSED',raising=False)
    else:monkeypatch.setenv('OPEN_TRADER_NLEG_PAUSED','0' if setting=='zero' else '1')
    files={'/proc/self/cgroup':'0::/system.slice/offline.service\n',
        '/proc/meminfo':'MemAvailable: 2097152 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n',
        '/sys/fs/cgroup/system.slice/offline.service/memory.current':'100000000',
        '/sys/fs/cgroup/system.slice/offline.service/memory.max':'805306368',
        '/sys/fs/cgroup/system.slice/offline.service/memory.events':'max 0\noom 0\noom_kill 0\n',
        '/sys/fs/cgroup/system.slice/offline.service/cpu.max':'100000 100000',
        '/sys/fs/cgroup/system.slice/offline.service/pids.max':'96'}
    worker_read=threading.Event();signals=[];native_text=Path.read_text
    def external_read(path,*args,**kwargs):
        if str(path) in files:
            if threading.current_thread().name=='prediction-shadow-resources' and path.name=='pids.max':worker_read.set()
            return files[str(path)]
        if path.is_relative_to('/proc') or path.is_relative_to('/sys/fs/cgroup'):
            raise AssertionError('unexpected host counter: '+str(path))
        return native_text(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',external_read)
    monkeypatch.setattr(os,'kill',lambda pid,signal:signals.append((pid,signal)))
    existing=set(threading.enumerate())
    with shadow_resource_guard('shadow'):
        assert worker_read.wait(4), 'real guard worker did not read external counters'
        assert any(t.name=='prediction-shadow-resources' and t not in existing for t in threading.enumerate())
    assert not any(t.name=='prediction-shadow-resources' and t not in existing for t in threading.enumerate())
    assert signals==[]


@pytest.mark.parametrize('setting',['unset','zero','one'])
def test_local_smoke_uses_effective_pause(tmp_path, monkeypatch, setting):
    from tests.test_deployment_gates import _run_smoke
    native_run = subprocess.run
    current_fault = [None]
    def external_make(command, **kwargs):
        if command[:2] == ['make','production-smoke']:
            command=list(command)
            if setting=='unset':
                command=[arg for arg in command if not arg.startswith('N_LEG_PAUSED=')]
                kwargs['env']=dict(kwargs['env']);kwargs['env'].pop('N_LEG_PAUSED',None)
            if current_fault[0]=='health-missing':
                curl = Path(kwargs['env']['FAKE_CALLS']).parent/'bin/curl'
                text=curl.read_text().replace('if [[ "$url" == "http://127.0.0.1:8769/healthz" ]]; then',
                    'if [[ "$url" == "http://127.0.0.1:8769/healthz" ]]; then\n  exit 22')
                curl.write_text(text)
        return native_run(command,**kwargs)
    monkeypatch.setattr(subprocess,'run',external_make)
    for fault in ('valid','running','health-missing','lp-missing','prediction_listener','prediction_log'):
        current_fault[0]=fault
        fixture=tmp_path/fault;fixture.mkdir()
        result,calls=_run_smoke(fixture,services='prediction',n_leg_paused=0 if setting!='one' else 1,
            prediction_n_leg=('running','N_LEG_RUNNING') if fault=='running' else ('paused','N_LEG_PAUSED'),
            lp_payload='{}' if fault=='lp-missing' else '{"state":"ready","orders":[],"positions":[],"recommendations":[]}',
            failure=fault if fault in ('prediction_listener','prediction_log') else None)
        assert (result.returncode==0) is (fault=='valid'), result.stdout+result.stderr
        assert result.stdout.rstrip().endswith('HEALTHY' if fault=='valid' else 'ROLLBACK')
        assert not any('/api/prediction-arbitrage/state' in call for call in calls)
        if fault=='valid':
            assert any('/api/prediction-arbitrage/lp/dashboard' in call for call in calls)
            assert any(call.startswith('playwright ') for call in calls)
