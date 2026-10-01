from pathlib import Path
import json
import pytest

pytestmark = pytest.mark.xdist_group("prediction_cloud_ports")
from open_trader.prediction_cloud import CloudConfig, credential_backend, load_config, render_unit


def test_systemd_unit_pins_all_code_paths_and_keeps_secrets_out(tmp_path):
    cfg = CloudConfig(release_root=tmp_path/'release', runtime_root=tmp_path/'runtime',
        python=tmp_path/'venv/bin/python', user='prediction', expected_sha='a'*40,
        region='', secret='', version='', role='',
        mode='shadow', n_leg_paused=1)
    unit = render_unit(cfg)
    assert f'WorkingDirectory={cfg.release_root}' in unit
    assert f'PYTHONPATH={cfg.release_root}/src' in unit
    assert f'--release-manifest {cfg.release_root}/ops/prediction-service-release.json' in unit
    assert '--host 127.0.0.1 --port 8769' in unit
    assert 'OPEN_TRADER_CREDENTIAL_BACKEND=disabled' in unit
    assert 'OPEN_TRADER_SSM_' not in unit
    assert 'LimitCORE=0' in unit and 'SendSIGKILL=no' in unit
    assert 'Environment=GIT_CONFIG_KEY_0=safe.directory' in unit
    assert f'Environment=GIT_CONFIG_VALUE_0={cfg.release_root}' in unit
    assert 'User=prediction' in unit and 'OPEN_TRADER_NLEG_PAUSED=1' in unit
    assert '--mode shadow' in unit
    production = CloudConfig(**{**cfg.__dict__, 'mode':'production', 'region':'ap-hongkong',
        'secret':'wallet','version':'v1','role':'reader'})
    assert '--mode production' in render_unit(production)
    assert 'OPEN_TRADER_CREDENTIAL_BACKEND=tencent-ssm' in render_unit(production)
    assert credential_backend(production) == 'tencent-ssm'
    with pytest.raises(ValueError, match='invalid cloud reference'):
        render_unit(CloudConfig(**{**cfg.__dict__, 'region':'placeholder'}))
    missing = tmp_path/'cloud.json'; missing.write_text('{}')
    with pytest.raises(ValueError, match='explicit production or shadow'):
        load_config(missing)
    missing.write_text(json.dumps({
        'release_root':str(tmp_path/'release'),'runtime_root':str(tmp_path/'runtime'),
        'python':str(tmp_path/'venv/bin/python'),'user':'prediction','expected_sha':'a'*40,
        'mode':'shadow','n_leg_paused':1}))
    credentialless = load_config(missing)
    assert credential_backend(credentialless) == 'disabled'
    with_credentials = {**json.loads(missing.read_text()), 'region':'ap-hongkong',
                        'secret':'wallet', 'version':'v1', 'role':'reader'}
    missing.write_text(json.dumps(with_credentials))
    readonly = load_config(missing)
    assert credential_backend(readonly) == 'tencent-ssm'
    assert 'OPEN_TRADER_CREDENTIAL_BACKEND=tencent-ssm' in render_unit(readonly)
    with_file = {key: value for key, value in json.loads(missing.read_text()).items()
                 if key not in {'region', 'secret', 'version', 'role'}}
    with_file.update(credential_backend='file',
                     credentials_file='/var/lib/open-trader/prediction-credentials/polymarket.json')
    missing.write_text(json.dumps(with_file))
    file_shadow = load_config(missing)
    file_unit = render_unit(file_shadow)
    assert credential_backend(file_shadow) == 'file'
    assert 'OPEN_TRADER_CREDENTIAL_BACKEND=file' in file_unit
    assert 'OPEN_TRADER_CREDENTIAL_FILE=/var/lib/open-trader/prediction-credentials/polymarket.json' in file_unit
    assert 'OPEN_TRADER_SSM_' not in file_unit
    with pytest.raises(ValueError):
        render_unit(CloudConfig(**{**file_shadow.__dict__, 'mode': 'production'}))
    missing.write_text(json.dumps({
        'release_root':str(tmp_path/'release'),'runtime_root':str(tmp_path/'runtime'),
        'python':str(tmp_path/'venv/bin/python'),'user':'prediction','expected_sha':'a'*40,
        'mode':'production','n_leg_paused':0}))
    with pytest.raises(ValueError, match='credential references are required'):
        load_config(missing)
    missing.write_text('{"mode":"readonly"}')
    with pytest.raises(ValueError, match='explicit production or shadow'):
        load_config(missing)
    assert 'SecretKey' not in unit and 'private-key' not in unit
    with pytest.raises(ValueError):
        render_unit(CloudConfig(**{**cfg.__dict__, 'role': 'reader\nExecStart=evil'}))
    with pytest.raises(ValueError):
        render_unit(CloudConfig(**{**cfg.__dict__, 'runtime_root': cfg.release_root/'data'}))


def test_cloud_preflight_rejects_wrong_sha_without_service_mutations(tmp_path, monkeypatch):
    import json, subprocess
    from open_trader.prediction_cloud import operate
    release = tmp_path/'release'
    (release/'ops').mkdir(parents=True)
    (release/'ops/prediction-service-release.json').write_text(json.dumps({
        'schema_version':'open_trader.prediction_service.release.v1','reader_generation':2,'contract_generation':2}))
    for args in [('init','-q'), ('add','.'), ('-c','user.name=Test','-c','user.email=t@example.invalid','commit','-qm','fixture'), ('checkout','--detach')]:
        subprocess.run(['git','-C',str(release),*args],check=True,capture_output=True)
    c = CloudConfig(release, tmp_path/'runtime', Path('/usr/bin/python3'), 'prediction', 'a'*40,
                    '','','','','shadow',1)
    with pytest.raises(ValueError, match='SHA mismatch'):
        operate(c, 'preflight')
    assert not c.runtime_root.exists()


def test_cloud_preflight_does_not_touch_wallet_for_paused_shadow(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    import open_trader.prediction_arbitrage_store as prediction_arbitrage_store
    import open_trader.prediction_cloud as cloud
    cfg = CloudConfig(tmp_path/'release', tmp_path/'runtime', tmp_path/'python',
                      'prediction', 'a'*40, '', '', '', '', 'shadow', 1)
    commands = []
    monkeypatch.setattr(cloud, 'release_identity', lambda c: {'reader_generation': 2})
    monkeypatch.setattr(cloud, 'absent', lambda c: {})
    monkeypatch.setattr(cloud, 'trusted_layout', lambda c: None)
    monkeypatch.setattr(prediction_arbitrage_store,
                        'read_minimum_reader_generation', lambda path: 1)

    def checked_run(*args, **kwargs):
        commands.append(args)
        return cfg.expected_sha if args[-1] == 'HEAD' else 'ok'
    monkeypatch.setattr(cloud, 'run', checked_run)
    monkeypatch.setattr(os, 'statvfs', lambda path: SimpleNamespace(
        f_bavail=2, f_frsize=1024**3))
    cloud.preflight(cfg)
    assert not any('import tencentcloud' in ' '.join(args) for args in commands)
    assert not any('wallet' in ' '.join(args) for args in commands)
    git_command = next(args for args in commands if args[-1] == 'HEAD')
    assert 'OPEN_TRADER_CREDENTIAL_BACKEND=disabled' in git_command


def test_cloud_preflight_file_shadow_uses_safe_auth_without_ssm(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    import open_trader.prediction_arbitrage_store as store_module
    import open_trader.prediction_cloud as cloud
    cfg = CloudConfig(tmp_path/'release', tmp_path/'runtime', tmp_path/'python',
                      'prediction', 'a'*40, '', '', '', '', 'shadow', 1,
                      'file', '/var/lib/open-trader/prediction-credentials/polymarket.json')
    commands = []
    monkeypatch.setattr(cloud, 'release_identity', lambda c: {'reader_generation': 2})
    monkeypatch.setattr(cloud, 'absent', lambda c: {})
    monkeypatch.setattr(cloud, 'trusted_layout', lambda c: None)
    monkeypatch.setattr(store_module, 'read_minimum_reader_generation', lambda path: 1)
    monkeypatch.setattr(cloud, 'run', lambda *args, **kwargs: commands.append(args) or
                        (cfg.expected_sha if args[-1] == 'HEAD' else 'ok'))
    monkeypatch.setattr(os, 'statvfs', lambda path: SimpleNamespace(f_bavail=2, f_frsize=1024**3))
    cloud.preflight(cfg)
    assert any('read-auth' in args for args in commands)
    assert not any('--require-trading-region' in args for args in commands)
    assert not any('import tencentcloud' in ' '.join(args) for args in commands)
    assert any('OPEN_TRADER_CREDENTIAL_FILE='+cfg.credentials_file in args for args in commands)


@pytest.mark.parametrize(('mode','n_leg_paused'), [
    ('production', 1), ('production', 0), ('shadow', 0), ('shadow', 1),
])
def test_cloud_preflight_reads_wallet_except_paused_shadow(tmp_path, monkeypatch, mode, n_leg_paused):
    import os
    from types import SimpleNamespace
    import open_trader.prediction_arbitrage_store as prediction_arbitrage_store
    import open_trader.prediction_cloud as cloud
    cfg = CloudConfig(tmp_path/'release', tmp_path/'runtime', tmp_path/'python',
                      'prediction', 'a'*40, 'region', 'secret', 'v1', 'reader', mode, n_leg_paused)
    commands = []
    monkeypatch.setattr(cloud, 'release_identity', lambda c: {'reader_generation': 2})
    monkeypatch.setattr(cloud, 'absent', lambda c: {})
    monkeypatch.setattr(cloud, 'trusted_layout', lambda c: None)
    monkeypatch.setattr(prediction_arbitrage_store,
                        'read_minimum_reader_generation', lambda path: 1)
    def checked_run(*args, **kwargs):
        commands.append(args)
        return cfg.expected_sha if args[-1] == 'HEAD' else 'ok'
    monkeypatch.setattr(cloud, 'run', checked_run)
    monkeypatch.setattr(os, 'statvfs', lambda path: SimpleNamespace(
        f_bavail=2, f_frsize=1024**3))
    cloud.preflight(cfg)
    assert any('wallet' in ' '.join(args) for args in commands)
    assert not any('wallet status' in ' '.join(args) for args in commands)
    assert any('read-auth' in args for args in commands)
    if mode == 'production' or not n_leg_paused:
        assert any('--require-trading-region' in args for args in commands)
    else:
        assert not any('--require-trading-region' in args for args in commands)


def test_two_host_gate_blocks_missing_operator_evidence_before_ssh(tmp_path):
    import subprocess, sys
    gate = Path(__file__).resolve().parents[1]/'scripts/prediction-cloud-gate.py'
    result = subprocess.run([sys.executable,str(gate),'readiness','--client-config',str(tmp_path/'missing'),
        '--service-config',str(tmp_path/'missing'),'--remote-config','/etc/open-trader/cloud.json',
        '--operator-evidence',str(tmp_path/'missing'),'--browser-runtime',str(tmp_path)],capture_output=True,text=True)
    assert result.returncode == 2 and result.stdout.endswith('BLOCKED\n')


def test_two_host_gate_modes_reject_before_ssh_or_remote_mismatch(tmp_path, monkeypatch):
    import json, os, subprocess, sys
    gate = Path(__file__).resolve().parents[1]/'scripts/prediction-cloud-gate.py'
    release = tmp_path/'release'; (release/'ops').mkdir(parents=True)
    (release/'ops/prediction-service-release.json').write_text(json.dumps({
        'schema_version':'open_trader.prediction_service.release.v1',
        'reader_generation':2,'contract_generation':2}))
    for args in [('init','-q'),('add','.'),('-c','user.name=Test','-c','user.email=t@example.invalid','commit','-qm','fixture'),('checkout','--detach')]:
        subprocess.run(['git','-C',str(release),*args],check=True,capture_output=True)
    sha = subprocess.check_output(['git','-C',str(release),'rev-parse','HEAD'],text=True).strip()
    fakebin = tmp_path/'bin'; fakebin.mkdir(); ssh = fakebin/'ssh'
    remote_runtime = tmp_path/'remote-runtime'
    ssh.write_text(f'''#!/usr/bin/env python3
import json, os
from pathlib import Path
if os.environ.get('OPEN_TRADER_SMOKE_URL'):
    with open(os.environ['GATE_URL_LOG'], 'a') as output:
        output.write(os.environ['OPEN_TRADER_SMOKE_URL']+'\\n')
root = Path(os.environ['GATE_FIXTURE'])
count = int(root.joinpath('ssh-count').read_text()) if root.joinpath('ssh-count').exists() else 0
root.joinpath('ssh-count').write_text(str(count+1))
print(json.dumps({{
    'mode': root.joinpath('remote-mode').read_text().strip(),
    'git_sha': os.environ['GATE_SHA'],
    'release_root': os.environ['GATE_RELEASE_ROOT'],
    'runtime_root': os.environ['GATE_REMOTE_RUNTIME'],
    'n_leg_paused': int(os.environ.get('GATE_REMOTE_NLEG', '1')),
    'credential_backend': os.environ.get('GATE_REMOTE_BACKEND', 'disabled'),
    'status': 'PRECHECK_OK',
}}))
''')
    ssh.chmod(0o755)
    monkeypatch.setenv('PATH',str(fakebin)+os.pathsep+os.environ['PATH'])
    monkeypatch.setenv('GATE_FIXTURE',str(tmp_path))
    monkeypatch.setenv('GATE_SHA',sha)
    monkeypatch.setenv('GATE_RELEASE_ROOT',str(release))
    monkeypatch.setenv('GATE_REMOTE_RUNTIME',str(remote_runtime))
    monkeypatch.setenv('GATE_URL_LOG',str(tmp_path/'browser-urls.log'))
    client_base = dict(release_root=str(release), runtime_root=str(tmp_path/'client'),
                       python=str(fakebin/'python'), ssh_alias='open-trader-test', expected_sha=sha,
                       gateway_port=8876, tunnel_port=8879)
    cloud_base = dict(release_root=str(release), runtime_root=str(remote_runtime),
                      python=str(fakebin/'python'), user='prediction', expected_sha=sha,
                      n_leg_paused=1)
    client = tmp_path/'client.json'; cloud = tmp_path/'cloud.json'
    evidence = tmp_path/'evidence.json'; evidence.write_text(json.dumps({
        'git_sha':sha, 'independent_runtime_root':str(remote_runtime),
        'resources_reviewed':True, 'resources_reviewed_evidence':'observed'}))
    def write_configs(client_mode, service_mode, *, file_profile=False):
        client.write_text(json.dumps({**client_base, 'mode':client_mode}))
        cloud.write_text(json.dumps({**cloud_base, 'mode':service_mode, **(
            {'credential_backend':'file', 'credentials_file':'/var/lib/open-trader/prediction-credentials/polymarket.json'}
            if file_profile else {})}))
    fake_python = fakebin/'python'; fake_python.write_text('''#!/bin/sh
if [ -n "$OPEN_TRADER_SMOKE_URL" ]; then printf '%s\\n' "$OPEN_TRADER_SMOKE_URL" >> "$GATE_URL_LOG"; fi
exit 0
''')
    fake_python.chmod(0o755)
    fake_node = fakebin/'node'; fake_node.write_text('#!/bin/sh\nexit 0\n')
    fake_node.chmod(0o755)
    runner = tmp_path/'node_modules/.bin/playwright'; runner.parent.mkdir(parents=True)
    runner.write_text('''#!/bin/sh
if [ -n "$OPEN_TRADER_SMOKE_URL" ]; then printf '%s\\n' "$OPEN_TRADER_SMOKE_URL" >> "$GATE_URL_LOG"; fi
exit 0
'''); runner.chmod(0o755)
    (fake_node.parent/'node').write_text('''#!/bin/sh
if [ -n "$OPEN_TRADER_SMOKE_URL" ]; then printf '%s\\n' "$OPEN_TRADER_SMOKE_URL" >> "$GATE_URL_LOG"; fi
exit 0
''')
    fake_node.chmod(0o755)
    def evidence_without_independent_runtime():
        evidence.write_text(json.dumps({
            'git_sha':sha, 'resources_reviewed':True,
            'resources_reviewed_evidence':'observed'}))
    def restore_evidence():
        evidence.write_text(json.dumps({
            'git_sha':sha, 'independent_runtime_root':str(remote_runtime),
            'resources_reviewed':True, 'resources_reviewed_evidence':'observed'}))
    def run(client_mode, service_mode, remote_mode, remote_backend='disabled', *, file_profile=False):
        (tmp_path/'ssh-count').unlink(missing_ok=True)
        (tmp_path/'remote-mode').write_text(remote_mode)
        monkeypatch.setenv('GATE_REMOTE_BACKEND',remote_backend)
        write_configs(client_mode, service_mode, file_profile=file_profile)
        return subprocess.run([sys.executable,str(gate),'readiness','--client-config',str(client),
            '--service-config',str(cloud),'--remote-config','/etc/open-trader/cloud.json',
            '--operator-evidence',str(evidence),'--browser-runtime',str(tmp_path)],
            capture_output=True,text=True)
    result = run('shadow','production','production')
    assert result.returncode == 2 and result.stdout.endswith('BLOCKED\n')
    assert not (tmp_path/'ssh-count').exists()
    evidence_without_independent_runtime()
    result = run('shadow','shadow','shadow')
    assert result.returncode == 2 and result.stdout.endswith('BLOCKED\n')
    assert not (tmp_path/'ssh-count').exists()
    restore_evidence()
    result = run('shadow','shadow','shadow')
    assert result.returncode == 0 and result.stdout.endswith('READY\n'), result.stdout + result.stderr
    assert (tmp_path/'ssh-count').exists(), result.stdout + result.stderr
    assert (tmp_path/'ssh-count').read_text() == '1'
    assert (tmp_path/'browser-urls.log').read_text().splitlines() == ['http://127.0.0.1:8876/']*3
    result = run('shadow','shadow','shadow',remote_backend='tencent-ssm')
    assert result.returncode == 2 and result.stdout.endswith('BLOCKED\n')
    assert (tmp_path/'ssh-count').read_text() == '1'
    result = run('shadow','shadow','shadow',remote_backend='file',file_profile=True)
    assert result.returncode == 0 and result.stdout.endswith('READY\n'), result.stdout + result.stderr
    result = run('shadow','shadow','production')
    assert result.returncode == 2 and result.stdout.endswith('BLOCKED\n')
    assert (tmp_path/'ssh-count').exists(), result.stdout + result.stderr
    assert (tmp_path/'ssh-count').read_text() == '1'


def test_systemd_identity_and_stop_use_actual_process_listener_and_lock(tmp_path, monkeypatch):
    import json, os, shutil, subprocess, sys, time
    import open_trader.prediction_cloud as cloud
    if sys.platform != 'linux':
        pytest.skip('Linux /proc evidence is tested in isolated Docker')
    import pwd
    from types import SimpleNamespace
    service_user = 'daemon'  # Existing non-root user/group in the read-only dev image.
    user = SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid())
    monkeypatch.setattr(pwd,'getpwnam',lambda name:user)
    monkeypatch.setattr(cloud,'service_user',lambda c:user)  # SETUID is unavailable in dev Docker.
    for parent in (tmp_path, *tmp_path.parents):
        if parent != Path('/tmp') and parent != Path('/'):
            parent.chmod(parent.stat().st_mode | 0o055)
    release, runtime = tmp_path/'release', tmp_path/'runtime'
    package = release/'src/open_trader'; package.mkdir(parents=True)
    (package/'__init__.py').write_text('')
    (release/'ops').mkdir()
    shutil.copy(Path(__file__).resolve().parents[1]/'ops/prediction-service-release.json',release/'ops')
    (runtime/'data/prediction_arbitrage').mkdir(parents=True)
    (runtime/'config').mkdir()
    (runtime/'config/prediction_arbitrage.json').write_text('{}')
    (package/'__main__.py').write_text('''import os,json,fcntl,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
root=Path.cwd(); runtime=Path(os.environ['TEST_RUNTIME'])
lock=(runtime/'data/prediction_arbitrage/runtime.lock').open('a')
fcntl.flock(lock,fcntl.LOCK_EX)
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  state=json.loads((runtime/'health.json').read_text())
  state.update(pid=os.getpid(),cwd=str(root),code_root=str(root/'src'))
  if self.path.endswith('/lp/dashboard'):
   if state.get('mode') != 'production':
    self.send_response(503);self.send_header('Content-Length','0');self.end_headers();return
   body=json.dumps(dict(state='ready',orders=[],positions=[],recommendations=[])).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
  body=json.dumps(state).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*a):pass
server=HTTPServer(('127.0.0.1',0),Handler)
(runtime/'listener.json').write_text(json.dumps(dict(pid=os.getpid(),port=server.server_port)))
server.serve_forever()
''')
    for args in [('init','-q'),('add','.'),('-c','user.name=Test','-c','user.email=t@example.invalid','commit','-qm','fixture'),('checkout','--detach')]:
        subprocess.run(['git','-C',str(release),*args],check=True,capture_output=True)
    sha = subprocess.check_output(['git','-C',str(release),'rev-parse','HEAD'],text=True).strip()
    cfg = CloudConfig(release,runtime,Path(sys.executable),service_user,sha,'','','','','shadow',1)
    unit = tmp_path/'open-trader-prediction.service';unit.write_text(render_unit(cfg))
    monkeypatch.setattr(cloud,'UNIT_PATH',unit)
    for folder in (runtime, runtime/'config', runtime/'data', runtime/'data/prediction_arbitrage'):
        folder.chmod(0o700)
    configfile=runtime/'config/prediction_arbitrage.json'
    configfile.chmod(0o600)
    health = dict(module='prediction_service',schema_version='open_trader.prediction_service.health.v1',
                  git_sha=sha,source_state='clean',status='running',mode='shadow',production_owner=False,
                  mutations='prohibited',first_violation=None,reader_generation=2,contract_generation=2,started_at='fixture-start',
                  release_schema_version=cloud.RELEASE_SCHEMA,
                  n_leg=dict(status='paused',code='N_LEG_PAUSED'))
    healthfile=runtime/'health.json';healthfile.write_text(json.dumps(health))
    # Let the fixture process own its ephemeral listener. The production
    # contract still uses logical port 8769; only local HTTP transport maps it.
    listener_file = runtime/'listener.json'
    def fixture_port():
        try:
            value = json.loads(listener_file.read_text())
        except (OSError, ValueError) as exc:
            raise OSError('fixture listener is not ready') from exc
        if value['pid'] != proc.pid:
            raise OSError('stale fixture listener identity')
        return value['port']
    original_read_json, original_read_status = cloud.read_json, cloud.read_status
    monkeypatch.setattr(cloud, 'read_json', lambda _port, path='/healthz': original_read_json(fixture_port(), path))
    monkeypatch.setattr(cloud, 'read_status', lambda _port, path: original_read_status(fixture_port(), path))

    command=render_unit(cfg).split('ExecStart=',1)[1].splitlines()[0].split()
    def launch():
        references=dict(line.removeprefix('Environment=').split('=',1) for line in render_unit(cfg).splitlines() if line.startswith('Environment='))
        return subprocess.Popen(command,cwd=release,env={**os.environ,**references,'TEST_RUNTIME':str(runtime)},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    proc=launch()
    original_run=subprocess.run; mutations=[]; loaded_version='v1'
    def external(args,**kwargs):
        nonlocal proc
        if args[:2]==('systemctl','show'):
            environment=' '.join(line.removeprefix('Environment=') for line in render_unit(cfg).splitlines() if line.startswith('Environment='))
            environment=environment.replace('OPEN_TRADER_SSM_VERSION=v1','OPEN_TRADER_SSM_VERSION='+loaded_version)
            if loaded_version == 'old-version':
                environment += ' OPEN_TRADER_UNIT_DRIFT=yes'
            active=proc.poll() is None
            stdout=f'LoadState=loaded\nActiveState={"active" if active else "inactive"}\nMainPID={proc.pid if active else 0}\nFragmentPath={unit}\nDropInPaths=\nExecMainStartTimestamp=2026-09-29 00:00:00 UTC\nNeedDaemonReload=no\nEnvironment={environment}\nUser={service_user}\nGroup={service_user}\nWorkingDirectory={release}\n'
        elif args[:2]==('systemctl','stop'):
            mutations.append('stop');proc.terminate();proc.wait(timeout=5);stdout=''
        elif args[:2]==('systemctl','start'):
            mutations.append('start');proc=launch();stdout=''
        elif args[0]=='runuser':
            assert f'GIT_CONFIG_VALUE_0={release}' in args
            if 'git' in args:
                # Dev Docker drops SETUID; Git's own ownership test switch exercises
                # the same dubious-ownership protection without extra capabilities.
                return original_run(('env','GIT_TEST_ASSUME_DIFFERENT_OWNER=1',*args[5:]),**kwargs)
            stdout=''
        elif args[:2]==('systemctl','daemon-reload') or args[0]=='systemd-analyze':
            stdout=''
        elif args[0]=='ss':
            stdout=f'LISTEN 0 100 127.0.0.1:8769 0.0.0.0:* users:(("python",pid={proc.pid},fd=3))' if proc.poll() is None else ''
        elif args[0]=='journalctl':stdout='prediction_runtime_state state=RUNNING'
        else:return original_run(args,**kwargs)
        return subprocess.CompletedProcess(args,0,stdout,'')
    def wait_ready():
        deadline = time.monotonic() + 2.5
        last = None
        while time.monotonic() < deadline:
            assert proc.poll() is None, f"fixture exited before readiness: {proc.returncode}"
            try:
                value = cloud.read_json(8769)
                if value.get('pid') == proc.pid and value.get('git_sha') == sha:
                    return value
                last = value
            except OSError as exc:
                last = exc
            time.sleep(.05)
        raise AssertionError(f"fixture pid={proc.pid} did not become ready: {last!r}")

    monkeypatch.setattr(subprocess,'run',external)
    try:
        wait_ready()
        with pytest.raises(ValueError,match='transition record'):
            cloud.operate(cfg,'status')
        cloud.record(cfg,'ready')
        assert cloud.operate(cfg,'status')['status']=='RUNNING'
        assert cloud.operate(cfg,'smoke')['status']=='BACKEND_SMOKE_OK'
        healthfile.write_text(json.dumps({**health,'first_violation':{'client':'guarded'}}))
        with pytest.raises(ValueError,match='identity mismatch'): cloud.operate(cfg,'status')
        assert mutations==[]
        healthfile.write_text(json.dumps(health))
        production_health = {**health,'mode':'production','production_owner':True,
                             'mutations':'enabled'}
        healthfile.write_text(json.dumps(production_health))
        cfg = CloudConfig(**{**cfg.__dict__, 'mode':'production',
                             'region':'ap-hongkong','secret':'wallet',
                             'version':'v1','role':'reader'})
        unit.write_text(render_unit(cfg))
        proc.terminate();proc.wait(timeout=5)
        command=render_unit(cfg).split('ExecStart=',1)[1].splitlines()[0].split()
        proc=launch()
        wait_ready()
        cloud.record(cfg,'ready')
        assert cloud.operate(cfg,'status')['status']=='RUNNING'
        assert cloud.operate(cfg,'smoke')['status']=='BACKEND_SMOKE_OK'
        cfg = CloudConfig(**{**cfg.__dict__, 'mode':'shadow',
                             'region':'','secret':'','version':'','role':''})
        unit.write_text(render_unit(cfg))
        proc.terminate();proc.wait(timeout=5)
        command=render_unit(cfg).split('ExecStart=',1)[1].splitlines()[0].split()
        proc=launch()
        wait_ready()
        healthfile.write_text(json.dumps({**production_health,'production_owner':False,
                                          'mutations':'prohibited'}))
        cloud.record(cfg,'ready')
        with pytest.raises(ValueError,match='identity mismatch'): cloud.operate(cfg,'status')
        healthfile.write_text(json.dumps(health))
        wait_ready()
        user.pw_gid=999999
        with pytest.raises(ValueError,match='user/group mismatch'): cloud.operate(cfg,'status')
        user.pw_gid=os.getgid()
        loaded_version='old-version'
        with pytest.raises(ValueError,match='loaded unit'): cloud.operate(cfg,'stop')
        assert mutations==[]
        loaded_version='v1'
        cloud.record(cfg,'failed')
        with pytest.raises(ValueError,match='transition record'): cloud.operate(cfg,'start')
        with pytest.raises(ValueError,match='transition record'): cloud.operate(cfg,'smoke')
        cloud.record(cfg,'ready')
        healthfile.write_text(json.dumps({**health,'started_at':''}))
        with pytest.raises(ValueError,match='identity mismatch'): cloud.operate(cfg,'status')
        healthfile.write_text(json.dumps({**health,'git_sha':'b'*40}))
        with pytest.raises(ValueError,match='identity mismatch'):cloud.operate(cfg,'stop')
        assert mutations==[] and proc.poll() is None
        healthfile.write_text(json.dumps(health))
        assert cloud.operate(cfg,'stop')['status']=='STOPPED'
        assert cloud.operate(cfg,'status')['status']=='STOPPED'
        assert mutations==['stop']
        cloud.record(cfg,'failed')
        with pytest.raises(ValueError,match='transition record'): cloud.operate(cfg,'status')
        assert cloud.operate(cfg,'stop')['status']=='STOPPED'
        assert cloud.operate(cfg,'status')['status']=='STOPPED'
        # Path trust has separate filesystem tests; this fixture process runs as
        # the container user because the dev gate deliberately drops SETUID.
        monkeypatch.setattr(cloud,'trusted_layout',lambda c:None)
        assert cloud.operate(cfg,'install')['status']=='INSTALLED_STOPPED'
        assert proc.poll() is not None
        assert cloud.operate(cfg,'start')['status']=='RUNNING'
        assert cloud.operate(cfg,'start')['status']=='RUNNING'
        assert mutations==['stop','start']
        assert cloud.operate(cfg,'stop')['status']=='STOPPED'
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_cloud_trust_rejects_writable_and_symlinked_paths(tmp_path):
    import os
    from open_trader.prediction_cloud import trusted_root_path, trusted_config
    if os.getuid() != 0:
        pytest.skip('root-owned deployment paths checked inside Docker')
    config=tmp_path/'cloud.json';config.write_text('{}');config.chmod(0o600)
    trusted_config(config)
    config.chmod(0o644)
    with pytest.raises(ValueError,match='0600'): trusted_config(config)
    config.chmod(0o666)
    with pytest.raises(ValueError,match='root-owned'): trusted_root_path(config)
    config.chmod(0o600)
    link=tmp_path/'link';link.symlink_to(config)
    with pytest.raises(ValueError,match='canonical'): trusted_root_path(link)
    tmp_path.chmod(0o777)
    with pytest.raises(ValueError,match='root-owned'): trusted_root_path(config)


def test_cloud_layout_checks_service_identity_and_private_configuration(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    import open_trader.prediction_cloud as cloud
    if os.getuid() != 0:
        pytest.skip('root-owned deployment paths checked inside Docker')
    release=tmp_path/'release';release.mkdir()
    source=release/'service.py';source.write_text('')
    runtime=tmp_path/'runtime';runtime.mkdir(mode=0o700)
    (runtime/'config').mkdir(mode=0o700)
    config=runtime/'config/prediction_arbitrage.json';config.write_text('{}');config.chmod(0o600)
    python=tmp_path/'bin/python';python.parent.mkdir();python.touch()
    cfg=CloudConfig(release,runtime,python,'prediction','a'*40,'ap-hongkong','wallet','v1','reader','shadow',1)
    user=SimpleNamespace(pw_uid=1001,pw_gid=1001)
    monkeypatch.setattr(cloud.pwd,'getpwnam',lambda name:user)
    monkeypatch.setattr(cloud.grp,'getgrnam',lambda name:SimpleNamespace(gr_gid=1001))
    original=Path.lstat
    def service_stat(path):
        info=original(path)
        if path == runtime or path.is_relative_to(runtime):
            fields=list(info);fields[4]=1001;fields[5]=1001
            return os.stat_result(fields)
        return info
    # Docker deliberately lacks CHOWN/SETUID; only OS ownership metadata is
    # substituted. The actual directories, modes and symlinks remain real.
    monkeypatch.setattr(Path,'lstat',service_stat)
    cloud.trusted_layout(cfg)
    source.chmod(0o666)
    with pytest.raises(ValueError,match='release tree'): cloud.trusted_layout(cfg)
    source.chmod(0o644); config.chmod(0o644)
    with pytest.raises(ValueError,match='private runtime'): cloud.trusted_layout(cfg)
    config.chmod(0o600); user.pw_gid=1002
    with pytest.raises(ValueError,match='primary group'): cloud.trusted_layout(cfg)
    user.pw_gid=1001;user.pw_uid=0
    with pytest.raises(ValueError,match='non-root'): cloud.trusted_layout(cfg)
    with pytest.raises(ValueError,match='non-root'): cloud.live_identity(cfg)


def test_cloud_display_smoke_accepts_reward_usd_unknown_and_records_source_evidence():
    from datetime import UTC, datetime, timedelta
    from open_trader.prediction_cloud import display_snapshot_evidence
    snapshot = dict(authenticated=True,stale=False,checked_at=datetime.now(UTC).isoformat(),
        orders=[],positions=[],recommendations=[],catalog_complete=True,
        open_orders_complete=True,positions_complete=True,trades_complete=False,
        candidate_state='unknown',candidate_stale=True,
        preparation={'state':'ready','checked_at':'source-time'},
        market_rewards={'condition':{'state':'unknown','reason':'usd_value_unknown','usd_value':None}})
    evidence = display_snapshot_evidence(snapshot)
    assert evidence['rewards']['condition']['reason'] == 'usd_value_unknown'
    assert evidence['account']['trades_complete'] is False
    assert evidence['candidates']['candidate_state'] == 'unknown'
    assert evidence['history']['state'] == 'ready'
    for change in ({'stale':True},{'authenticated':False},{'checked_at':(datetime.now(UTC)-timedelta(seconds=61)).isoformat()}):
        with pytest.raises(ValueError): display_snapshot_evidence({**snapshot,**change})


@pytest.mark.parametrize('change', [
    'refresh', 'pid', 'started_at', 'systemd_started_at', 'git_sha', 'mode',
    'release_root', 'runtime_root', 'n_leg_paused', 'credential_backend', 'status',
    'missing_pid', 'missing_started_at', 'missing_systemd_started_at',
    'missing_git_sha', 'missing_mode', 'missing_release_root', 'missing_runtime_root',
    'missing_n_leg_paused', 'missing_credential_backend', 'missing_status',
    'missing_display_snapshot', 'stale_display_snapshot', 'missing_snapshot_account',
    'missing_snapshot_time', 'changed_extra_identity',
])
@pytest.mark.parametrize('observation', ['before', 'after'])
def test_two_host_smoke_revalidates_refresh_and_stable_identity(tmp_path, monkeypatch, capsys, change, observation):
    import importlib.util
    import sys
    from datetime import UTC, datetime, timedelta
    spec = importlib.util.spec_from_file_location('cloud_gate',
        Path(__file__).resolve().parents[1]/'scripts/prediction-cloud-gate.py')
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    cfg = CloudConfig(tmp_path/'release', tmp_path/'remote', tmp_path/'python',
        'prediction', 'a'*40, '', '', '', '', 'shadow', 1, 'file', '/private/wallet.json')
    local = dict(release_root=str(cfg.release_root), runtime_root=str(tmp_path/'client'),
        python=str(cfg.python), ssh_alias='fixture', expected_sha='b'*40,
        cloud_expected_sha=cfg.expected_sha, execution_port=8769,
        execution_expected_sha='c'*40, gateway_port=8766, tunnel_port=8879, mode='shadow')
    client_config = tmp_path/'client.json'; client_config.write_text(json.dumps(local))
    operator = tmp_path/'evidence.json'; operator.write_text(json.dumps(dict(
        git_sha=cfg.expected_sha, independent_runtime_root=str(cfg.runtime_root),
        resources_reviewed=True, resources_reviewed_evidence='offline fixture')))
    cfg.runtime_root.mkdir()
    Path(local['runtime_root']).mkdir()
    (Path(local['runtime_root'])/'gateway.log').write_text('frontend_gateway_runtime: started\n')
    monkeypatch.setattr(gate, 'load_config', lambda path: cfg)
    monkeypatch.setattr(gate, 'validate', lambda value: None)
    monkeypatch.setattr(gate, 'inspect_prediction_release_checkout', lambda path: {'git_sha':local['expected_sha']})
    monkeypatch.setattr(gate, 'client_release', lambda value: None)
    monkeypatch.setattr(gate, 'client_operation', lambda value, action: dict(
        status='CONNECTED', execution_status='ok', execution_git_sha=local['execution_expected_sha']))
    before = dict(status='BACKEND_SMOKE_OK', pid=123, started_at='process-start',
        systemd_started_at='systemd-start', git_sha=cfg.expected_sha, mode='shadow',
        release_root=str(cfg.release_root), runtime_root=str(cfg.runtime_root),
        n_leg_paused=1, credential_backend='file', display_snapshot=dict(
            account={'checked_at':datetime.now(UTC).isoformat()}, catalog={'complete':True},
            candidates={'candidate_state':'unknown'}, history=None,
            rewards={'condition':{'state':'unknown','reason':'usd_value_unknown'}}))
    after = json.loads(json.dumps(before))
    after['display_snapshot']['account']['checked_at'] = datetime.now(UTC).isoformat()
    after['display_snapshot']['catalog']['complete'] = False
    changed = before if observation == 'before' else after
    if change == 'missing_snapshot_account':
        changed['display_snapshot'].pop('account')
    elif change == 'missing_snapshot_time':
        changed['display_snapshot']['account'].pop('checked_at')
    elif change == 'changed_extra_identity':
        changed['extra_identity'] = 'changed'
    elif change.startswith('missing_'):
        changed.pop(change.removeprefix('missing_'))
    elif change == 'stale_display_snapshot':
        changed['display_snapshot']['account']['checked_at'] = (datetime.now(UTC)-timedelta(seconds=61)).isoformat()
    elif change != 'refresh':
        changed[change] = {'pid':124, 'n_leg_paused':0}.get(change, 'changed')
    remote_results = iter([before, after]); ssh_calls = []
    def checked(command, **kwargs):
        if command[0] == 'ssh':
            ssh_calls.append(command)
            return json.dumps(next(remote_results))
        if command[0] == 'git': return 'HEAD\n'
        return ''
    monkeypatch.setattr(gate, 'checked', checked)
    monkeypatch.setattr(sys, 'argv', ['gate', 'smoke', '--client-config',str(client_config),
        '--service-config',str(tmp_path/'unused'), '--remote-config','/etc/cloud.json',
        '--operator-evidence',str(operator), '--browser-runtime',str(tmp_path)])
    assert gate.main() == (0 if change == 'refresh' else 2)
    assert capsys.readouterr().out.endswith('HEALTHY\n' if change == 'refresh' else 'ROLLBACK\n')
    early_rejection = observation == 'before' and change not in {
        'refresh', 'pid', 'started_at', 'systemd_started_at', 'changed_extra_identity'}
    assert len(ssh_calls) == (1 if early_rejection else 2)
