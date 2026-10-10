"""Installed command contracts at the executable/process/filesystem boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'scripts/order_probe_launcher.py'


def executable(path, text):
    path.write_text('#!' + sys.executable + '\n' + text)
    path.chmod(0o700)
    return str(path)


@pytest.fixture
def installation(tmp_path):
    profile = tmp_path / 'tokyo.json'
    remote = tmp_path / 'remote.json'
    log = tmp_path / 'ssh.log'
    remote.write_text(json.dumps(dict(sha='a' * 40, release='/opt/open-trader/releases/' + 'a' * 40)))
    ssh = executable(tmp_path / 'ssh', f'''import json, pathlib, sys
args=sys.argv[1:]
script=sys.stdin.read()
pathlib.Path({str(log)!r}).write_text(json.dumps(dict(args=args,script=script)))
state=json.loads(pathlib.Path({str(remote)!r}).read_text())
if state.get('failure'):
    sys.exit(255)
print(json.dumps(dict(result='PASS',single_writer='not_claimed',selected_market='Synthetic',outcome='Yes',notional='0.0500',order_id='probe-1',region={{'status':'blocked'}},evidence_path='/var/lib/open-trader/prediction/order-probe/probe-synthetic.json',release_sha=state['sha'],release_root=state['release'])))
''')
    profile.write_text(json.dumps(dict(schema='open_trader.lpprobe.tokyo.v1', ssh=ssh,
        host='open-trader-tokyo-experiment', bootstrap='/usr/bin/python3',
        cloud_config='/etc/open-trader/prediction-cloud.json', user='prediction',
        runtime_root='/var/lib/open-trader/prediction',
        credentials_file='/var/lib/open-trader/prediction-credentials/polymarket.json')))
    profile.chmod(0o600)
    git = executable(tmp_path / 'git', f'''import pathlib, sys
args=sys.argv[1:]
if 'rev-parse' in args: print('b'*40)
elif 'cat-file' in args: sys.stdout.buffer.write(pathlib.Path({str(LAUNCHER)!r}).read_bytes())
else: sys.exit(1)
''')
    bin_dir, payload = tmp_path / 'bin', tmp_path / 'payload'
    args = [sys.executable, '-I', '-B', str(LAUNCHER), 'install', '--profile', str(profile),
            '--launcher-python', sys.executable, '--git', git, '--bin-dir', str(bin_dir), '--payload-dir', str(payload)]
    return dict(args=args, binary=bin_dir / 'lpprobe', payload=payload, profile=profile,
                remote=remote, log=log, tmp=tmp_path)


def run(args):
    return subprocess.run([str(a) for a in args], env={'HOME': str(Path.home()), 'PATH': '/usr/bin:/bin'},
                          stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=15)


def test_installed_tokyo_command_needs_no_environment_or_user_input(installation, monkeypatch):
    setup = installation
    installed = run(setup['args'])
    assert installed.returncode == 0, installed.stdout + installed.stderr
    command = run([setup['binary'], 'tokyo'])
    assert command.returncode == 0, command.stdout + command.stderr
    report = json.loads(command.stdout)
    assert report['result'] == 'PASS' and report['single_writer'] == 'not_claimed'
    assert report['region']['status'] == 'blocked'
    call = json.loads(setup['log'].read_text())
    assert 'BatchMode=yes' in call['args'] and 'StrictHostKeyChecking=yes' in call['args']
    assert 'ForwardAgent=no' in call['args']
    assert 'open-trader-tokyo-experiment' in call['args']
    assert 'sudo -n /usr/bin/python3 -I -B -' == call['args'][-1]
    assert 'runuser' in call['script'] and 'self-test' in call['script']
    assert 'confirm-single-writer' not in call['script']
    assert 'prediction-cloud.json' in call['script']
    assert 'signing-private-key' not in call['script']
    assert report['release_sha'] == 'a' * 40
    setup['remote'].write_text(json.dumps(dict(sha='c' * 40, release='/opt/open-trader/releases/' + 'c' * 40)))
    switched = run([setup['binary'], 'tokyo'])
    assert switched.returncode == 0
    assert json.loads(switched.stdout)['release_sha'] == 'c' * 40
    receipt = json.loads((setup['payload'] / 'installation.json').read_text())
    assert receipt['binary'] == str(setup['binary'])
    assert receipt['source_sha'] == 'b' * 40
    assert setup['profile'].read_bytes() == (setup['payload'] / 'tokyo.json').read_bytes()
    for sha in ('a' * 40, 'c' * 40):
        code, commands = remote_boundary(monkeypatch, json.loads(setup['profile'].read_text()), sha)
        assert code == 0
        assert len(commands) == 1
        assert commands[0][:5] == ['/usr/sbin/runuser', '-u', 'prediction', '--', '/usr/bin/env']
        assert '/opt/open-trader/venvs/' + sha + '/bin/python' in commands[0]
        assert 'PYTHONPATH=/opt/open-trader/releases/' + sha + '/src' in commands[0]
        assert 'self-test' in commands[0]


@pytest.mark.parametrize('fault', ['missing_profile', 'profile_mismatch', 'missing_release',
    'mismatched_release', 'connection', 'symlink_target', 'unrelated_executable',
    'tampered_installation', 'receipt_creation', 'unsafe_receipts', 'service_owned_record',
    'hardlinked_record', 'hardlinked_account'])
def test_launcher_fails_closed_and_preserves_unrelated_installation(installation, fault, monkeypatch, tmp_path, capsys):
    setup = installation
    if fault in ('service_owned_record', 'hardlinked_record', 'hardlinked_account'):
        code, commands = remote_boundary(monkeypatch, json.loads(setup['profile'].read_text()), 'a' * 40, fault)
        assert code == 2 and commands == []
        assert json.loads(capsys.readouterr().out)['reason'] == 'REMOTE_RELEASE_UNVERIFIED'
        return
    if fault == 'missing_profile': setup['profile'].unlink()
    elif fault == 'profile_mismatch':
        data = json.loads(setup['profile'].read_text()); data['host'] = 'untrusted; echo unsafe'
        setup['profile'].write_text(json.dumps(data))
    elif fault == 'symlink_target':
        setup['binary'].parent.mkdir()
        target = setup['tmp'] / 'unrelated'; target.write_text('keep')
        setup['binary'].symlink_to(target)
    elif fault == 'unrelated_executable':
        setup['binary'].parent.mkdir()
        setup['binary'].write_text('keep')
    if fault in ('missing_profile', 'profile_mismatch', 'symlink_target', 'unrelated_executable'):
        result = run(setup['args'])
        assert result.returncode == 2
        assert json.loads(result.stdout)['result'] == 'BLOCKED'
        assert not setup['log'].exists()
        if fault in ('symlink_target', 'unrelated_executable'): assert setup['binary'].read_text() == 'keep'
        assert not setup['payload'].exists()
        return
    if fault in ('receipt_creation', 'unsafe_receipts'):
        from test_polymarket_order_probe import SelfExchange, PRIVATE, SIGNER, SECRET
        import httpx
        import importlib
        import socket
        import time
        venue = SelfExchange()
        monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda _, request: venue.handle(request))
        monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('real network forbidden'))
        monkeypatch.setattr(time, 'time', lambda: 1700000000.0)
        credentials_dir = tmp_path / 'credentials'; credentials_dir.mkdir(mode=0o700)
        credentials = credentials_dir / 'synthetic.json'
        credentials.write_text(json.dumps({'com.open-trader.polymarket': {
            'signing-private-key': PRIVATE, 'builder-key': 'synthetic-builder', 'builder-secret': SECRET,
            'builder-passphrase': 'synthetic-pass'}})); credentials.chmod(0o600)
        account = tmp_path / 'account.json'
        account.write_text(json.dumps(dict(signer_address=SIGNER, wallet_address=SIGNER)))
        receipts = tmp_path / 'receipts'
        if fault == 'unsafe_receipts':
            receipts.symlink_to(credentials_dir, target_is_directory=True)
        module = importlib.import_module('open_trader.polymarket_order_probe')
        code = module.main(['self-test', '--config', str(account), '--credential-backend', 'file',
                            '--credentials-file', str(credentials), '--receipt-dir', str(receipts)])
        report = json.loads(capsys.readouterr().out)
        if fault == 'receipt_creation':
            assert code == 0, report
            assert receipts.stat().st_mode & 0o777 == 0o700
            assert all(p.stat().st_mode & 0o777 == 0o600 for p in receipts.iterdir())
        else:
            assert code == 2 and report['result'] == 'BLOCKED'
            assert venue.requests == []
            assert list(credentials_dir.iterdir()) == [credentials]
        return
    if fault in ('missing_release', 'mismatched_release'):
        code, commands = remote_boundary(monkeypatch, json.loads(setup['profile'].read_text()), 'a' * 40, fault)
        assert code == 2 and commands == []
        report = json.loads(capsys.readouterr().out)
        assert report['reason'] == 'REMOTE_RELEASE_UNVERIFIED'
    assert run(setup['args']).returncode == 0
    if fault == 'connection':
        setup['remote'].write_text(json.dumps(dict(failure=True)))
    elif fault == 'tampered_installation':
        (setup['payload'] / 'tokyo.json').write_text('{}')
    else:
        # Trusted SSH process boundary reports failed remote metadata verification.
        ssh = json.loads(setup['profile'].read_text())['ssh']
        executable(Path(ssh), "import json,sys\nsys.stdin.read()\nprint(json.dumps({'result':'BLOCKED','reason':'REMOTE_RELEASE_UNVERIFIED'}))\nsys.exit(2)\n")
    result = run([setup['binary'], 'tokyo'])
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report['result'] in ('BLOCKED', 'UNKNOWN')
    assert report['reason'] in ('REMOTE_RELEASE_UNVERIFIED', 'SSH_CONNECTION_UNKNOWN', 'INSTALLATION_MISMATCH')
    assert 'keep' not in result.stdout
    if fault == 'tampered_installation': assert not setup['log'].exists()


def remote_boundary(monkeypatch, profile_data, sha, fault=None, runtime_sha=None, directory_links=True, record_owner=0):
    """Run the transmitted resolver against synthetic OS/filesystem/process facts."""
    import importlib.util
    import io
    import pathlib
    import pwd
    import signal
    import stat
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('probe_launcher_contract', LAUNCHER)
    launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)
    root = '/opt/open-trader/releases/' + sha
    python = '/opt/open-trader/venvs/' + (runtime_sha or sha) + '/bin/python'
    runtime = '/var/lib/open-trader/prediction'
    config = dict(user='prediction', runtime_root=runtime, credential_backend='file',
                  credentials_file=profile_data['credentials_file'], release_root=root, python=python, expected_sha=sha)
    unit = '\n'.join(['User=prediction', 'Group=prediction', 'WorkingDirectory=' + root,
        'Environment=PYTHONPATH=' + root + '/src', 'ExecStart=' + python + ' -m open_trader prediction-service --mode shadow'])
    saved = dict(schema_version='open_trader.prediction_service.runtime.v1', manager='systemd', state='stopped',
                 candidate=dict(checkout=root, git_sha='d' * 40 if fault == 'mismatched_release' else sha), unit_text=unit)
    files = {profile_data['cloud_config']: json.dumps(config).encode(),
             runtime + '/prediction-systemd-release.json': json.dumps(saved).encode(),
             '/etc/systemd/system/open-trader-prediction.service': unit.encode()}
    class HostPath:
        def __init__(self, path): self.value = str(path)
        def __str__(self): return self.value
        def __truediv__(self, other): return HostPath(self.value + '/' + str(other))
        def __eq__(self, other): return str(self) == str(other)
        def is_absolute(self): return self.value.startswith('/')
        @property
        def parts(self): return pathlib.PurePosixPath(self.value).parts
        @property
        def parents(self): return [HostPath(str(p)) for p in pathlib.PurePosixPath(self.value).parents]
        @property
        def parent(self): return HostPath(str(pathlib.PurePosixPath(self.value).parent))
        def lstat(self):
            if fault == 'missing_release' and self.value == root: raise FileNotFoundError()
            service = self.value == runtime or self.value.startswith(runtime + '/') or self.value == profile_data['credentials_file']
            mode = stat.S_IFDIR | (0o700 if service else 0o755)
            if self.value in files or self.value.endswith('.json'): mode = stat.S_IFREG | 0o600
            if self.value.endswith('.service'): mode = stat.S_IFREG | 0o644
            if self.value == python: mode = stat.S_IFLNK | 0o777
            if self.value == '/usr/bin/python3': mode = stat.S_IFREG | 0o755
            owner = 123 if service else 0
            links = 1
            if directory_links and self.value == runtime: links = 4
            if directory_links and self.value == runtime + '/config': links = 2
            if self.value == runtime + '/prediction-systemd-release.json':
                owner = 123 if fault == 'service_owned_record' else record_owner
                if fault == 'hardlinked_record': links = 2
            if self.value == runtime + '/config/prediction_arbitrage.json' and fault == 'hardlinked_account': links = 2
            return SimpleNamespace(st_uid=owner, st_mode=mode, st_nlink=links)
        def resolve(self, strict=False): return HostPath('/usr/bin/python3') if self.value == python else self
        def rglob(self, pattern): return [HostPath(python)] if self.value == python.rsplit('/bin/', 1)[0] else []
        def open(self, mode): return io.BytesIO(files[self.value])
        def read_text(self): return files[self.value].decode()
    executed = []
    def execute(path, argv):
        executed.append(argv)
        raise SystemExit(0)
    def git_process(argv, **kwargs):
        assert argv[:3] == ['/usr/bin/git', '--no-optional-locks', '-C']
        assert argv[3] == root
        tail = argv[4:]
        output = 'HEAD' if '--abbrev-ref' in tail else sha
        if tail[0] == 'status': output = ''
        if tail[0] == 'ls-files': output = 'src/open_trader/polymarket_order_probe.py'
        return SimpleNamespace(returncode=0, stdout=output)
    with monkeypatch.context() as context:
        context.setattr(pathlib, 'Path', HostPath)
        context.setattr(os, 'geteuid', lambda: 0)
        context.setattr(pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=123))
        context.setattr(os, 'execv', execute)
        context.setattr(subprocess, 'run', git_process)
        context.setattr(signal, 'signal', lambda *a: None)
        context.setattr(signal, 'alarm', lambda *a: None)
        with pytest.raises(SystemExit) as result:
            exec(launcher.REMOTE + '\nremote(' + repr(profile_data) + ')', {})
        return result.value.code, executed


def test_installed_tokyo_command_accepts_configured_lock_matched_runtime_identity(installation, monkeypatch):
    p = json.loads(installation['profile'].read_text())
    code, commands = remote_boundary(monkeypatch, p, 'a' * 40,
        runtime_sha='2797cd5029c7812e9405825674f559e7d1ebafaa')
    assert code == 0
    assert len(commands) == 1
    assert commands[0][:5] == ['/usr/sbin/runuser', '-u', 'prediction', '--', '/usr/bin/env']
    assert '/opt/open-trader/venvs/2797cd5029c7812e9405825674f559e7d1ebafaa/bin/python' in commands[0]
    assert 'PYTHONPATH=/opt/open-trader/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/src' in commands[0]
    assert '/opt/open-trader/venvs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bin/python' not in commands[0]


@pytest.mark.parametrize('directory_links', [True, False], ids=['normal_directory_links', 'root_control_record'])
def test_remote_resolver_accepts_actual_directory_and_control_record_ownership(installation, monkeypatch, directory_links):
    p = json.loads(installation['profile'].read_text())
    code, commands = remote_boundary(monkeypatch, p, 'a' * 40,
        runtime_sha='2797cd5029c7812e9405825674f559e7d1ebafaa', directory_links=directory_links, record_owner=0)
    assert code == 0
    assert commands[0][:5] == ['/usr/sbin/runuser', '-u', 'prediction', '--', '/usr/bin/env']
    assert '/opt/open-trader/venvs/2797cd5029c7812e9405825674f559e7d1ebafaa/bin/python' in commands[0]
    assert 'PYTHONPATH=/opt/open-trader/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/src' in commands[0]
