"""Stable entry contracts at process, Git, OS-tool and HTTP boundaries."""
from copy import deepcopy
from datetime import datetime
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import resource
import shutil
import signal
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / 'scripts/lp_auto_launcher.py'
PYTHON = sys.executable  # The explicitly selected test runner, preserving venv spelling.
LABEL = 'com.open-trader.prediction-service'
ROOT = '/api/prediction-arbitrage/lp/auto/'
START = '2026-10-09T01:02:03+00:00'


def process(args, **kwargs):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'GIT_'))}
    return subprocess.run(args, capture_output=True, text=True, timeout=25, env=env, **kwargs)


def git(root, *args):
    result = process(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-C', str(root), *args])
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


STUB = '''import json, os, sys, urllib.request
from pathlib import Path
marker = MARKER
args = sys.argv[1:]
log = Path(LOG)
with log.open('a') as handle: handle.write(json.dumps({'marker': marker, 'argv': args, 'python': sys.executable, 'cwd': os.getcwd()}) + '\\n')
if MODE == 'forward':
    print('{"fixture":"literal output"}')
    print('fixture diagnostic', file=sys.stderr)
    raise SystemExit(17)
url = args[args.index('--url') + 1]
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url + '/api/prediction-arbitrage/lp/auto/state', timeout=10) as response:
    state = json.load(response)
print(json.dumps({'result': 'STATUS', 'state': state, 'marker': marker}))
'''


@pytest.fixture
def managed(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    control = SimpleNamespace(
        state={'desired_running': False, 'pause_confirmed': True, 'runtime_state': 'paused',
               'budget_usd': '100', 'target_buy_count': 5, 'buy_price_level': 2, 'config_version': 7},
        orders=['buy-1', 'sell-1'], requests=[], health={}, health_hook=None,
        block=False, disconnect=False, entered=threading.Event(), release=threading.Event(),
        csrf='fixture-csrf-secret', cookie='fixture-cookie-secret',
    )

    class Handler(BaseHTTPRequestHandler):
        @property
        def control(self):
            return self.server.control

        def log_message(self, *args):
            pass

        def respond(self, body, cookie=False):
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Content-Type', 'application/json')
            if cookie:
                self.send_header('Set-Cookie', f'session={self.control.cookie}; Path=/')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.control.requests.append(('GET', self.path, None, dict(self.headers)))
            if self.path == '/healthz':
                health = deepcopy(self.control.health)
                if self.control.health_hook:
                    self.control.health_hook()
                self.respond(health)
            elif self.path.endswith('/venues'):
                self.respond({'csrf_token': self.control.csrf}, cookie=True)
            elif self.path == ROOT + 'state':
                if self.control.block:
                    self.control.entered.set()
                    assert self.control.release.wait(15), 'independent state barrier watchdog expired'
                self.respond(self.control.state)
            else:
                self.send_error(404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.control.requests.append(('POST', self.path, body, dict(self.headers)))
            if (self.headers.get('Cookie') != f'session={self.control.cookie}'
                    or self.headers.get('X-CSRF-Token') != self.control.csrf
                    or self.headers.get('Origin') != self.control.url):
                self.send_error(403)
                return
            if self.path == ROOT + 'config':
                self.control.state.update({k: v for k, v in body.items() if k != 'expected_config_version'})
                self.control.state['config_version'] += 1
            elif self.path == ROOT + 'enable':
                self.control.state.update(desired_running=True, pause_confirmed=False, runtime_state='blocked')
            elif self.path == ROOT + 'pause':
                self.control.state.update(desired_running=False, pause_confirmed=True, runtime_state='paused')
            else:
                self.send_error(404)
                return
            self.control.entered.set()
            if self.control.disconnect:
                assert self.control.release.wait(15), 'independent disconnect barrier watchdog expired'
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.respond(self.control.state)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.control = control
    assert server.server_port != 8769
    control.url = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tool_state = tmp_path / 'observations.json'
    child_log = tmp_path / 'children.jsonl'
    tools = {}
    for name in ('launchctl', 'lsof', 'ps'):
        tool = tmp_path / name
        tool.write_text(f'''#!{PYTHON}
import json, sys
import socket
from pathlib import Path
s = json.loads(Path({str(tool_state)!r}).read_text())
name = {name!r}
if name == 'launchctl':
    if s.get('barrier'):
        barrier = s.pop('barrier')
        Path({str(tool_state)!r}).write_text(json.dumps(s))
        with socket.create_connection(('127.0.0.1', barrier), timeout=15) as connection:
            connection.sendall(b'entered')
            assert connection.recv(2) == b'go', 'external publication barrier watchdog expired'
    def quote(value): return '"' + value + '"' if s.get('quoted') else value
    print('gui/501/{LABEL} = {{')
    print(' path = ' + quote(s['plist']))
    print(' working directory = ' + quote(s['cwd']))
    print(' arguments = {{')
    for arg in s['args']: print('  ' + quote(arg))
    print(' }}')
    print(' pid = ' + str(s['pid']))
    print(' }}')
elif name == 'ps': print('Fri Oct  9 01:02:00 2026')
elif '-d' in sys.argv: print('p' + str(s['pid']) + '\\nn' + s['cwd'])
elif any(a.startswith('-iTCP:') for a in sys.argv): print('p' + str(s['listener_pid']) + '\\nf3\\nn' + s['listener'])
else: print(s['owner_pid'])
''')
        tool.chmod(0o755)
        tools[name] = str(tool)
    runtime = tmp_path / 'runtime'
    (runtime / 'data/prediction_arbitrage').mkdir(parents=True)
    (runtime / 'data/prediction_arbitrage/runtime.lock').write_bytes(b'owner-lock')
    record = runtime / 'prediction-service-runtime.json'
    plist = tmp_path / f'{LABEL}.plist'
    lock = tmp_path / f'.{LABEL}.release.lock'
    lock.write_bytes(b'operation-lock')
    releases = {}

    def make_release(marker, real=False, mode='status'):
        root = tmp_path / ('release-' + marker)
        root.mkdir()
        if real:
            shutil.copytree(REPO / 'src', root / 'src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        else:
            package = root / 'src/open_trader'
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('')
            (package / '__main__.py').write_text(STUB.replace('MARKER', repr(marker)).replace('LOG', repr(str(child_log))).replace('MODE', repr(mode)))
        (root / 'ops').mkdir()
        (root / 'ops/prediction-service-release.json').write_text(json.dumps({
            'schema_version': 'open_trader.prediction_service.release.v1',
            'reader_generation': 2, 'contract_generation': 2}))
        git(root, 'init', '-q')
        git(root, 'add', '.')
        git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', marker)
        sha = git(root, 'rev-parse', 'HEAD')
        git(root, 'checkout', '--detach', '-q')
        releases[marker] = (root, sha)
        return root, sha

    def select(marker, pid=43210):
        root, sha = releases[marker]
        manifest = str(root / 'ops/prediction-service-release.json')
        args = [PYTHON, '-m', 'open_trader', 'prediction-service', '--mode', 'production',
                '--data-dir', str(runtime / 'data'), '--config', str(runtime / 'not-read.json'),
                '--host', '127.0.0.1', '--port', str(server.server_port), '--release-manifest', manifest]
        plist.write_bytes(plistlib.dumps({'Label': LABEL, 'WorkingDirectory': str(root),
                                         'EnvironmentVariables': {'PYTHONPATH': str(root / 'src')},
                                         'ProgramArguments': args}))
        tool_state.write_text(json.dumps({'plist': str(plist), 'cwd': str(root), 'args': args,
                                         'pid': pid, 'listener_pid': pid, 'owner_pid': pid,
                                         'listener': f'127.0.0.1:{server.server_port}'}))
        record.write_text(json.dumps({'schema_version': 'open_trader.prediction_service.runtime.v1',
                                     'state': 'ready', 'candidate': {'checkout': str(root), 'git_sha': sha,
                                     'source_state': 'clean', 'manifest': manifest,
                                     'reader_generation': 2, 'contract_generation': 2},
                                     'ready': {'pid': pid, 'process_started_at': START}}))
        control.health = {'schema_version': 'open_trader.prediction_service.health.v1',
                          'module': 'prediction_service', 'status': 'running', 'mode': 'production',
                          'production_owner': True, 'mutations': 'enabled', 'pid': pid,
                          'cwd': str(root), 'code_root': str(root / 'src'), 'git_sha': sha,
                          'source_state': 'clean', 'started_at': START,
                          'release_schema_version': 'open_trader.prediction_service.release.v1',
                          'reader_generation': 2, 'contract_generation': 2}

    make_release('A')
    select('A')
    launcher_source = tmp_path / 'launcher-source'
    (launcher_source / 'scripts').mkdir(parents=True)
    shutil.copyfile(INSTALLER, launcher_source / 'scripts/lp_auto_launcher.py')
    git(launcher_source, 'init', '-q')
    git(launcher_source, 'add', '.')
    git(launcher_source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'candidate launcher')
    launcher_sha = git(launcher_source, 'rev-parse', 'HEAD')
    fixture = SimpleNamespace(home=home, control=control, record=record, plist=plist, lock=lock,
                              tools=tools, tool_state=tool_state, runtime=runtime, releases=releases,
                              make_release=make_release, select=select, child_log=child_log,
                              bin=home / 'bin', payload=home / 'payload',
                              launcher_source=launcher_source / 'scripts/lp_auto_launcher.py', launcher_sha=launcher_sha,
                              handler=Handler)
    try:
        yield fixture
    finally:
        control.release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def installation_args(fixture, mode='init', source=None, extra=()):
    source = source or fixture.launcher_source
    assert source.is_file(), 'stable launcher installer is missing'
    args = [PYTHON, '-I', '-B', str(source), mode, '--launcher-python', PYTHON,
            '--bin-dir', str(fixture.bin), '--payload-dir', str(fixture.payload), '--plist', str(fixture.plist)]
    for tool, path in fixture.tools.items():
        args += ['--' + tool, path]
    return [*args, *extra]


def install(fixture, mode='init', source=None, extra=(), **kwargs):
    return process(installation_args(fixture, mode, source, extra), **kwargs)


def invoke(fixture, *args, **kwargs):
    return process([str(fixture.bin / 'lpauto'), *args], **kwargs)


def posts(fixture):
    return [(path, body) for method, path, body, _ in fixture.control.requests if method == 'POST']


def children(fixture):
    return [json.loads(line) for line in fixture.child_log.read_text().splitlines()] if fixture.child_log.exists() else []


def installed(fixture):
    result = install(fixture)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('target', ['valid-target', 'unverifiable-target'])
def test_init_is_repeatable_and_never_controls_service(managed, target):
    before = (managed.plist.read_bytes(), managed.record.read_bytes(), deepcopy(managed.control.state), list(managed.control.orders))
    if target == 'unverifiable-target':
        other, _ = managed.make_release('B')
        managed.control.health['code_root'] = str(other / 'src')
        result = install(managed)
        assert result.returncode == 2, result.stdout + result.stderr
        assert 'DEPLOYED_TARGET_UNVERIFIABLE' in result.stderr
        assert not (managed.bin / 'lpauto').exists()
        assert all(not (managed.payload / name).exists() for name in ('launcher.py', 'config.json', 'receipt.json'))
        assert posts(managed) == []
        assert all(path == '/healthz' for _, path, _, _ in managed.control.requests)
        assert before == (managed.plist.read_bytes(), managed.record.read_bytes(), managed.control.state, managed.control.orders)
        return
    installed(managed)
    config = (managed.payload / 'config.json').read_bytes()
    installed(managed)
    assert (managed.payload / 'config.json').read_bytes() == config
    assert {p.name for p in managed.bin.iterdir()} == {'lpauto'}
    assert {p.name for p in managed.payload.iterdir()} == {'launcher.py', 'config.json', 'receipt.json'}
    assert posts(managed) == []
    assert before == (managed.plist.read_bytes(), managed.record.read_bytes(), managed.control.state, managed.control.orders)
    assert managed.control.state['desired_running'] is False
    assert managed.control.state['config_version'] == 7


def test_failed_first_guard_publication_is_recoverable(managed, tmp_path):
    managed.bin.mkdir(parents=True)
    managed.payload.mkdir()
    home_note, bin_note = managed.home / 'sentinel.txt', managed.bin / 'sentinel.txt'
    home_note.write_bytes(b'unrelated home sentinel')
    bin_note.write_bytes(b'unrelated bin sentinel')
    runtime_config = managed.runtime / 'independent-config.json'
    runtime_config.write_bytes(b'{"budget":"100","desired_running":false}')
    sentinels = {path: path.read_bytes() for path in (home_note, bin_note)}
    before = (managed.plist.read_bytes(), managed.record.read_bytes(), runtime_config.read_bytes(),
              deepcopy(managed.control.state), list(managed.control.orders))
    wrapper = tmp_path / 'guard-limited-installer.py'
    wrapper.write_text('import os, resource, signal, sys\n'
                       'resource.setrlimit(resource.RLIMIT_FSIZE, (1, 1))\n'
                       'signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n'
                       'os.execv(sys.argv[1], sys.argv[1:])\n')
    failed = process([PYTHON, '-I', '-B', str(wrapper), *installation_args(managed)])
    assert failed.returncode != 0, 'external one-byte limit must interrupt first guard publication'
    print('FAILED_FIRST_GUARD', failed.returncode, failed.stdout, failed.stderr)
    print('GUARD_ARTIFACTS_AFTER_FAILURE', {str(path.relative_to(managed.home)): path.read_bytes()
                                          for path in managed.home.rglob('*.lock') if path.is_file()})
    assert not (managed.bin / 'lpauto').exists()
    assert all(not (managed.payload / name).exists() for name in ('launcher.py', 'config.json', 'receipt.json'))
    assert posts(managed) == [] and children(managed) == []
    assert all(path == '/healthz' for _, path, _, _ in managed.control.requests)
    assert before == (managed.plist.read_bytes(), managed.record.read_bytes(), runtime_config.read_bytes(),
                      managed.control.state, managed.control.orders)
    assert all(path.read_bytes() == raw for path, raw in sentinels.items())

    # Remove only the external file-size limit: do not delete/adopt a guard.
    result = install(managed)
    assert result.returncode == 0, result.stdout + result.stderr
    status = invoke(managed, 'status', '--json')
    assert status.returncode == 0, status.stdout + status.stderr
    assert json.loads(status.stdout)['marker'] == 'A'
    assert json.loads(status.stdout)['state'] == before[3]
    assert install(managed, 'uninstall').returncode == 0
    assert not (managed.bin / 'lpauto').exists()
    assert all(not (managed.payload / name).exists() for name in ('launcher.py', 'config.json', 'receipt.json'))
    assert all(path.read_bytes() == raw for path, raw in sentinels.items())

    # An unrelated malformed marker is still a collision, never recovery input.
    guards = sorted(managed.home.rglob('*.lock'))
    assert len(guards) == 2
    guard = guards[0]
    guard.write_bytes(b'unknown pre-existing guard artifact')
    guard_identity = guard.stat().st_dev, guard.stat().st_ino
    guard_bytes = {path: path.read_bytes() for path in guards}
    requests = deepcopy(managed.control.requests)
    refusal = install(managed)
    assert refusal.returncode == 2, refusal.stdout + refusal.stderr
    assert all(path.exists() and path.read_bytes() == raw for path, raw in guard_bytes.items())
    assert (guard.stat().st_dev, guard.stat().st_ino) == guard_identity
    assert managed.control.requests == requests and posts(managed) == []
    assert not (managed.bin / 'lpauto').exists()
    assert all(not (managed.payload / name).exists() for name in ('launcher.py', 'config.json', 'receipt.json'))
    assert before == (managed.plist.read_bytes(), managed.record.read_bytes(), runtime_config.read_bytes(),
                      managed.control.state, managed.control.orders)
    assert all(path.read_bytes() == raw for path, raw in sentinels.items())


@pytest.mark.parametrize('manager', ['plain-manager', 'quoted-manager'])
def test_fresh_terminal_is_cwd_and_source_independent(managed, tmp_path, manager):
    if manager == 'quoted-manager':
        root, sha = managed.releases['A']
        spaced = root.with_name('release A')
        root.rename(spaced)
        managed.releases['A'] = (spaced, sha)
        managed.select('A')
    installed(managed)
    if manager == 'quoted-manager':
        observations = json.loads(managed.tool_state.read_text())
        observations['quoted'] = True
        managed.tool_state.write_text(json.dumps(observations))
    hostile = tmp_path / 'hostile'
    (hostile / 'open_trader').mkdir(parents=True)
    (hostile / 'open_trader/__init__.py').write_text('')
    (hostile / 'open_trader/__main__.py').write_text('print("HOSTILE")')
    (hostile / '.zshrc').write_text('')
    (hostile / '.zprofile').write_text('')
    env = dict(os.environ, PYTHONPATH=str(hostile), PYTHONHOME=str(hostile), PYTHONUSERBASE=str(hostile))
    result = subprocess.run([str(managed.bin / 'lpauto'), 'status', '--json'], cwd=hostile,
                            env=env, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['marker'] == 'A'
    assert 'HOSTILE' not in result.stdout + result.stderr
    assert children(managed) == [{'marker': 'A', 'argv': ['prediction-arb', 'lp-auto', 'status', '--json', '--url', managed.control.url],
                                  'python': PYTHON, 'cwd': str(managed.releases['A'][0])}]


def test_help_and_status_are_read_only(managed):
    installed(managed)
    state, orders = deepcopy(managed.control.state), list(managed.control.orders)
    managed.control.requests.clear()
    help_result = invoke(managed, '--help')
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    for name in ('status', 'config', 'on', 'off', 'pause'):
        assert name in help_result.stdout
    assert managed.control.requests == []
    result = invoke(managed, 'status', '--json')
    assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document['result'] == 'STATUS'
    assert document['state']['budget_usd'] == '100'
    assert document['state']['target_buy_count'] == 5
    assert document['state']['config_version'] == 7
    assert posts(managed) == []
    assert managed.control.state == state and managed.control.orders == orders


@pytest.mark.parametrize('source_state', ['committed-source', 'modified-source', 'untracked-source', 'staged-source'])
def test_version_reports_launcher_and_deployment_separately(managed, source_state):
    source, source_sha = managed.make_release('C')
    (source / 'scripts').mkdir()
    shutil.copyfile(INSTALLER, source / 'scripts/lp_auto_launcher.py')
    git(source, 'add', 'scripts')
    git(source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'launcher C')
    source_sha = git(source, 'rev-parse', 'HEAD')
    script = source / 'scripts/lp_auto_launcher.py'
    before = (managed.plist.read_bytes(), managed.record.read_bytes(), deepcopy(managed.control.state), list(managed.control.orders))
    if source_state in ('modified-source', 'staged-source'):
        script.write_bytes(script.read_bytes() + b'\n# harmless uncommitted fixture change\n')
        if source_state == 'staged-source':
            git(source, 'add', 'scripts/lp_auto_launcher.py')
    elif source_state == 'untracked-source':
        script = source / 'untracked-launcher.py'
        shutil.copyfile(INSTALLER, script)
    result = install(managed, source=script)
    if source_state != 'committed-source':
        assert result.returncode == 2, result.stdout + result.stderr
        assert 'LAUNCHER_SOURCE_UNVERIFIABLE' in result.stderr
        assert not (managed.bin / 'lpauto').exists()
        assert all(not (managed.payload / name).exists() for name in ('launcher.py', 'config.json', 'receipt.json'))
        assert posts(managed) == [] and children(managed) == []
        assert all(path == '/healthz' for _, path, _, _ in managed.control.requests)
        assert before == (managed.plist.read_bytes(), managed.record.read_bytes(), managed.control.state, managed.control.orders)
        return
    assert result.returncode == 0, result.stdout + result.stderr
    result = invoke(managed, '--version', '--json')
    assert result.returncode == 0, result.stdout + result.stderr
    version = json.loads(result.stdout)
    root, sha = managed.releases['A']
    assert version['launcher']['source_sha'] == source_sha
    assert version['launcher']['source_sha256'] == __import__('hashlib').sha256(INSTALLER.read_bytes()).hexdigest()
    assert version['deployment']['sha'] == sha != source_sha
    assert version['deployment']['root'] == str(root)
    assert version['deployment']['code_root'] == str(root / 'src')
    assert version['deployment']['interpreter'] == PYTHON
    assert '0.1.0' not in result.stdout
    assert posts(managed) == [] and children(managed) == []


def test_next_invocation_follows_prediction_upgrade(managed):
    installed(managed)
    managed.make_release('B')
    gateway, gateway_sha = managed.make_release('C')
    (managed.runtime / 'gateway-service-runtime.json').write_text(json.dumps({'checkout': str(gateway), 'git_sha': gateway_sha}))
    first = invoke(managed, 'status', '--json')
    assert first.returncode == 0 and json.loads(first.stdout)['marker'] == 'A'
    assert json.loads(invoke(managed, '--version', '--json').stdout)['deployment']['sha'] == managed.releases['A'][1]
    managed.select('B')
    second = invoke(managed, 'status', '--json')
    assert second.returncode == 0 and json.loads(second.stdout)['marker'] == 'B'
    assert json.loads(invoke(managed, '--version', '--json').stdout)['deployment']['sha'] == managed.releases['B'][1]
    assert [child['marker'] for child in children(managed)] == ['A', 'B']
    assert posts(managed) == []


@pytest.mark.parametrize('fault', ['absent-record', 'malformed-record', 'not-ready', 'absent-interpreter',
                                'absent-source', 'wrong-sha', 'wrong-code-root', 'wrong-manifest',
                                'dirty-source', 'hidden-source', 'ignored-source', 'manager', 'listener', 'health-nonobject', 'ready-nonobject'])
def test_unverifiable_target_fails_without_fallback(managed, fault):
    installed(managed)
    managed.make_release('B')
    root, _ = managed.releases['A']
    record = json.loads(managed.record.read_text())
    plist = plistlib.loads(managed.plist.read_bytes())
    observations = json.loads(managed.tool_state.read_text())
    if fault == 'absent-record':
        managed.record.unlink()
    elif fault == 'malformed-record':
        managed.record.write_text('{')
    elif fault == 'not-ready':
        record['state'] = 'maintenance'
        managed.record.write_text(json.dumps(record))
    elif fault == 'absent-interpreter':
        plist['ProgramArguments'][0] = str(root / 'missing-python')
        managed.plist.write_bytes(plistlib.dumps(plist))
    elif fault == 'absent-source':
        shutil.rmtree(root / 'src')
    elif fault == 'wrong-sha':
        record['candidate']['git_sha'] = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
        managed.record.write_text(json.dumps(record))
    elif fault == 'wrong-code-root':
        managed.control.health['code_root'] = str(managed.releases['B'][0] / 'src')
    elif fault == 'wrong-manifest':
        plist['ProgramArguments'][-1] = str(managed.releases['B'][0] / 'ops/prediction-service-release.json')
        managed.plist.write_bytes(plistlib.dumps(plist))
    elif fault in ('dirty-source', 'hidden-source'):
        path = root / 'src/open_trader/__main__.py'
        if fault == 'hidden-source':
            git(root, 'update-index', '--assume-unchanged', 'src/open_trader/__main__.py')
        path.write_text('print("WRONG")')
    elif fault == 'ignored-source':
        (root / '.git/info/exclude').write_text('src/open_trader/hidden.py\n')
        (root / 'src/open_trader/hidden.py').write_text('print("HIDDEN")')
    elif fault == 'manager':
        observations['cwd'] = str(managed.releases['B'][0])
        managed.tool_state.write_text(json.dumps(observations))
    elif fault == 'listener':
        observations['listener_pid'] = 99999
        managed.tool_state.write_text(json.dumps(observations))
    elif fault == 'health-nonobject':
        managed.control.health = []
    elif fault == 'ready-nonobject':
        record['ready'] = []
        managed.record.write_text(json.dumps(record))
    record_bytes = managed.record.read_bytes() if managed.record.exists() else None
    state = deepcopy(managed.control.state)
    result = invoke(managed, 'status', '--json')
    assert result.returncode == 2, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document['result'] == 'UNKNOWN' and document['state'] is None
    assert document['reason'] == 'DEPLOYED_TARGET_UNVERIFIABLE'
    assert document['next_action']
    assert children(managed) == [] and posts(managed) == []
    assert (managed.record.read_bytes() if managed.record.exists() else None) == record_bytes
    assert all(path == '/healthz' for _, path, _, _ in managed.control.requests)
    assert managed.control.state == state and managed.control.orders == ['buy-1', 'sell-1']


def test_stale_record_disagrees_with_current_release(managed):
    installed(managed)
    old = managed.record.read_bytes()
    managed.make_release('B')
    managed.select('B')
    managed.record.write_bytes(old)
    result = invoke(managed, 'on', '--json')
    assert result.returncode == 2, result.stdout + result.stderr
    assert json.loads(result.stdout)['result'] == 'UNKNOWN'
    assert managed.record.read_bytes() == old
    assert posts(managed) == [] and children(managed) == []


def test_same_release_restart_uses_fresh_identity(managed):
    installed(managed)
    saved = managed.record.read_bytes()
    managed.select('A', pid=54321)
    managed.control.health['started_at'] = '2026-10-09T01:03:03+00:00'
    managed.record.write_bytes(saved)
    result = invoke(managed, '--version', '--json')
    assert result.returncode == 0, result.stdout + result.stderr
    deployment = json.loads(result.stdout)['deployment']
    assert deployment['sha'] == managed.releases['A'][1]
    assert deployment['saved_observation_current'] is False
    assert deployment['pid'] == 54321
    assert managed.record.read_bytes() == saved and posts(managed) == []
    managed.make_release('B')
    managed.select('B', pid=54321)
    managed.record.write_bytes(saved)
    result = invoke(managed, 'on', '--json')
    assert result.returncode == 2 and json.loads(result.stdout)['result'] == 'UNKNOWN'
    assert children(managed) == [] and posts(managed) == []


@pytest.mark.parametrize('fault,reason', [('exclusive', 'DEPLOYMENT_IN_PROGRESS'), ('missing', 'DEPLOYED_TARGET_UNVERIFIABLE')])
def test_deployment_lock_prevents_selection_during_switch(managed, fault, reason):
    installed(managed)
    lock = managed.lock.open('rb')
    try:
        if fault == 'exclusive':
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            managed.lock.unlink()
        result = invoke(managed, 'status', '--json')
        assert result.returncode == 2, result.stdout + result.stderr
        document = json.loads(result.stdout)
        assert document['result'] == 'UNKNOWN' and document['state'] is None
        assert document['reason'] == reason
        assert children(managed) == [] and posts(managed) == []
        if fault == 'missing':
            assert not managed.lock.exists()
    finally:
        lock.close()


def exclusive_probe(path):
    code = '''import fcntl, sys
with open(sys.argv[1], 'rb') as handle:
    try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit(9)
'''
    return process([PYTHON, '-I', '-B', '-c', code, str(path)]).returncode


def test_delegate_holds_read_only_release_lock_until_exit(managed):
    installed(managed)
    before = managed.lock.read_bytes()
    managed.control.block = True
    child = subprocess.Popen([str(managed.bin / 'lpauto'), 'status', '--json'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert managed.control.entered.wait(15), 'child must reach explicit HTTP state barrier'
        assert exclusive_probe(managed.lock) == 9
        managed.control.release.set()
        stdout, stderr = child.communicate(timeout=15)
        assert child.returncode == 0, stdout + stderr
        assert exclusive_probe(managed.lock) == 0
        assert managed.lock.read_bytes() == before
    finally:
        managed.control.release.set()
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize('changed', ['record', 'manager'])
def test_identity_change_during_probe_fails(managed, changed):
    installed(managed)
    setup_probes = len([r for r in managed.control.requests if r[1] == '/healthz'])
    def change():
        if changed == 'record':
            document = json.loads(managed.record.read_text())
            document['ready']['pid'] = 99999
            managed.record.write_text(json.dumps(document))
        else:
            document = json.loads(managed.tool_state.read_text())
            document['pid'] = 99999
            managed.tool_state.write_text(json.dumps(document))
    managed.control.health_hook = change
    result = invoke(managed, 'on', '--json')
    assert result.returncode == 2, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document['result'] == 'UNKNOWN' and document['state'] is None
    assert children(managed) == [] and posts(managed) == []
    assert len([r for r in managed.control.requests if r[1] == '/healthz']) == setup_probes + 1


def test_argv_stdout_stderr_and_exit_are_forwarded_once(managed):
    managed.make_release('F', mode='forward')
    managed.select('F')
    installed(managed)
    tail = ['config', '--budget', '100.00', '--target-buys', '5', '--bid-level', '2',
            '--url', managed.control.url, '--timeout', '15', '--json']
    result = invoke(managed, *tail)
    assert result.returncode == 17
    assert result.stdout == '{"fixture":"literal output"}\n'
    assert result.stderr == 'fixture diagnostic\n'
    assert len(children(managed)) == 1
    assert children(managed)[0]['argv'] == ['prediction-arb', 'lp-auto', *tail]
    assert posts(managed) == []


def test_isolated_controls_preserve_existing_contract(managed):
    managed.make_release('REAL', real=True)
    managed.select('REAL')
    installed(managed)
    orders = list(managed.control.orders)
    results = []
    for tail in (['config', '--budget', '100.00', '--target-buys', '5', '--bid-level', '2'], ['on'], ['off'], ['pause']):
        result = invoke(managed, *tail, '--json')
        assert result.returncode == 0, result.stdout + result.stderr
        assert managed.control.csrf not in result.stdout + result.stderr
        assert managed.control.cookie not in result.stdout + result.stderr
        results.append(json.loads(result.stdout))
    assert [document['result'] for document in results] == ['CONFIGURED', 'ON', 'PAUSED', 'PAUSED']
    assert results[0]['state']['config_version'] == 8
    assert results[0]['state']['desired_running'] is False
    assert results[1]['state']['desired_running'] is True
    assert results[1]['state']['runtime_state'] == 'blocked'
    assert results[2]['state']['desired_running'] is False and results[3]['state']['desired_running'] is False
    assert posts(managed) == [(ROOT + 'config', {'budget_usd': '100.00', 'target_buy_count': 5,
                                              'buy_price_level': 2, 'expected_config_version': 7}),
                              (ROOT + 'enable', {'confirm': True}),
                              (ROOT + 'pause', {'confirm': True}), (ROOT + 'pause', {'confirm': True})]
    assert managed.control.orders == orders == ['buy-1', 'sell-1']
    for method, _, _, headers in managed.control.requests:
        if method == 'POST':
            assert headers['Cookie'] == 'session=fixture-cookie-secret'
            assert headers['X-Csrf-Token'] == 'fixture-csrf-secret'
            assert headers['Origin'] == managed.control.url


def test_isolated_writes_are_not_retried(managed):
    managed.make_release('REAL', real=True)
    managed.select('REAL')
    installed(managed)
    managed.control.disconnect = True
    child = subprocess.Popen([str(managed.bin / 'lpauto'), 'on', '--json'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert managed.control.entered.wait(15), 'accepted-write barrier not reached'
        assert managed.control.state['desired_running'] is True
        managed.control.release.set()
        stdout, stderr = child.communicate(timeout=15)
        assert child.returncode == 2, stdout + stderr
        document = json.loads(stdout)
        assert document['result'] == 'UNKNOWN' and document['state'] is None
        assert posts(managed) == [(ROOT + 'enable', {'confirm': True})]
        assert managed.control.state['desired_running'] is True
        assert managed.control.orders == ['buy-1', 'sell-1']
        assert 'fixture-csrf-secret' not in stdout + stderr and 'fixture-cookie-secret' not in stdout + stderr
    finally:
        managed.control.release.set()
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize('tail', [
    ['status', '--url', 'http://192.0.2.1:9999'],
    ['status', '--url', 'http://user:password@127.0.0.1:9999'],
    ['status', '--url', 'http://127.0.0.1:9999/extra'],
    ['status', '--url', 'https://127.0.0.1:9999'],
    ['status', '--timeout', 'nan'],
    ['config', '--budget', '100', '--target-buys', '5', '--bid-level', '3'],
    ['on', '--unknown-lp-option'],
    ['status', '--url', 'SECOND_LISTENER'],
    ['on', 'SHORT_UR'], ['on', 'SHORT_U'], ['on', 'SHORT_UR_EQUALS'], ['on', 'SHORT_U_EQUALS'],
], ids=['nonloopback', 'credentials', 'extra-path', 'https', 'nonfinite-timeout', 'bad-bid', 'unknown-option', 'wrong-managed-loopback',
        'full-then-short-ur', 'full-then-short-u', 'full-then-short-ur-equals', 'full-then-short-u-equals'])
def test_url_and_argument_failures_never_reach_control(managed, tail):
    trap = None
    trap_thread = None
    trap_requests = []
    secondary = None
    if tail[-1] == 'SECOND_LISTENER':
        class Trap(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                trap_requests.append(('GET', self.path))
                raw = b'{"trap":true}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                trap_requests.append(('POST', self.path))
                self.send_error(400)

        trap = ThreadingHTTPServer(('127.0.0.1', 0), Trap)
        assert trap.server_port != 8769
        trap_thread = threading.Thread(target=trap.serve_forever, daemon=True)
        trap_thread.start()
        tail = [*tail[:-1], f'http://127.0.0.1:{trap.server_port}']
    else:
        managed.make_release('REAL', real=True)
        managed.select('REAL')
        if tail[-1] in ('SHORT_UR', 'SHORT_U', 'SHORT_UR_EQUALS', 'SHORT_U_EQUALS'):
            secondary = SimpleNamespace(state=deepcopy(managed.control.state), orders=list(managed.control.orders),
                                        requests=[], health={}, health_hook=None, block=False, disconnect=False,
                                        entered=threading.Event(), release=threading.Event(),
                                        csrf='second-csrf-secret', cookie='second-cookie-secret')
            trap = ThreadingHTTPServer(('127.0.0.1', 0), managed.handler)
            assert trap.server_port != 8769
            trap.control = secondary
            secondary.url = f'http://127.0.0.1:{trap.server_port}'
            trap_thread = threading.Thread(target=trap.serve_forever, daemon=True)
            trap_thread.start()
            option = '--ur' if tail[-1] in ('SHORT_UR', 'SHORT_UR_EQUALS') else '--u'
            short = [option + '=' + secondary.url] if tail[-1].endswith('EQUALS') else [option, secondary.url]
            tail = ['on', '--url', managed.control.url, *short]
            secondary_before = deepcopy(secondary.state), list(secondary.orders)
    installed(managed)
    state = deepcopy(managed.control.state)
    try:
        result = invoke(managed, *tail, '--json')
        if secondary and secondary.requests:
            print('SHORT_URL_BYPASS', result.stdout, [(method, path, body) for method, path, body, _ in secondary.requests], secondary.state)
            assert [(path, body) for method, path, body, _ in secondary.requests if method == 'POST'] == [(ROOT + 'enable', {'confirm': True})], 'bypass fixture must demonstrate real accepted control, not an authentication failure'
            assert secondary.state['desired_running'] is True
        assert result.returncode == 2, result.stdout + result.stderr
        document = json.loads(result.stdout)
        assert document['result'] == 'UNKNOWN' and document['state'] is None and document['reason']
        assert all(path == '/healthz' for _, path, _, _ in managed.control.requests)
        assert posts(managed) == [] and managed.control.state == state
        assert managed.control.orders == ['buy-1', 'sell-1']
        if trap:
            assert children(managed) == []
            assert trap_requests == []
        if secondary:
            assert secondary.requests == []
            assert (secondary.state, secondary.orders) == secondary_before
    finally:
        if trap:
            trap.shutdown()
            trap.server_close()
            trap_thread.join(5)
            assert not trap_thread.is_alive()


@pytest.mark.parametrize('second_mode', ['repair', 'uninstall'])
def test_concurrent_installation_management_preserves_owned_state(managed, tmp_path, second_mode):
    installed(managed)
    note = managed.payload / 'operator-note.txt'
    note.write_bytes(b'preserve unrelated operator file')
    runtime_config = managed.runtime / 'independent-config.json'
    runtime_config.write_bytes(b'{"budget":"100","desired_running":false}')
    runtime_before = (managed.plist.read_bytes(), managed.record.read_bytes(), runtime_config.read_bytes(),
                      deepcopy(managed.control.state), list(managed.control.orders))
    bootstrap_b, bootstrap_c = tmp_path / 'bootstrap-B', tmp_path / 'bootstrap-C'
    bootstrap_b.symlink_to(PYTHON)
    bootstrap_c.symlink_to(PYTHON)
    entered, release = threading.Event(), threading.Event()
    barrier_errors = []
    barrier = socket.socket()
    barrier.bind(('127.0.0.1', 0))
    assert barrier.getsockname()[1] != 8769
    barrier.listen(1)
    barrier.settimeout(15)

    def synchronize():
        try:
            connection, _ = barrier.accept()
            with connection:
                connection.settimeout(15)
                assert connection.makefile('rb').read(7) == b'entered'
                entered.set()
                assert release.wait(15), 'independent first-installer barrier watchdog expired'
                connection.sendall(b'go')
        except BaseException as exc:
            barrier_errors.append(exc)
            entered.set()

    barrier_thread = threading.Thread(target=synchronize, daemon=True)
    barrier_thread.start()
    observations = json.loads(managed.tool_state.read_text())
    observations['barrier'] = barrier.getsockname()[1]
    managed.tool_state.write_text(json.dumps(observations))
    first = subprocess.Popen(installation_args(managed, 'repair', extra=['--launcher-python', str(bootstrap_b)]),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert entered.wait(15), 'first repair must reach external validation barrier'
        assert not barrier_errors, barrier_errors
        artifacts = {path: path.read_bytes() for path in [managed.bin / 'lpauto',
                     *[managed.payload / name for name in ('launcher.py', 'config.json', 'receipt.json')]]}
        requests_before = deepcopy(managed.control.requests)
        result = install(managed, second_mode, extra=['--launcher-python', str(bootstrap_c)])
        print('SECOND_MANAGEMENT', result.returncode, result.stdout, result.stderr)
        print('SECOND_PROBES', managed.control.requests[len(requests_before):])
        assert result.returncode == 2, result.stdout + result.stderr
        assert 'INSTALLATION_IN_PROGRESS' in result.stdout + result.stderr
        assert managed.control.requests == requests_before
        assert all(path.exists() and path.read_bytes() == raw for path, raw in artifacts.items())
        assert not (managed.payload / '.transaction.json').exists()
        release.set()
        stdout, stderr = first.communicate(timeout=15)
        assert first.returncode == 0, stdout + stderr

        def coherent(bootstrap):
            receipt = json.loads((managed.payload / 'receipt.json').read_text())
            assert receipt['source_sha'] == managed.launcher_sha
            for filename, key in (('launcher.py', 'source_sha256'), ('config.json', 'config_sha256')):
                assert __import__('hashlib').sha256((managed.payload / filename).read_bytes()).hexdigest() == receipt[key]
            assert __import__('hashlib').sha256((managed.bin / 'lpauto').read_bytes()).hexdigest() == receipt['entry_sha256']
            assert json.loads((managed.payload / 'config.json').read_text())['bootstrap'] == str(bootstrap)
            assert not (managed.payload / '.transaction.json').exists()
            assert invoke(managed, '--version', '--json').returncode == 0
            status = invoke(managed, 'status', '--json')
            assert status.returncode == 0, status.stdout + status.stderr
            assert json.loads(status.stdout)['result'] == 'STATUS'

        coherent(bootstrap_b)
        guards = {path: path.read_bytes() for path in managed.home.rglob('*.lock')}
        assert len(guards) == 2
        assert all(path.is_file() and not path.is_symlink() and managed.bin not in path.parents
                   and managed.payload not in path.parents for path in guards)
        result = install(managed, second_mode, extra=['--launcher-python', str(bootstrap_c)])
        assert result.returncode == 0, result.stdout + result.stderr
        if second_mode == 'repair':
            coherent(bootstrap_c)
        else:
            assert not (managed.bin / 'lpauto').exists()
            assert {path.name for path in managed.payload.iterdir()} == {'operator-note.txt'}
        assert all(path.exists() and path.read_bytes() == raw for path, raw in guards.items())
        assert note.read_bytes() == b'preserve unrelated operator file'
        assert posts(managed) == []
        assert runtime_before == (managed.plist.read_bytes(), managed.record.read_bytes(), runtime_config.read_bytes(),
                                  managed.control.state, managed.control.orders)
    finally:
        release.set()
        if first.poll() is None:
            first.communicate(timeout=15)
        barrier_thread.join(15)
        barrier.close()
        assert not barrier_thread.is_alive() and not barrier_errors, barrier_errors


@pytest.mark.parametrize('recovery', ['normal-lifecycle', 'interrupted-init-repair', 'interrupted-init-uninstall',
                                    'interrupted-repair-repair', 'interrupted-repair-uninstall'])
def test_uninstall_and_repair_only_manage_owned_files(managed, tmp_path, recovery):
    if recovery != 'normal-lifecycle':
        managed.bin.mkdir(parents=True)
        note = managed.bin / 'operator-note.txt'
        note.write_bytes(b'preserve unrelated operator file')
        runtime_config = managed.runtime / 'independent-config.json'
        runtime_config.write_bytes(b'{"budget":"100","desired_running":false}')
        before = (managed.plist.read_bytes(), managed.record.read_bytes(), runtime_config.read_bytes(),
                  deepcopy(managed.control.state), list(managed.control.orders))
        proposed_bootstrap = tmp_path / 'bootstrap-B'
        proposed_bootstrap.symlink_to(PYTHON)
        if recovery.startswith('interrupted-init'):
            wrapper = tmp_path / 'limited-installer.py'
            wrapper.write_text('import os, resource, signal, sys\n'
                               'resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))\n'
                               'signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n'
                               'os.execv(sys.argv[1], sys.argv[1:])\n')
            failed = process([PYTHON, '-I', '-B', str(wrapper), *installation_args(managed)])
            assert failed.returncode != 0, 'external file-size limit must interrupt initial publication'
        else:
            installed(managed)
            entry = managed.bin / 'lpauto'
            old_bytes, old_mode = entry.read_bytes(), entry.stat().st_mode
            aside = tmp_path / 'saved-old-entry'
            entered, release = threading.Event(), threading.Event()
            barrier = socket.socket()
            barrier.bind(('127.0.0.1', 0))
            assert barrier.getsockname()[1] != 8769
            barrier.listen(1)
            barrier.settimeout(15)
            barrier_errors = []
            def synchronize():
                try:
                    connection, _ = barrier.accept()
                    with connection:
                        connection.settimeout(15)
                        assert connection.recv(7) == b'entered'
                        entered.set()
                        assert release.wait(15), 'external publication barrier release watchdog expired'
                        connection.sendall(b'go')
                except BaseException as exc:
                    barrier_errors.append(exc)
                    entered.set()
            barrier_thread = threading.Thread(target=synchronize, daemon=True)
            barrier_thread.start()
            observations = json.loads(managed.tool_state.read_text())
            observations['barrier'] = barrier.getsockname()[1]
            managed.tool_state.write_text(json.dumps(observations))
            child = subprocess.Popen(installation_args(managed, 'repair', extra=['--launcher-python', str(proposed_bootstrap)]),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                assert entered.wait(15), 'management must reach explicit external OS-tool barrier'
                assert not barrier_errors, barrier_errors
                entry.rename(aside)
                entry.mkdir()
                release.set()
                stdout, stderr = child.communicate(timeout=15)
                failed = SimpleNamespace(returncode=child.returncode, stdout=stdout, stderr=stderr)
                assert failed.returncode != 0, 'entry directory must prevent repair publication'
            finally:
                release.set()
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)
                barrier_thread.join(15)
                barrier.close()
                assert not barrier_thread.is_alive() and not barrier_errors, barrier_errors
                if aside.exists():
                    entry.rmdir()
                    aside.rename(entry)
            assert entry.read_bytes() == old_bytes and entry.stat().st_mode == old_mode
        print('INTERRUPTED_MANAGEMENT', failed.returncode, failed.stdout, failed.stderr)
        print('PUBLICATION_ARTIFACTS', {str(path.relative_to(managed.home)): path.stat().st_size
                                      for path in managed.home.rglob('*') if path.is_file()})
        mode = recovery.rsplit('-', 1)[1]
        result = install(managed, mode, extra=['--launcher-python', str(proposed_bootstrap)])
        assert result.returncode == 0, result.stdout + result.stderr
        if mode == 'repair':
            version = invoke(managed, '--version', '--json')
            assert version.returncode == 0, version.stdout + version.stderr
            document = json.loads(version.stdout)
            assert document['launcher']['source_sha'] == managed.launcher_sha
            assert document['launcher']['source_sha256'] == __import__('hashlib').sha256(managed.launcher_source.read_bytes()).hexdigest()
            assert document['deployment']['sha'] == managed.releases['A'][1]
            assert managed.launcher_source.read_bytes() == (managed.payload / 'launcher.py').read_bytes()
            assert json.loads((managed.payload / 'config.json').read_text())['bootstrap'] == str(proposed_bootstrap)
            assert str(proposed_bootstrap) in (managed.bin / 'lpauto').read_text()
            assert {path.name for path in managed.payload.iterdir()} == {'launcher.py', 'config.json', 'receipt.json'}
        else:
            assert not (managed.bin / 'lpauto').exists()
            assert all(not (managed.payload / name).exists() for name in ('launcher.py', 'config.json', 'receipt.json'))
        assert note.read_bytes() == b'preserve unrelated operator file'
        assert before == (managed.plist.read_bytes(), managed.record.read_bytes(), runtime_config.read_bytes(),
                          managed.control.state, managed.control.orders)
        assert posts(managed) == [] and children(managed) == []
        return
    installed(managed)
    unrelated = managed.payload / 'operator-note.txt'
    unrelated.write_bytes(b'preserve unrelated file')
    runtime_config = managed.runtime / 'independent-config.json'
    runtime_config.write_bytes(b'{"budget":"100","desired_running":false}')
    before = (managed.plist.read_bytes(), managed.record.read_bytes(), runtime_config.read_bytes(),
              deepcopy(managed.control.state), list(managed.control.orders))
    root, _ = managed.releases['A']
    backup = tmp_path / 'source-backup'
    (root / 'src').rename(backup)
    result = install(managed, mode='uninstall')
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (managed.bin / 'lpauto').exists()
    assert {p.name for p in managed.payload.iterdir()} == {'operator-note.txt'}
    assert unrelated.read_bytes() == b'preserve unrelated file'
    backup.rename(root / 'src')
    # An unrelated directory is never adopted as this installation.
    result = install(managed)
    assert result.returncode == 2
    unrelated.unlink()
    bootstrap = tmp_path / 'bootstrap-python'
    bootstrap.symlink_to(PYTHON)
    result = install(managed, extra=['--launcher-python', str(bootstrap)])
    assert result.returncode == 0, result.stdout + result.stderr
    bootstrap.unlink()
    result = invoke(managed, 'status', '--json')
    assert result.returncode == 2
    assert json.loads(result.stdout)['reason'] == 'LAUNCHER_BOOTSTRAP_MISSING'
    result = install(managed, mode='repair')
    assert result.returncode == 0, result.stdout + result.stderr
    assert invoke(managed, '--version', '--json').returncode == 0
    assert install(managed, mode='uninstall').returncode == 0
    collision = managed.bin / 'lpauto'
    collision.write_bytes(b'unrelated command')
    assert install(managed).returncode == 2
    assert collision.read_bytes() == b'unrelated command'
    collision.unlink()
    victim = tmp_path / 'victim'
    victim.write_bytes(b'unrelated target')
    collision.symlink_to(victim)
    assert install(managed).returncode == 2
    assert victim.read_bytes() == b'unrelated target'
    assert before == (managed.plist.read_bytes(), managed.record.read_bytes(), runtime_config.read_bytes(),
                      managed.control.state, managed.control.orders)
    assert posts(managed) == []
