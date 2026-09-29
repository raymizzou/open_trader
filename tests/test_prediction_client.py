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


def test_client_start_status_stop_with_real_gateway_and_fake_ssh(tmp_path, monkeypatch):
    import os, shutil, socket, subprocess, sys
    if sys.platform != 'linux':
        pytest.skip('fixed production ports tested only in isolated Linux Docker')
    release = tmp_path/'release'
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
assert '127.0.0.1:8879:127.0.0.1:8769' in sys.argv
assert 'ForwardAgent=no' in sys.argv and 'StrictHostKeyChecking=yes' in sys.argv
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  guard_path={str(tmp_path/'guard')!r};production_path={str(tmp_path/'production')!r}
  guard=Path(guard_path).exists();production=Path(production_path).exists()
  violation=None if not guard else 'guarded'
  body=json.dumps(dict(module='prediction_service',status='unavailable' if guard else 'running',mode='production' if production else 'shadow',git_sha={sha!r},source_state='clean',schema_version='open_trader.prediction_service.health.v1',release_schema_version='open_trader.prediction_service.release.v1',started_at='fixture-start',production_owner=production,mutations='enabled' if production else 'prohibited',first_violation=violation,pid=os.getpid(),cwd='/opt/release',code_root='/opt/release/src',reader_generation=2,contract_generation=2)).encode()
  status=503 if guard else 200;self.send_response(status);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*a):pass
HTTPServer(('127.0.0.1',8879),Handler).serve_forever()
''')
    ssh.chmod(0o755)
    monkeypatch.setenv('PATH',str(fakebin)+os.pathsep+os.environ['PATH'])
    cfg = dict(release_root=str(release),runtime_root=str(tmp_path/'client'),python=sys.executable,
               ssh_alias='open-trader-test',expected_sha=sha,mode='shadow',
               gateway_port=8876,tunnel_port=8879)
    try:
        original_owner = socket.socket()
        original_owner.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        original_owner.bind(('127.0.0.1',8766)); original_owner.listen()
        first = client_operation(cfg,'start')
        assert first['status']=='CONNECTED'
        assert client_operation(cfg,'start')['gateway_pid']==first['gateway_pid']
        assert client_operation(cfg,'status')['ssh_pid']==first['ssh_pid']
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
        assert client_operation(cfg,'stop')['status']=='STOPPED'
        assert client_operation(cfg,'status')['status']=='STOPPED'
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(('127.0.0.1',8876));occupied.listen()
            with pytest.raises(OSError): client_operation(cfg,'start')
        assert original_owner.fileno() >= 0
        assert not (Path(cfg['runtime_root'])/'client.json').exists()
    finally:
        client_operation(cfg,'stop')
        original_owner.close()


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
    import subprocess, sys
    from open_trader.prediction_client import process_identity
    monkeypatch.setenv('COLUMNS','80')
    marker='identity-tail-'+'x'*200
    process=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)',marker])
    try:
        assert process_identity(process.pid).endswith(marker)
    finally:
        process.terminate();process.wait(timeout=5)


def test_process_identity_rechecks_a_torn_ps_exit_snapshot(monkeypatch):
    import subprocess
    from open_trader.prediction_client import same_process
    # Observed on Linux: cmdline disappears while the sampled status is still R.
    responses=iter(['Rs Tue Sep 29 10:40:37 2026 [python]\n',
                    'Zs Tue Sep 29 10:40:37 2026 [python] <defunct>\n'])
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a,0,next(responses),''))
    assert not same_process({'pid':123,'identity':'Tue Sep 29 10:40:37 2026 /python owned-client'})
