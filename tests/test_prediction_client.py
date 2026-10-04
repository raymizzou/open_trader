import json
from pathlib import Path
import pytest

pytestmark = pytest.mark.xdist_group("prediction_cloud_ports")
from open_trader.prediction_client import client_operation, client_ports, status, validate


def test_client_stop_without_state_does_not_touch_other_processes(tmp_path, monkeypatch):
    cfg = dict(release_root=str(tmp_path/'release'), runtime_root=str(tmp_path/'runtime'),
               python='/usr/bin/python3', ssh_alias='open-trader-hk', expected_sha='a'*40,
               mode='shadow')
    def forbidden(*a, **k): raise AssertionError('must not execute SSH or signal processes')
    monkeypatch.setattr('subprocess.run', forbidden)
    monkeypatch.setattr('os.kill', forbidden)
    assert client_operation(cfg, 'stop')['status'] == 'STOPPED'
    assert not Path(cfg['runtime_root']).exists()


def test_client_refuses_reused_pid_and_retains_state(tmp_path, monkeypatch):
    import os
    root = tmp_path/'client'
    root.mkdir(mode=0o700)
    cfg = dict(release_root=str(tmp_path/'release'), runtime_root=str(root),
               python='/usr/bin/python3',ssh_alias='open-trader-hk',expected_sha='a'*40,mode='shadow')
    (root/'client.json').write_text(json.dumps({'config':cfg,'gateway':{'pid':os.getpid(),'identity':'different-process'}}))
    def forbidden(*a, **k): raise AssertionError('must not signal changed PID')
    monkeypatch.setattr('os.kill', forbidden)
    with pytest.raises(ValueError,match='PID identity changed'):
        client_operation(cfg,'stop')
    assert (root/'client.json').exists()


def test_client_status_accepts_selected_mode_and_rejects_mismatch_or_guard(tmp_path, monkeypatch):
    import open_trader.prediction_client as client
    release = str(tmp_path/'release')
    base = dict(release_root=release,runtime_root=str(tmp_path/'client'),
                python='/usr/bin/python3',ssh_alias='test',expected_sha='a'*40)
    with pytest.raises(ValueError,match='explicit production or shadow'):
        validate({**base,'mode':'readonly'})
    with pytest.raises(ValueError,match='invalid client config keys'):
        validate({**base})
    state = {'gateway':{'pid':4242,'identity':'owned'},'ssh':{'pid':4243,'identity':'owned'}}
    monkeypatch.setattr(client,'same_process',lambda proc:True)
    monkeypatch.setattr(client,'client_release',lambda c:{
        'reader_generation':2,'contract_generation':2})
    def health(mode, first_violation='sentinel'):
        payload = dict(pid=4242,git_sha='a'*40,source_state='clean',started_at='fixture',
                       module='prediction_service',status='running',mode=mode,
                           schema_version='open_trader.prediction_service.health.v1',
                           release_schema_version=client.RELEASE_SCHEMA,reader_generation=2,
                           contract_generation=2,cwd=release,code_root=release+'/src')
        if mode == 'shadow':
            payload.update(production_owner=False,mutations='prohibited')
            payload['first_violation'] = None if first_violation == 'sentinel' else first_violation
        else:
            payload.update(production_owner=True,mutations='enabled')
        return payload
    def gateway(mode, **kwargs):
        return {**health(mode,**kwargs),'prediction_only':True,
                'prediction_upstream_status':'ok','prediction_route_mode':'service',
                'cwd':release,'code_root':release+'/src'}
    monkeypatch.setattr(client,'read_json',lambda port:gateway('production') if port==8766 else health('production'))
    assert status({**base,'mode':'production'},state)['status']=='CONNECTED'
    shadow_cfg = {**base,'mode':'shadow','gateway_port':8876,'tunnel_port':8879}
    monkeypatch.setattr(client,'read_json',lambda port:gateway('shadow') if port==8876 else health('shadow'))
    assert status(shadow_cfg,state)['status']=='CONNECTED'
    monkeypatch.setattr(client,'read_json',lambda port:gateway('shadow') if port==8876 else health('production'))
    with pytest.raises(ValueError,match='health identity'): status(shadow_cfg,state)
    shadow = health('shadow')
    shadow.pop('first_violation')
    monkeypatch.setattr(client,'read_json',lambda port:gateway('shadow') if port==8876 else shadow)
    with pytest.raises(ValueError,match='health identity'): status(shadow_cfg,state)


def test_client_ports_default_by_mode_and_reject_collisions():
    assert client_ports({'mode':'shadow'}) == (8876, 8879)
    assert client_ports({'mode':'production'}) == (8766, 8769)
    assert client_ports({'mode':'shadow','gateway_port':1876,'tunnel_port':1879}) == (1876, 1879)
    for bad in (
        {'mode':'shadow','gateway_port':True,'tunnel_port':8879},
        {'mode':'shadow','gateway_port':0,'tunnel_port':8879},
        {'mode':'shadow','gateway_port':65536,'tunnel_port':8879},
        {'mode':'shadow','gateway_port':8879,'tunnel_port':8879},
    ):
        with pytest.raises(ValueError, match='distinct loopback client ports'):
            client_ports(bad)


@pytest.mark.parametrize('startup', [
    'exec', 'stable', 'incomplete', 'never_complete', 'incomplete_changed_start',
    'changed_start', 'changed_args', 'exited', 'wrong_health', 'late_exit', 'late_args',
])
def test_client_start_pins_post_exec_identity_and_keeps_replacement_fences(
    tmp_path, monkeypatch, startup,
):
    import os, shutil, socket, subprocess, time
    from types import SimpleNamespace
    import open_trader.prediction_client as client

    release = (tmp_path/'release').resolve()
    (release/'src/open_trader').mkdir(parents=True)
    (release/'src/open_trader/__init__.py').write_text('')
    (release/'ops').mkdir()
    shutil.copy(Path(__file__).resolve().parents[1]/'ops/prediction-service-release.json', release/'ops')
    for args in [('init','-q'), ('add','.'), ('-c','user.name=Test','-c','user.email=t@example.invalid','commit','-qm','fixture'), ('checkout','--detach')]:
        subprocess.run(['git','-C',str(release),*args],check=True,capture_output=True)
    sha = subprocess.check_output(['git','-C',str(release),'rev-parse','HEAD'],text=True).strip()
    with socket.socket() as gateway_port, socket.socket() as tunnel_port:
        gateway_port.bind(('127.0.0.1',0)); tunnel_port.bind(('127.0.0.1',0))
        cfg = dict(release_root=str(release), runtime_root=str(tmp_path/'client'),
                   python='/fixture/venv/bin/python', ssh_alias='fixture-host',
                   expected_sha=sha, mode='shadow',
                   gateway_port=gateway_port.getsockname()[1], tunnel_port=tunnel_port.getsockname()[1])
    children = {}
    initial = 'Sun Oct 4 07:13:56 2026 '
    final_python = '/fixture/Framework/Resources/Python.app/Contents/MacOS/Python'
    clock = [0.0]
    signals = []
    monkeypatch.setattr(client, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0]+seconds),
        strftime=time.strftime, gmtime=time.gmtime,
    ))

    class Child:
        def __init__(self, key, command):
            self.key, self.command = key, command
            self.pid = 12341 if key == 'ssh' else 12342
            self.returncode = None
            self.samples = 0
            self.replacement = None
        def poll(self):
            return self.returncode
        def terminate(self):
            signals.append(self.key)
            self.returncode = 0
        def wait(self, timeout):
            assert self.returncode is not None
            return self.returncode

    real_popen, real_run = subprocess.Popen, subprocess.run

    def popen(command, **kwargs):
        key = 'ssh' if command[0] == 'ssh' else 'gateway' if command[0] == cfg['python'] else None
        if key is None:
            return real_popen(command, **kwargs)
        child = Child(key, command)
        children[key] = child
        return child

    def run(command, **kwargs):
        if command[0] != 'ps':
            return real_run(command, **kwargs)
        child = next(item for item in children.values() if item.pid == int(command[command.index('-p')+1]))
        if child.returncode is not None and startup != 'exited':
            return subprocess.CompletedProcess(command, 1, '', '')
        child.samples += 1
        prefix, args = initial, list(child.command)
        if child.key == 'gateway' and (
            startup == 'never_complete' or startup.startswith('incomplete') and child.samples == 1
        ):
            return subprocess.CompletedProcess(command, 0, 'Rs '+prefix+'(python3.12)\n', '')
        if child.key == 'gateway' and (startup == 'stable' or child.samples > 1):
            args[0] = final_python
            if startup in {'changed_start', 'incomplete_changed_start'}: prefix = 'Sun Oct 4 07:14:00 2026 '
            if startup == 'changed_args': args.append('--different-process')
        if child.replacement == 'start': prefix = 'Sun Oct 4 07:15:00 2026 '
        if child.replacement == 'args': args.append('--replaced')
        if child.replacement == 'executable': args[0] = '/fixture/replaced-python'
        if child.key == 'gateway' and startup == 'exited':
            child.returncode = 0  # The owned child exited; this PID can belong to another process.
        return subprocess.CompletedProcess(command, 0, 'Ss '+prefix+' '.join(args)+'\n', '')

    def health(port):
        if port == cfg['tunnel_port']:
            return dict(module='prediction_service', status='running', mode='shadow',
                        git_sha=sha, source_state='clean', pid=4321, started_at='backend-start',
                        schema_version='open_trader.prediction_service.health.v1',
                        release_schema_version=client.RELEASE_SCHEMA, reader_generation=2,
                        contract_generation=2, production_owner=False, mutations='prohibited',
                        first_violation=None, cwd='/opt/release', code_root='/opt/release/src')
        if startup == 'late_exit': children['gateway'].returncode = 0
        if startup == 'late_args': children['gateway'].replacement = 'args'
        return dict(pid=children['gateway'].pid, git_sha='b'*40 if startup == 'wrong_health' else sha,
                    source_state='clean', started_at='gateway-start', cwd=str(release),
                    code_root=str(release/'src'), prediction_only=True,
                    prediction_route_mode='service', prediction_upstream_status='ok')

    monkeypatch.setattr(subprocess, 'Popen', popen)
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(client, 'read_json', health)
    record = Path(cfg['runtime_root'])/'client.json'
    if startup not in {'exec', 'stable', 'incomplete'}:
        with pytest.raises(ValueError): client_operation(cfg, 'start')
        assert not record.exists()
        assert signals == (['ssh'] if startup in {'exited', 'late_exit'} else ['gateway', 'ssh'])
        return

    assert client_operation(cfg, 'start')['status'] == 'CONNECTED'
    saved = json.loads(record.read_text())
    assert saved['gateway']['identity'] == initial+final_python+' '+' '.join(children['gateway'].command[1:])
    assert client_operation(cfg, 'status')['status'] == 'CONNECTED'
    assert client_operation(cfg, 'start')['gateway_pid'] == children['gateway'].pid
    assert signals == []
    # Stable records must never refresh themselves to accept a changed process.
    for replacement in ('start', 'args', 'executable'):
        children['gateway'].replacement = replacement
        with pytest.raises(ValueError, match='PID identity changed'):
            client_operation(cfg, 'status')
        with pytest.raises(ValueError, match='PID identity changed'):
            client_operation(cfg, 'stop')
        assert json.loads(record.read_text()) == saved
        assert signals == []
    children['gateway'].replacement = None

    def kill(pid, sig):
        next(child for child in children.values() if child.pid == pid).terminate()

    monkeypatch.setattr(os, 'kill', kill)
    assert client_operation(cfg, 'stop')['status'] == 'STOPPED'
    assert signals == ['gateway', 'ssh']
    assert not record.exists()


@pytest.mark.parametrize("split", [False, True])
def test_client_start_status_stop_with_real_gateway_and_fake_ssh(tmp_path, monkeypatch, split, request):
    import os, shutil, socket, subprocess, sys
    from types import SimpleNamespace
    from timing_support import run_test_in_subprocess
    # loadgroup decorates nodeids; the supervised serial run needs the real node.
    node = SimpleNamespace(nodeid=request.node.nodeid.removesuffix('@prediction_cloud_ports'))
    if run_test_in_subprocess(SimpleNamespace(node=node), timeout=45):
        return
    with socket.socket() as gateway_socket, socket.socket() as tunnel_socket:
        gateway_socket.bind(('127.0.0.1',0)); tunnel_socket.bind(('127.0.0.1',0))
        gateway_port, tunnel_port = gateway_socket.getsockname()[1], tunnel_socket.getsockname()[1]
    release = (tmp_path/'release').resolve()
    src = release/'src/open_trader'
    src.mkdir(parents=True)
    original = Path(__file__).resolve().parents[1]
    for name in ('__init__.py','__main__.py','frontend_gateway.py'):
        shutil.copy(original/'src/open_trader'/name,src/name)
    shutil.copytree(original/'src/open_trader/dashboard_static',src/'dashboard_static')
    (release/'ops').mkdir()
    shutil.copy(original/'ops/prediction-service-release.json',release/'ops')
    for args in [('init','-q'),('add','.'),('-c','user.name=Test','-c','user.email=t@example.invalid','commit','-qm','fixture'),('checkout','--detach')]:
        subprocess.run(['git','-C',str(release),*args],check=True,capture_output=True)
    sha = subprocess.check_output(['git','-C',str(release),'rev-parse','HEAD'],text=True).strip()
    fakebin = tmp_path/'bin'; fakebin.mkdir()
    ssh = fakebin/'ssh'
    ssh.write_text(f'''#!{sys.executable}
import json,sys,os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
assert sys.argv[-1]=='open-trader-test'
assert '127.0.0.1:{tunnel_port}:127.0.0.1:8769' in sys.argv
assert 'ForwardAgent=no' in sys.argv and 'StrictHostKeyChecking=yes' in sys.argv
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  guard_path={str(tmp_path/'guard')!r};production_path={str(tmp_path/'production')!r}
  guard=Path(guard_path).exists();production=Path(production_path).exists()
  violation=None if not guard else 'guarded'
  body=json.dumps(dict(module='prediction_service',status='unavailable' if guard else 'running',mode='production' if production else 'shadow',git_sha={sha!r},source_state='clean',schema_version='open_trader.prediction_service.health.v1',release_schema_version='open_trader.prediction_service.release.v1',started_at='fixture-start',production_owner=production,mutations='enabled' if production else 'prohibited',first_violation=violation,pid=os.getpid(),cwd='/opt/release',code_root='/opt/release/src',reader_generation=2,contract_generation=2,n_leg=dict(status='paused',code='N_LEG_PAUSED'))).encode()
  status=503 if guard else 200;self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*a):pass
HTTPServer(('127.0.0.1',{tunnel_port}),Handler).serve_forever()
''')
    ssh.chmod(0o755)
    monkeypatch.setenv('PATH',str(fakebin)+os.pathsep+os.environ['PATH'])
    cfg = dict(release_root=str(release),runtime_root=str(tmp_path/'client'),python=sys.executable,
               ssh_alias='open-trader-test',expected_sha=sha,mode='shadow',
               gateway_port=gateway_port,tunnel_port=tunnel_port)
    air = None
    if split:
        import threading
        from tests.test_frontend_gateway import _Upstream
        air = _Upstream()
        air.health_body = json.dumps(dict(module='prediction_service',mode='production',
            production_owner=True,mutations='enabled',git_sha=sha,source_state='clean',pid=os.getpid())).encode()
        air.response_body = json.dumps(dict(mode='production',mutations='enabled',csrf_token='air-csrf')).encode()
        air_thread = threading.Thread(target=air.serve_forever,daemon=True)
        air_thread.start()
        cfg.update(execution_port=air.server_address[1],execution_expected_sha=sha)
    try:
        original_owner = socket.socket()
        original_owner.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        original_owner.bind(('127.0.0.1',0)); original_owner.listen()
        first = client_operation(cfg,'start')
        assert first['status']=='CONNECTED'
        assert client_operation(cfg,'start')['gateway_pid']==first['gateway_pid']
        assert client_operation(cfg,'status')['ssh_pid']==first['ssh_pid']
        if split:
            from urllib.request import urlopen
            with urlopen(f'http://127.0.0.1:{gateway_port}/') as response:
                html = response.read()
                assert b'data-prediction-split="true"' in html
                assert b'data-prediction-only="true"' not in html
            with urlopen(f'http://127.0.0.1:{gateway_port}/api/prediction-arbitrage/execution/identity') as response:
                assert json.load(response)['csrf_token'] == 'air-csrf'
            assert air.requests[-1]['path'] == '/api/prediction-arbitrage/venues'
        (tmp_path/'production').touch()
        with pytest.raises(ValueError,match='health identity'): client_operation(cfg,'status')
        (tmp_path/'production').unlink()
        (tmp_path/'guard').touch()
        with pytest.raises(ValueError,match='read API unavailable'): client_operation(cfg,'status')
        (tmp_path/'guard').unlink()
        init=src/'__init__.py'; saved=init.read_text(); init.write_text(saved+'\n# changed after startup\n')
        with pytest.raises(ValueError,match='dirty'): client_operation(cfg,'status')
        init.write_text(saved)
        assert client_operation(cfg,'stop')['status']=='STOPPED'
        if split:
            from open_trader.prediction_cloud import read_json
            assert read_json(cfg['execution_port'])['git_sha'] == sha
        assert client_operation(cfg,'stop')['status']=='STOPPED'
        assert client_operation(cfg,'status')['status']=='STOPPED'
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(('127.0.0.1',gateway_port));occupied.listen()
            with pytest.raises(OSError): client_operation(cfg,'start')
        assert original_owner.fileno() >= 0
        assert not (Path(cfg['runtime_root'])/'client.json').exists()
    finally:
        client_operation(cfg,'stop')
        original_owner.close()
        if air is not None:
            air.shutdown(); air.server_close(); air_thread.join(timeout=5)


def test_client_missing_pid_with_unknown_listener_retains_record(tmp_path, monkeypatch):
    import socket, subprocess, sys
    if sys.platform != 'linux':
        pytest.skip('fixed ports only in isolated Docker')
    root = tmp_path/'client'; root.mkdir(mode=0o700)
    cfg = dict(release_root=str(tmp_path/'release'),runtime_root=str(root),
               python=sys.executable,ssh_alias='test',expected_sha='a'*40,mode='shadow',
               gateway_port=8876,tunnel_port=8879)
    record = root/'client.json'
    record.write_text(json.dumps(dict(config=cfg,gateway=dict(pid=99999999,identity='gone'))))
    def forbidden(*a,**k): raise AssertionError('must not signal unknown listener')
    monkeypatch.setattr('os.kill',forbidden)
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('127.0.0.1',8876)); listener.listen()
        with pytest.raises(OSError): client_operation(cfg,'stop')
        assert record.exists()
    assert client_operation(cfg,'stop')['status']=='STOPPED'
    assert not record.exists()


def test_process_identity_keeps_the_entire_long_command(monkeypatch):
    import select, subprocess, sys
    from open_trader.prediction_client import process_identity
    monkeypatch.setenv('COLUMNS','80')
    marker='identity-tail-'+'x'*200
    process=subprocess.Popen(
        [sys.executable,'-c','import sys; print("ready", flush=True); sys.stdin.read()',marker],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        # Test command width after exec, not the launcher's transient ps record.
        assert select.select([process.stdout], [], [], 5)[0], 'child startup watchdog'
        assert process.stdout.readline().strip() == 'ready'
        assert process_identity(process.pid).endswith(marker)
    finally:
        process.terminate();process.wait(timeout=5)


def test_process_identity_rechecks_a_torn_ps_exit_snapshot(monkeypatch):
    import subprocess
    from open_trader.prediction_client import same_process
    # Observed on Linux: cmdline disappears while the sampled status is still R.
    sleeps = []
    monkeypatch.setattr('open_trader.prediction_client.time.sleep', sleeps.append)
    responses=iter(['Rs Tue Sep 29 10:40:37 2026 [python]\n',
                    'Zs Tue Sep 29 10:40:37 2026 [python] <defunct>\n'])
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a,0,next(responses),''))
    assert not same_process({'pid':123,'identity':'Tue Sep 29 10:40:37 2026 /python owned-client'})
    assert sleeps == [0.05]


def test_split_client_validates_independent_air_cloud_identities_and_ports(tmp_path, monkeypatch):
    import open_trader.prediction_client as client
    base = dict(release_root=str(tmp_path/'release'),runtime_root=str(tmp_path/'client'),
        python='/usr/bin/python3',ssh_alias='test',expected_sha='a'*40,mode='shadow',
        execution_port=8769,execution_expected_sha='b'*40,cloud_expected_sha='c'*40)
    validate(base)
    assert client_ports(base) == (8766,8879)
    for change in ({'tunnel_port':8769}, {'execution_port':True}, {'mode':'production'}, {'execution_expected_sha':''}):
        with pytest.raises(ValueError): validate({**base, **change})
    state = {'gateway':{'pid':4242,'identity':'owned'},'ssh':{'pid':4243,'identity':'owned'}}
    monkeypatch.setattr(client,'same_process',lambda _:True)
    monkeypatch.setattr(client,'client_release',lambda c:{'reader_generation':2,'contract_generation':2})
    gateway = dict(pid=4242,git_sha='a'*40,source_state='clean',started_at='fixture',
        prediction_only=False,prediction_split=True,prediction_display_upstream_port=8879,
        prediction_upstream_status='ok',prediction_route_mode='service',
        cwd=base['release_root'],code_root=base['release_root']+'/src')
    cloud = dict(module='prediction_service',status='running',git_sha='c'*40,source_state='clean',
        mode='shadow',production_owner=False,mutations='prohibited',first_violation=None,
        n_leg={'status':'paused'},pid=4243,started_at='fixture',cwd='/opt/release',code_root='/opt/release/src',
        schema_version='open_trader.prediction_service.health.v1',release_schema_version=client.RELEASE_SCHEMA,
        reader_generation=2,contract_generation=2)
    air = dict(module='prediction_service',mode='production',production_owner=True,mutations='enabled',
        git_sha='b'*40,source_state='clean',pid=4244)
    def reader(port): return {8766:gateway,8879:cloud,8769:air}[port]
    monkeypatch.setattr(client,'read_json',reader)
    result = status(base,state)
    assert result['execution_git_sha'] == 'b'*40 and result['cloud_git_sha'] == 'c'*40
    air['git_sha']='d'*40
    with pytest.raises(ValueError,match='Air execution identity'): status(base,state)
    def offline(port):
        if port == 8769: raise OSError('offline')
        return reader(port)
    monkeypatch.setattr(client,'read_json',offline)
    assert status(base,state)['execution_status'] == 'unavailable'
