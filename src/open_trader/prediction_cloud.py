"""Explicit, fail-closed systemd operations for the Prediction-only release."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import grp
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import subprocess
import stat
import sys
import tempfile
import time

from .prediction_release import RELEASE_SCHEMA, inspect_prediction_release_checkout, write_prediction_runtime_record, load_prediction_runtime_record

UNIT = 'open-trader-prediction.service'
UNIT_PATH = Path('/etc/systemd/system') / UNIT
_HEALTH_MISSING = object()


@dataclass(frozen=True)
class CloudConfig:
    release_root: Path
    runtime_root: Path
    python: Path
    user: str
    expected_sha: str
    region: str
    secret: str
    version: str
    role: str
    mode: str
    n_leg_paused: int
    credential_backend: str | None = None
    credentials_file: str = ''

    @property
    def record(self):
        return self.runtime_root / 'prediction-systemd-release.json'

    @property
    def runtime_lock(self):
        return self.runtime_root / 'data/prediction_arbitrage/runtime.lock'


def load_config(path: Path) -> CloudConfig:
    data = json.loads(path.read_text())
    if data.get('mode') not in {'production', 'shadow'}:
        raise ValueError('cloud service mode must be explicit production or shadow')
    credential_fields = ('region', 'secret', 'version', 'role')
    if data.get('credential_backend') == 'file':
        data.update({field: data.get(field, '') for field in credential_fields})
    elif data.get('mode') == 'shadow' and data.get('n_leg_paused') == 1 and not any(
        field in data for field in credential_fields
    ):
        data.update({field:'' for field in credential_fields})
    elif any(field not in data for field in credential_fields):
        raise ValueError('credential references are required for this mode')
    for field in ('release_root', 'runtime_root', 'python'):
        data[field] = Path(data[field])
    cfg = CloudConfig(**data)
    render_unit(cfg)
    return cfg


def credential_backend(c: CloudConfig) -> str:
    if c.credential_backend is not None:
        return c.credential_backend
    return 'disabled' if c.mode == 'shadow' and c.n_leg_paused == 1 and not any(
        (c.region, c.secret, c.version, c.role)
    ) else 'tencent-ssm'


def trusted_root_path(path: Path) -> None:
    """Existing root-owned paths; sticky root-owned /tmp remains safe."""
    for part in (path, *path.parents):
        info = part.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0
            or (info.st_mode & 0o022 and not (part != path and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX))):
            raise ValueError('root-owned non-writable canonical path required')


def service_user(c: CloudConfig):
    user = pwd.getpwnam(c.user)
    if user.pw_uid == 0 or grp.getgrnam(c.user).gr_gid != user.pw_gid:
        raise ValueError('dedicated non-root user and matching primary group required')
    return user


def trusted_config(path: Path) -> None:
    trusted_root_path(path)
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError('cloud config must be a root-owned mode 0600 regular file')


def trusted_layout(c: CloudConfig) -> None:
    user = service_user(c)
    trusted_root_path(c.release_root)
    for path in c.release_root.rglob('*'):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('release tree must be root-owned and non-writable by service')
    # A standard venv Python symlink is allowed only to a trusted interpreter.
    trusted_root_path(c.python.parent)
    trusted_root_path(c.python.resolve(strict=True))
    prefix = c.python.parent.parent
    if (prefix/'pyvenv.cfg').exists():
        for path in prefix.rglob('*'):
            trusted_root_path(path.resolve(strict=True))
    trusted_root_path(c.runtime_root.parent)
    for path in (c.runtime_root, c.runtime_root/'config', c.runtime_root/'config/prediction_arbitrage.json'):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != user.pw_uid or info.st_mode & 0o077:
            raise ValueError('service-owned private runtime and configuration required')


def render_unit(c: CloudConfig) -> str:
    for path in (c.release_root, c.runtime_root, c.python):
        if not path.is_absolute() or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path)) or '..' in path.parts:
            raise ValueError('absolute paths without whitespace or systemd specifiers required')
    if c.release_root.is_relative_to(c.runtime_root) or c.runtime_root.is_relative_to(c.release_root):
        raise ValueError('release and runtime roots must be separate')
    for value in (c.user,):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
            raise ValueError('invalid cloud reference')
    if c.user == 'root':
        raise ValueError('dedicated non-root service user required')
    if c.mode not in {'production', 'shadow'}:
        raise ValueError('cloud service mode must be explicit production or shadow')
    if not re.fullmatch(r'[0-9a-f]{40}', c.expected_sha) or type(c.n_leg_paused) is not int or c.n_leg_paused not in (0, 1):
        raise ValueError('exact SHA and explicit N-leg setting required')
    backend = credential_backend(c)
    if backend not in {'disabled', 'tencent-ssm', 'file'}:
        raise ValueError('unsupported credential backend')
    if backend == 'disabled':
        if any((c.region, c.secret, c.version, c.role)):
            raise ValueError('credentialless paused Shadow must not configure credential references')
        if c.mode != 'shadow' or c.n_leg_paused != 1 or c.credentials_file:
            raise ValueError('disabled backend requires credentialless paused Shadow')
        credential_env = 'Environment=OPEN_TRADER_CREDENTIAL_BACKEND=disabled\n'
    elif backend == 'file':
        path = Path(c.credentials_file)
        if (c.mode != 'shadow' or c.n_leg_paused != 1 or any((c.region, c.secret, c.version, c.role))
            or not path.is_absolute() or '..' in path.parts
            or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path))
            or path.is_relative_to(c.release_root)):
            raise ValueError('file backend requires a separate paused Shadow credential path')
        credential_env = ('Environment=OPEN_TRADER_CREDENTIAL_BACKEND=file\n'
                          f'Environment=OPEN_TRADER_CREDENTIAL_FILE={path}\n')
    else:
        if c.credentials_file:
            raise ValueError('SSM backend cannot use a credential file')
        for value in (c.region, c.secret, c.version, c.role):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
                raise ValueError('invalid cloud reference')
        if c.version == 'SSM_Current':
            raise ValueError('dedicated user and pinned secret version required')
        credential_env = 'Environment=OPEN_TRADER_CREDENTIAL_BACKEND=tencent-ssm\n'
        credential_env += ''.join([
            f'Environment=OPEN_TRADER_SSM_REGION={c.region}\n',
            f'Environment=OPEN_TRADER_SSM_SECRET={c.secret}\n',
            f'Environment=OPEN_TRADER_SSM_VERSION={c.version}\n',
            f'Environment=OPEN_TRADER_SSM_ROLE={c.role}\n',
        ])
    return f'''[Unit]
Description=OpenTrader Prediction
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User={c.user}
Group={c.user}
WorkingDirectory={c.release_root}
Environment=PYTHONPATH={c.release_root}/src
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=GIT_CONFIG_COUNT=1
Environment=GIT_CONFIG_KEY_0=safe.directory
Environment=GIT_CONFIG_VALUE_0={c.release_root}
{credential_env}Environment=OPEN_TRADER_NLEG_PAUSED={c.n_leg_paused}
Environment=OPEN_TRADER_NLEG_PAUSED={c.n_leg_paused}
ExecStart={c.python} -m open_trader prediction-service --mode {c.mode} --data-dir {c.runtime_root}/data --config {c.runtime_root}/config/prediction_arbitrage.json --host 127.0.0.1 --port 8769 --release-manifest {c.release_root}/ops/prediction-service-release.json
Restart=on-failure
RestartSec=30
TimeoutStopSec=90
SendSIGKILL=no
KillMode=control-group
UMask=0077
LimitCORE=0
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths={c.runtime_root}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
'''


def run(*args: str, timeout=15) -> str:
    result = subprocess.run(args, check=False, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError(f'{args[0]} inspection failed')
    return result.stdout.strip()


def release_identity(c: CloudConfig) -> dict:
    identity = inspect_prediction_release_checkout(c.release_root)
    if identity['git_sha'] != c.expected_sha:
        raise ValueError('release SHA mismatch')
    if run('git', '-C', str(c.release_root), 'rev-parse', '--abbrev-ref', 'HEAD') != 'HEAD':
        raise ValueError('detached immutable release required')
    return identity


def unit_state() -> dict:
    output = run('systemctl', 'show', UNIT, '--no-pager',
                 '--property=LoadState,ActiveState,SubState,MainPID,FragmentPath,DropInPaths,ExecMainStartTimestamp,NeedDaemonReload,Environment,User,Group,WorkingDirectory')
    state = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if not {'LoadState', 'ActiveState', 'MainPID'} <= state.keys():
        raise ValueError('systemd state unknown')
    return state


def listener_pids() -> set[int]:
    # ss succeeds with no listeners; command failure must not mean an empty port.
    text = run('ss', '-H', '-ltnp', 'sport = :8769')
    pids = set()
    for line in text.splitlines():
        fields = line.split()
        matches = re.findall(r'pid=(\d+)', line)
        if len(fields) < 5 or fields[3] != '127.0.0.1:8769' or len(matches) != 1:
            raise ValueError('listener identity unknown or not loopback')
        pids.add(int(matches[0]))
    return pids


def lock_pids(c: CloudConfig) -> set[int]:
    if not c.runtime_lock.exists():
        return set()
    stat = c.runtime_lock.stat()
    owners = set()
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if '->' in fields:
            continue  # a blocked waiter is not an owner
        if len(fields) < 6:
            raise ValueError('kernel lock evidence malformed')
        major, minor, inode = fields[5].split(':')
        if (int(major, 16), int(minor, 16), int(inode)) == (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino):
            owners.add(int(fields[4]))
    return owners


def absent(c: CloudConfig) -> dict:
    state = unit_state()
    if state['ActiveState'] not in ('inactive', 'failed') or state['MainPID'] != '0':
        raise ValueError('stop the existing managed owner before installation/start')
    if listener_pids() or lock_pids(c):
        raise ValueError('listener or runtime owner remains')
    return state


def read_json(port: int, path: str = '/healthz') -> dict:
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('read API unavailable')
        value = json.loads(response.read())
        if not isinstance(value, dict):
            raise ValueError('invalid read API payload')
        return value
    finally:
        connection.close()


def read_status(port: int, path: str) -> int:
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        connection.request('GET', path)
        return connection.getresponse().status
    finally:
        connection.close()


def installed_unit(c: CloudConfig) -> None:
    trusted_root_path(UNIT_PATH)
    if UNIT_PATH.is_symlink() or UNIT_PATH.read_text() != render_unit(c):
        raise ValueError('managed unit does not match requested release/config')
    state = unit_state()
    expected_env = {line.removeprefix('Environment=') for line in render_unit(c).splitlines() if line.startswith('Environment=')}
    if (state.get('NeedDaemonReload') != 'no' or set(shlex.split(state.get('Environment',''))) != expected_env
        or state.get('User') != c.user or state.get('Group') != c.user
        or state.get('WorkingDirectory') != str(c.release_root)):
        raise ValueError('loaded unit configuration mismatch; reload/install required')
    if state.get('FragmentPath') != str(UNIT_PATH) or state.get('DropInPaths'):
        raise ValueError('unknown unit source or drop-in')


def live_identity(c: CloudConfig) -> dict:
    user = service_user(c)
    identity = release_identity(c)
    installed_unit(c)
    state = unit_state()
    pid = int(state['MainPID'])
    if state['ActiveState'] != 'active' or pid <= 0 or listener_pids() != {pid} or lock_pids(c) != {pid}:
        raise ValueError('systemd/listener/runtime-lock ownership mismatch')
    process = Path('/proc') / str(pid)
    if (process / 'cwd').resolve() != c.release_root.resolve():
        raise ValueError('process cwd mismatch')
    process_status = (process/'status').read_text().splitlines()
    for field, expected in (('Uid:',user.pw_uid),('Gid:',user.pw_gid)):
        line = next(line for line in process_status if line.startswith(field))
        if set(map(int,line.split()[1:])) != {expected}:
            raise ValueError('service user/group mismatch')
    environment = dict(item.split('=',1) for item in (process/'environ').read_bytes().decode().split('\0') if '=' in item)
    for line in render_unit(c).splitlines():
        if line.startswith('Environment='):
            key, value = line.removeprefix('Environment=').split('=',1)
            if environment.get(key) != value:
                raise ValueError('running credential/runtime environment mismatch')
    args = (process / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
    expected = render_unit(c).split('ExecStart=', 1)[1].splitlines()[0].split()
    if args != expected:
        raise ValueError('loaded command mismatch')
    health = read_json(8769)
    mode_identity = (
        dict(mode='shadow', production_owner=False, mutations='prohibited',
             first_violation=None)
        if c.mode == 'shadow' else
        dict(mode='production', production_owner=True, mutations='enabled')
    )
    required = dict(module='prediction_service', schema_version='open_trader.prediction_service.health.v1',
        pid=pid, git_sha=c.expected_sha, source_state='clean', cwd=str(c.release_root),
        status='running', **mode_identity, release_schema_version=RELEASE_SCHEMA,
        reader_generation=identity['reader_generation'], contract_generation=identity['contract_generation'])
    if (any((health[key] if key in health else _HEALTH_MISSING) != value
            for key, value in required.items())
        or not isinstance(health.get('started_at'),str) or not health['started_at']):
        raise ValueError('health release identity mismatch')
    if Path(str(health.get('code_root', ''))).resolve() != (c.release_root/'src').resolve():
        raise ValueError('loaded source root mismatch')
    if unit_state()['MainPID'] != str(pid):
        raise ValueError('owner changed during inspection')
    release_identity(c)  # Recheck after process and API observations.
    return {'pid': pid, 'git_sha': c.expected_sha, 'started_at': health.get('started_at'),
            'systemd_started_at': state.get('ExecMainStartTimestamp')}


def preflight(c: CloudConfig) -> None:
    release_identity(c)
    absent(c)
    trusted_layout(c)
    run(str(c.python), '-c', 'import sys; assert sys.version_info >= (3,12)' +
        ('; import tencentcloud.ssm.v20190923.ssm_client' if credential_backend(c) == 'tencent-ssm' else ''))
    from .prediction_arbitrage_store import read_minimum_reader_generation
    if read_minimum_reader_generation(c.runtime_root/'data') > release_identity(c)['reader_generation']:
        raise ValueError('release cannot read this database')
    # Credential access runs as the service identity, with only non-secret refs.
    references = [line.removeprefix('Environment=') for line in render_unit(c).splitlines()
                  if line.startswith('Environment=')]
    if run('runuser', '-u', c.user, '--', 'env', *references, 'git', '-C', str(c.release_root), 'rev-parse', 'HEAD') != c.expected_sha:
        raise ValueError('service user cannot verify release SHA')
    if credential_backend(c) != 'disabled':
        run('runuser', '-u', c.user, '--', 'env', *references, str(c.python), '-m', 'open_trader',
            'prediction-arb', 'wallet', 'read-auth', '--config',
            str(c.runtime_root/'config/prediction_arbitrage.json'),
            *(['--require-trading-region'] if c.mode == 'production' or not c.n_leg_paused else []), timeout=60)
    if os.statvfs(c.runtime_root).f_bavail * os.statvfs(c.runtime_root).f_frsize < 1024**3:
        raise ValueError('less than 1 GiB free runtime storage')


def record(c: CloudConfig, state: str, **extra):
    previous = load_prediction_runtime_record(c.record)
    candidate = {'checkout': str(c.release_root), 'git_sha': c.expected_sha}
    old = None if previous is None else (previous.get('previous_release')
        if previous.get('candidate') == candidate else previous.get('candidate'))
    write_prediction_runtime_record(c.record, dict(state=state, manager='systemd',
        candidate=candidate, previous_release=old, unit_text=render_unit(c), **extra))


def verified_record(c: CloudConfig, states: tuple[str, ...]) -> dict:
    saved = load_prediction_runtime_record(c.record)
    if (not saved or saved.get('manager') != 'systemd' or saved.get('state') not in states
        or saved.get('candidate') != {'checkout':str(c.release_root),'git_sha':c.expected_sha}
        or saved.get('unit_text') != render_unit(c)):
        raise ValueError('managed release transition record not verified')
    return saved


def display_snapshot_evidence(snapshot: dict) -> dict:
    """Prove a published background read without requiring derived USD facts."""
    from datetime import UTC, datetime
    from decimal import Decimal
    from .polymarket_lp_risk import _freshness
    if (snapshot.get("authenticated") is not True or snapshot.get("stale") is not False
        or any(not isinstance(snapshot.get(key), list) for key in ("orders", "positions", "recommendations"))):
        raise ValueError("cloud display account snapshot unavailable or stale")
    _freshness(snapshot.get("checked_at"), datetime.now(UTC), "account_facts", max_age=Decimal(60))
    rewards = snapshot.get("market_rewards") or {}
    return {
        "account": {key:snapshot.get(key) for key in
            ("checked_at", "last_success_at", "open_orders_complete", "positions_complete", "trades_complete")},
        "catalog": {"complete":snapshot.get("catalog_complete")},
        "candidates": {key:snapshot.get(key) for key in
            ("candidate_state", "candidate_checked_at", "candidate_last_success_at", "candidate_stale",
             "missing_metadata_condition_ids", "missing_book_token_ids")},
        "history": snapshot.get("preparation"),
        "rewards": {condition:{key:row.get(key) for key in ("state", "reason", "checked_at")}
            for condition,row in rewards.items() if isinstance(row,dict)},
    }


def operate(c: CloudConfig, action: str) -> dict:
    if action == 'preflight':
        preflight(c)
        return {'status': 'PRECHECK_OK', 'git_sha': c.expected_sha, 'mode': c.mode,
                'n_leg_paused': c.n_leg_paused, 'credential_backend': credential_backend(c)}
    if action == 'status' and unit_state()['MainPID'] == '0':
        absent(c)
        installed_unit(c)
        verified_record(c, ('stopped',))
        return {'status': 'STOPPED', 'git_sha': c.expected_sha}
    if action in ('status', 'smoke'):
        verified_record(c, ('ready',))
        evidence = live_identity(c)
        display_evidence = {}
        if action == 'smoke':
            health = read_json(8769)
            nleg = health.get('n_leg', {})
            if c.n_leg_paused:
                if nleg.get('status') != 'paused' or nleg.get('code') != 'N_LEG_PAUSED':
                    raise ValueError('N-leg pause contract mismatch')
                if c.mode == 'shadow' and credential_backend(c) != 'disabled':
                    display_evidence = display_snapshot_evidence(
                        read_json(8769, '/api/prediction-arbitrage/lp/dashboard'))
                elif c.mode == 'shadow':
                    if read_status(8769, '/api/prediction-arbitrage/lp/dashboard') != 503:
                        raise ValueError('paused Shadow LP read model must be unavailable')
                else:
                    lp = read_json(8769, '/api/prediction-arbitrage/lp/dashboard')
                    if lp.get('state') != 'ready' or any(not isinstance(lp.get(k), list) for k in ('orders','positions','recommendations')):
                        raise ValueError('LP read model not ready')
            else:
                if nleg.get('status') != 'running' or nleg.get('code') != 'N_LEG_RUNNING':
                    raise ValueError('N-leg running contract mismatch')
                state = read_json(8769, '/api/prediction-arbitrage/state')
                leg = state.get('n_leg', {})
                if leg.get('contract_generation') != 2 or leg.get('mode') != 'MANUAL' or leg.get('execution_scopes', {}).get('SAME_EVENT_SAME_VENUE', {}).get('capability') != 'OBSERVE_ONLY':
                    raise ValueError('N-leg state contract mismatch')
                if any(row.get('engine_owner') != 'N_LEG' for row in state.get('opportunities', [])):
                    raise ValueError('N-leg opportunity owner mismatch')
            since = evidence['systemd_started_at']
            if not since:
                raise ValueError('log time boundary missing')
            logs = run('journalctl', '-u', UNIT, '--since', since, '--no-pager', '-o', 'cat')
            if not logs or 'prediction_runtime_state' not in logs or re.search(r'traceback|fatal|exception|error', logs, re.I):
                raise ValueError('runtime logs missing or contain errors')
            if live_identity(c) != evidence:
                raise ValueError('owner changed during smoke')
        component = {'mode': c.mode, 'n_leg_paused': c.n_leg_paused,
                     'credential_backend': credential_backend(c)}
        return {'status': 'RUNNING' if action == 'status' else 'BACKEND_SMOKE_OK',
                **evidence, **component, **({'display_snapshot':display_evidence} if display_evidence else {})}
    if os.geteuid() != 0:
        raise ValueError('systemd mutations require root')
    # Same global lock for all runtime roots/configurations of this unit.
    with open('/run/open-trader-prediction-operation.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == 'install':
            preflight(c)
            if UNIT_PATH.exists():
                trusted_root_path(UNIT_PATH)
                saved = load_prediction_runtime_record(c.record)
                if not saved or saved.get('manager') != 'systemd' or saved['state'] != 'stopped':
                    raise ValueError('existing unit requires verified stopped release record')
                if saved.get('unit_text') != UNIT_PATH.read_text():
                    raise ValueError('existing unit differs from stopped record')
            unit = render_unit(c)
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory)/UNIT
                target.write_text(unit)
                run('systemd-analyze', 'verify', str(target))
            if UNIT_PATH.exists():
                with tempfile.NamedTemporaryFile(dir=c.runtime_root, prefix='unit-backup-', suffix='.service', mode='wb', delete=False) as backup:
                    backup.write(UNIT_PATH.read_bytes())
            temp = UNIT_PATH.with_suffix('.service.tmp')
            temp.write_text(unit)
            temp.chmod(0o644)
            os.replace(temp, UNIT_PATH)
            run('systemctl', 'daemon-reload')
            record(c, 'stopped')
            return {'status': 'INSTALLED_STOPPED', 'git_sha': c.expected_sha}
        installed_unit(c)
        if action == 'stop':
            if unit_state()['MainPID'] == '0':
                absent(c)
                saved = verified_record(c, ('ready','maintenance','failed','stopped'))
                if saved['state'] != 'stopped':
                    record(c, 'stopped')
                return {'status':'STOPPED', 'git_sha':c.expected_sha}
            verified_record(c, ('ready','maintenance','failed'))
            live = live_identity(c)
            record(c, 'maintenance', ready=live)
            run('systemctl', 'stop', UNIT, timeout=100)
            absent(c)
            record(c, 'stopped', ready=live)
            return {'status': 'STOPPED', 'exchange_orders': 'unchanged'}
        if action == 'start':
            if unit_state()['MainPID'] != '0':
                return operate(c, 'status')
            preflight(c)
            verified_record(c, ('stopped',))
            record(c, 'maintenance')
            run('systemctl', 'start', UNIT)
            deadline = time.monotonic()+120
            while time.monotonic() < deadline:
                try:
                    live = live_identity(c)
                    record(c, 'ready', ready=live)
                    return {'status': 'RUNNING', **live}
                except (OSError, ValueError):
                    time.sleep(2)
            record(c, 'failed', failure_reason='candidate_readiness_not_proven')
            raise ValueError('candidate readiness not proven; inspect service, no automatic rollback')
    raise ValueError('unsupported action')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['render','preflight','install','start','status','stop','smoke'])
    parser.add_argument('--config', type=Path, default=Path('/etc/open-trader/prediction-cloud.json'))
    args = parser.parse_args(argv)
    try:
        if args.action != 'render':
            trusted_config(args.config)
        config = load_config(args.config)
        if args.action == 'render':
            print(render_unit(config), end='')
        else:
            print(json.dumps({**operate(config, args.action),
                              'mode': config.mode,
                              'release_root': str(config.release_root),
                              'runtime_root': str(config.runtime_root)}))
        return 0
    except Exception:
        # Never expose config/credential values via an unexpected exception chain.
        print(json.dumps({'status': 'BLOCKED', 'action': args.action,
                          'reason': 'cloud operation could not be verified'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
