from pathlib import Path
import pytest

pytestmark = pytest.mark.xdist_group("prediction_cloud_ports")
from open_trader.prediction_cloud import CloudConfig, render_unit


def test_systemd_unit_pins_all_code_paths_and_keeps_secrets_out(tmp_path):
    cfg = CloudConfig(release_root=tmp_path/'release', runtime_root=tmp_path/'runtime',
        python=tmp_path/'venv/bin/python', user='prediction', expected_sha='a'*40,
        region='ap-hongkong', secret='wallet', version='v1', role='reader', n_leg_paused=1)
    unit = render_unit(cfg)
    assert f'WorkingDirectory={cfg.release_root}' in unit
    assert f'PYTHONPATH={cfg.release_root}/src' in unit
    assert f'--release-manifest {cfg.release_root}/ops/prediction-service-release.json' in unit
    assert '--host 127.0.0.1 --port 8769' in unit
    assert 'OPEN_TRADER_CREDENTIAL_BACKEND=tencent-ssm' in unit
    assert 'LimitCORE=0' in unit and 'SendSIGKILL=no' in unit
    assert 'Environment=GIT_CONFIG_KEY_0=safe.directory' in unit
    assert f'Environment=GIT_CONFIG_VALUE_0={cfg.release_root}' in unit
    assert 'User=prediction' in unit and 'OPEN_TRADER_NLEG_PAUSED=1' in unit
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
                    'ap-hongkong','wallet','v1','reader',1)
    with pytest.raises(ValueError, match='SHA mismatch'):
        operate(c, 'preflight')
    assert not c.runtime_root.exists()


def test_two_host_gate_blocks_missing_operator_evidence_before_ssh(tmp_path):
    import subprocess, sys
    gate = Path(__file__).resolve().parents[1]/'scripts/prediction-cloud-gate.py'
    result = subprocess.run([sys.executable,str(gate),'readiness','--client-config',str(tmp_path/'missing'),
        '--service-config',str(tmp_path/'missing'),'--remote-config','/etc/open-trader/cloud.json',
        '--operator-evidence',str(tmp_path/'missing'),'--browser-runtime',str(tmp_path)],capture_output=True,text=True)
    assert result.returncode == 2 and result.stdout.endswith('BLOCKED\n')


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
  if self.path.endswith('/lp/dashboard'):state=dict(state='ready',orders=[],positions=[],recommendations=[])
  body=json.dumps(state).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*a):pass
HTTPServer(('127.0.0.1',8769),Handler).serve_forever()
''')
    for args in [('init','-q'),('add','.'),('-c','user.name=Test','-c','user.email=t@example.invalid','commit','-qm','fixture'),('checkout','--detach')]:
        subprocess.run(['git','-C',str(release),*args],check=True,capture_output=True)
    sha = subprocess.check_output(['git','-C',str(release),'rev-parse','HEAD'],text=True).strip()
    cfg = CloudConfig(release,runtime,Path(sys.executable),service_user,sha,'ap-hongkong','wallet','v1','reader',1)
    unit = tmp_path/'open-trader-prediction.service';unit.write_text(render_unit(cfg))
    monkeypatch.setattr(cloud,'UNIT_PATH',unit)
    for folder in (runtime, runtime/'config', runtime/'data', runtime/'data/prediction_arbitrage'):
        folder.chmod(0o700)
    configfile=runtime/'config/prediction_arbitrage.json'
    configfile.chmod(0o600)
    health = dict(module='prediction_service',schema_version='open_trader.prediction_service.health.v1',
                  git_sha=sha,source_state='clean',status='running',mode='production',production_owner=True,
                  mutations='enabled',reader_generation=2,contract_generation=2,started_at='fixture-start',
                  release_schema_version=cloud.RELEASE_SCHEMA,
                  n_leg=dict(status='paused',code='N_LEG_PAUSED'))
    healthfile=runtime/'health.json';healthfile.write_text(json.dumps(health))
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
    monkeypatch.setattr(subprocess,'run',external)
    try:
        for _ in range(50):
            try: cloud.read_json(8769);break
            except OSError:time.sleep(.05)
        with pytest.raises(ValueError,match='transition record'):
            cloud.operate(cfg,'status')
        cloud.record(cfg,'ready')
        assert cloud.operate(cfg,'status')['status']=='RUNNING'
        assert cloud.operate(cfg,'smoke')['status']=='BACKEND_SMOKE_OK'
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
        if proc.poll() is None:proc.terminate()
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
    cfg=CloudConfig(release,runtime,python,'prediction','a'*40,'ap-hongkong','wallet','v1','reader',1)
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
