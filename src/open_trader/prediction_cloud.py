"""Explicit, fail-closed systemd operations for the Prediction-only release."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
import fcntl
import grp
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import sqlite3
import subprocess
import stat
import sys
import tempfile
import time

from .prediction_release import RUNTIME_SCHEMA, RELEASE_SCHEMA, inspect_prediction_release_checkout, write_prediction_runtime_record, load_prediction_runtime_record

UNIT = 'open-trader-prediction.service'
UNIT_PATH = Path('/etc/systemd/system') / UNIT
CONFIG_PATH = Path('/etc/open-trader/prediction-cloud.json')
OPERATION_LOCK = Path('/run/open-trader-prediction-operation.lock')
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
    memory_max_bytes: int = 768 * 1024 * 1024
    candidate_exclusions: bool = False

    @property
    def record(self):
        return self.runtime_root / 'prediction-systemd-release.json'

    @property
    def runtime_lock(self):
        return self.runtime_root / 'data/prediction_arbitrage/runtime.lock'


def load_config(path: Path, *, contents: bytes | None = None) -> CloudConfig:
    data = json.loads(path.read_text() if contents is None else contents)
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
    if not path.is_file() or path.stat().st_nlink != 1 or stat.S_IMODE(path.stat().st_mode) != 0o600:
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
    if type(c.candidate_exclusions) is not bool:
        raise ValueError("candidate_exclusions must be a boolean")
    if type(c.memory_max_bytes) is not int or not 64 * 1024**2 < c.memory_max_bytes <= 1_000_000_000:
        raise ValueError('cloud memory budget must exceed 64MiB and be at most 1GB')
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
    exclusions_env = 'Environment=OPEN_TRADER_LP_CANDIDATE_EXCLUSIONS=1\n' if c.candidate_exclusions else ''
    guarded = c.mode == 'shadow' and c.n_leg_paused == 1
    resources = (f'Environment=OPEN_TRADER_SHADOW_MEMORY_MAX_BYTES={c.memory_max_bytes}\n'
                 f'MemoryAccounting=yes\nMemoryMax={c.memory_max_bytes}\n'
                 'CPUAccounting=yes\nCPUQuota=100%\nTasksAccounting=yes\nTasksMax=96\n') if guarded else ''
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
{exclusions_env}{credential_env}Environment=OPEN_TRADER_NLEG_PAUSED={c.n_leg_paused}
Environment=OPEN_TRADER_NLEG_PAUSED={c.n_leg_paused}
ExecStart={c.python} -m open_trader prediction-service --mode {c.mode} --data-dir {c.runtime_root}/data --config {c.runtime_root}/config/prediction_arbitrage.json --host 127.0.0.1 --port 8769 --release-manifest {c.release_root}/ops/prediction-service-release.json
{resources}Restart={'no' if guarded else 'on-failure'}
RestartSec=30
TimeoutStopSec={20 if guarded else 90}
SendSIGKILL={'yes' if guarded else 'no'}
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


def installed_unit(c: CloudConfig, *, contents: bytes | None = None) -> None:
    trusted_root_path(UNIT_PATH)
    if (not stat.S_ISREG(UNIT_PATH.lstat().st_mode) or UNIT_PATH.lstat().st_nlink != 1
        or (UNIT_PATH.read_text() if contents is None else contents.decode()) != render_unit(c)):
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
    process_environment = (process/'environ').read_bytes().decode().split('\0')
    environment = dict(item.split('=',1) for item in process_environment if '=' in item)
    effective_exclusions = environment.get('OPEN_TRADER_LP_CANDIDATE_EXCLUSIONS') == '1'
    if (sum(item.startswith('OPEN_TRADER_LP_CANDIDATE_EXCLUSIONS=') for item in process_environment) > 1
        or effective_exclusions != c.candidate_exclusions):
        raise ValueError('running candidate exclusion environment mismatch')
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
    return {'pid': pid, 'git_sha': c.expected_sha, 'candidate_exclusions': effective_exclusions,
            'started_at': health.get('started_at'),
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


def verified_record(c: CloudConfig, states: tuple[str, ...], *, contents: bytes | None = None) -> dict:
    saved = load_prediction_runtime_record(c.record) if contents is None else json.loads(contents)
    if (not isinstance(saved, dict) or saved.get('schema_version') != RUNTIME_SCHEMA or saved.get('manager') != 'systemd' or saved.get('state') not in states
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


class PreparationRecoveryBlocked(ValueError):
    """Only redacted operation evidence may cross the command boundary."""

    def __init__(self, *, phase, backup=None, before=None, after=None,
                 committed=False, recovered_item_count=0):
        super().__init__('stopped preparation recovery blocked')
        self.evidence = dict(phase=phase, backup=str(backup) if backup else None,
                             before=before, after=after, recovery_committed=committed,
                             recovered_item_count=recovered_item_count)


@contextmanager
def _recovery_lock(path: Path, owner: int, *, create: bool = False):
    if create:
        trusted_root_path(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner
            or info.st_mode & (0o022 if create else 0o077)):
            raise ValueError('private canonical recovery lock required')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _recovery_file(path: Path, *, owner=None, mode=None):
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
        or owner is not None and before.st_uid != owner
        or mode is not None and stat.S_IMODE(before.st_mode) != mode):
        raise ValueError('canonical recovery file required before open')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        pin = (fd, info)
        _check_recovery_file(path, pin)
        if ((before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
            or owner is not None and info.st_uid != owner
            or mode is not None and stat.S_IMODE(info.st_mode) != mode):
            raise ValueError('recovery file changed before open')
        yield pin
    finally:
        os.close(fd)


def _check_recovery_file(path: Path, pin) -> None:
    fd, expected = pin
    for observed in (os.fstat(fd), path.lstat()):
        if (not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino)):
            raise ValueError('recovery file identity changed')


def _control_contents(path: Path, control) -> bytes:
    pin, expected = control
    _check_recovery_file(path, pin)
    current = os.fstat(pin[0])
    if (current.st_uid, current.st_gid, current.st_mode) != (pin[1].st_uid, pin[1].st_gid, pin[1].st_mode):
        raise ValueError('recovery control trust changed')
    actual = os.pread(pin[0], current.st_size, 0)
    _check_recovery_file(path, pin)
    if expected is not None and actual != expected:
        raise ValueError('recovery control contents changed')
    return actual


def _preparation_projection(store) -> dict:
    value = store.lp_preparation()
    if value is None:
        raise ValueError('existing paused preparation required')
    result = dict(generation=value.get('generation'), paused=value.get('paused')
                  if type(value.get('paused')) is bool else None)
    for key, allowed in [('state', {'idle','ready','paused','partial','preparing','waiting_retry'}),
                         ('stage', {'idle','catalog','metadata','history','complete'})]:
        raw = value.get(key)
        result[key] = raw if isinstance(raw, str) and raw in allowed else 'unknown'
    return result


def _recovery_paths(c: CloudConfig) -> list[Path]:
    """Only database, ownership records and known non-secret operations files."""
    names = ['config/prediction_arbitrage.json', 'prediction-systemd-release.json',
             'data/prediction_arbitrage/prediction_arbitrage.sqlite3',
             'data/prediction_arbitrage/prediction_arbitrage.sqlite3-wal',
             'data/prediction_arbitrage/prediction_arbitrage.sqlite3-shm',
             'data/prediction_arbitrage/runtime.lock', 'data/prediction_arbitrage/lp-preparation.lock']
    return [c.runtime_root/name for name in names] + sorted(c.runtime_root.glob('unit-backup-*.service'))


def _backup_stopped_preparation(c: CloudConfig, config: Path, backup_root: Path, controls) -> Path:
    if not backup_root.is_absolute():
        raise ValueError('absolute backup directory required')
    trusted_root_path(backup_root)
    if not backup_root.is_dir() or backup_root.stat().st_mode & 0o077:
        raise ValueError('private backup directory required')
    credential = Path(c.credentials_file)
    if any(path.resolve().is_relative_to(credential.parent.resolve()) for path in (config, UNIT_PATH)):
        raise ValueError('credential directory cannot supply backup control files')
    if any(backup_root.is_relative_to(path) or path.is_relative_to(backup_root)
           for path in (c.runtime_root, c.release_root, credential.parent)):
        raise ValueError('independent backup directory required')
    backup = Path(tempfile.mkdtemp(prefix='lp-preparation-', dir=backup_root))
    try:
        _recovery_files(c)
        files = []
        with ExitStack() as opened:
            sources = [(path, backup/'runtime'/path.relative_to(c.runtime_root))
                       for path in _recovery_paths(c) if path.exists()]
            sources += [(config, backup/'cloud.json'), (UNIT_PATH, backup/'prediction.service')]
            for source, target in sources:
                control = controls.get(source)
                pin = control[0] if control else opened.enter_context(_recovery_file(source))
                _check_recovery_file(source, pin)
                if control:
                    _control_contents(source, control)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Copy the validated descriptor, never reopen a source path.
                with os.fdopen(os.dup(pin[0]), 'rb') as handle, target.open('xb') as output:
                    handle.seek(0)
                    shutil.copyfileobj(handle, output)
                target.chmod(0o600)
                _check_recovery_file(source, pin)
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                source_hash = hashlib.sha256()
                with os.fdopen(os.dup(pin[0]), 'rb') as handle:
                    handle.seek(0)
                    for chunk in iter(lambda: handle.read(1024*1024), b''):
                        source_hash.update(chunk)
                _check_recovery_file(source, pin)
                if digest != source_hash.hexdigest():
                    raise ValueError('stopped backup changed during copy')
                info = pin[1]
                files.append(dict(path=str(target.relative_to(backup)), sha256=digest,
                                  uid=info.st_uid, gid=info.st_gid, mode=stat.S_IMODE(info.st_mode)))
        # Retain the byte-for-byte stopped snapshot, including WAL/SHM, and verify
        # a separate consistent SQLite copy without opening a mutable Store.
        database = c.runtime_root/'data/prediction_arbitrage/prediction_arbitrage.sqlite3'
        consistent = backup/'consistent.sqlite3'
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(consistent)) as target:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('backup database integrity failed')
        consistent.chmod(0o600)
        files.append(dict(path='consistent.sqlite3', sha256=hashlib.sha256(consistent.read_bytes()).hexdigest()))
        manifest = backup/'manifest.json'
        manifest.write_text(json.dumps(dict(status='COMPLETE', git_sha=c.expected_sha,
                                           sqlite_integrity='ok', files=files)))
        manifest.chmod(0o600)
        for path in [manifest, *[backup/item['path'] for item in files]]:
            with path.open('rb') as handle:
                os.fsync(handle.fileno())
        for path in (backup_root, backup, *[p for p in backup.rglob('*') if p.is_dir()]):
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return backup
    except Exception:
        raise PreparationRecoveryBlocked(phase='backup', backup=backup) from None



def _recovery_files(c: CloudConfig) -> None:
    # SQLite also opens WAL/SHM: reject aliases before its first read.
    credential_directory = Path(c.credentials_file).parent.resolve()
    for path in _recovery_paths(c):
        if path.resolve().is_relative_to(credential_directory):
            raise ValueError('credential paths cannot enter the recovery backup')
        if not path.exists() and not path.is_symlink():
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('runtime recovery requires canonical unlinked files')


def _stopped_recovery_policy(c: CloudConfig, config: Path, controls) -> None:
    trusted_config(config)
    if load_config(config, contents=_control_contents(config, controls[config])) != c:
        raise ValueError('recovery configuration changed')
    installed_unit(c, contents=_control_contents(UNIT_PATH, controls[UNIT_PATH]))
    verified_record(c, ('stopped',), contents=_control_contents(c.record, controls[c.record]))
    state = dict(line.split('=', 1) for line in run('systemctl', 'show', UNIT,
        '-p', 'UnitFileState', '-p', 'Restart', '-p', 'MemoryMax', '-p', 'CPUQuotaPerSecUSec',
        '-p', 'TasksMax', '-p', 'MainPID', '-p', 'ActiveState').splitlines())
    expected = dict(UnitFileState='disabled', Restart='no', MemoryMax=str(c.memory_max_bytes),
                    CPUQuotaPerSecUSec='1s', TasksMax='96', MainPID='0', ActiveState='inactive')
    if any(state.get(key) != value for key, value in expected.items()) or listener_pids():
        raise ValueError('stopped resource and boot policy mismatch')



def _restore_recovery_sidecars(c: CloudConfig, user, directory_identity, pins) -> list[dict]:
    """Never unlink/checkpoint a WAL retained by a concurrent read-only client."""
    directory = c.runtime_root/'data/prediction_arbitrage'
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) !=
        (directory_identity.st_dev, directory_identity.st_ino)):
        raise ValueError('recovery storage directory changed')
    observed = []
    for suffix in ('-wal', '-shm'):
        path = directory/('prediction_arbitrage.sqlite3'+suffix)
        pin = pins.get(path)
        if not path.exists() and not path.is_symlink():
            # Closing the held SQLite reader can legitimately unlink its own
            # sidecars. A rename (still linked) or a new inode is not that case.
            if pin is not None and os.fstat(pin[0]).st_nlink != 0:
                raise ValueError('recovery sidecar disappeared with unknown ownership')
            observed.append(dict(name=path.name, exists=False))
            continue
        if pin is None:
            raise ValueError('unobserved recovery sidecar')
        _check_recovery_file(path, pin)
        fd, info = pin
        actual = os.fstat(fd)
        if actual.st_uid not in (0, user.pw_uid):
            raise ValueError('unknown recovery sidecar owner')
        os.fchown(fd, user.pw_uid, user.pw_gid)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        _check_recovery_file(path, pin)
        actual = os.fstat(fd)
        if (actual.st_uid, actual.st_gid, stat.S_IMODE(actual.st_mode)) != (user.pw_uid, user.pw_gid, 0o600):
            raise ValueError('recovery sidecar ownership not restored')
        observed.append(dict(name=path.name, exists=True, uid=actual.st_uid,
                             gid=actual.st_gid, mode=stat.S_IMODE(actual.st_mode)))
    return observed


def recover_stopped_preparation(config: Path, *, expected_sha: str,
                                expected_generation: int, backup_root: Path) -> dict:
    """Offline operator recovery; never authenticates, starts or reads a wallet."""
    if os.geteuid() != 0:
        raise ValueError('stopped preparation recovery requires root')
    if config != CONFIG_PATH:
        raise ValueError('official managed recovery configuration required')
    with ExitStack() as locks:
        trusted_config(config)
        controls = {}
        config_pin = locks.enter_context(_recovery_file(config, owner=0, mode=0o600))
        controls[config] = (config_pin, _control_contents(config, (config_pin, None)))
        c = load_config(config, contents=controls[config][1])
        if (c.mode != 'shadow' or c.n_leg_paused != 1 or credential_backend(c) != 'file'
            or c.expected_sha != expected_sha or type(expected_generation) is not int
            or expected_generation < 1 or '..' in Path(c.credentials_file).parts
            or Path(c.credentials_file).resolve().is_relative_to(c.runtime_root)):
            raise ValueError('exact authenticated paused Shadow recovery profile required')
        if (Path(sys.executable).resolve() != c.python.resolve()
            or Path(sys.prefix).resolve() != c.python.parent.parent.resolve()
            or any(os.environ.get(key) for key in ('PYTHONHOME', 'PYTHONUSERBASE'))
            or Path(__file__).resolve() != c.release_root/'src/open_trader/prediction_cloud.py'):
            raise ValueError('recovery must use the selected release source and interpreter')
        held = [(OPERATION_LOCK, locks.enter_context(_recovery_lock(OPERATION_LOCK, 0, create=True)))]
        trusted_layout(c)
        release_identity(c)
        # The selected preflight implementation verifies source/lock/runtime
        # locally. Its GitHub client and network preflight are not invoked.
        run(str(c.python), '-I', '-B', '-c',
            'import runpy,sys; n=runpy.run_path(sys.argv[1]); '
            'n["verify_checkout"](sys.argv[2],sys.argv[3]); '
            'n["inspect_runtime"](sys.argv[2],[])',
            str(c.release_root/'scripts/deployment_preflight.py'), str(c.release_root), c.expected_sha,
            timeout=120)
        _recovery_files(c)
        for path, mode in ((UNIT_PATH, 0o644), (c.record, 0o600)):
            if path == UNIT_PATH:
                trusted_root_path(path)
            if path.resolve().is_relative_to(Path(c.credentials_file).parent.resolve()):
                raise ValueError('credential alias in recovery controls')
            pin = locks.enter_context(_recovery_file(path, owner=0, mode=mode))
            controls[path] = (pin, _control_contents(path, (pin, None)))
        installed_unit(c, contents=_control_contents(UNIT_PATH, controls[UNIT_PATH]))
        absent(c)
        verified_record(c, ('stopped',), contents=_control_contents(c.record, controls[c.record]))
        user = service_user(c)
        directory = c.runtime_root/'data/prediction_arbitrage'
        _recovery_files(c)
        for path in (c.runtime_root/'data', directory, directory/'prediction_arbitrage.sqlite3'):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != user.pw_uid or info.st_mode & 0o077:
                raise ValueError('private service-owned recovery database required')
        for path in (c.runtime_lock, directory/'lp-preparation.lock'):
            held.append((path, locks.enter_context(_recovery_lock(path, user.pw_uid))))
        _stopped_recovery_policy(c, config, controls)
        before = after = backup = None
        committed = False
        recovered = []
        directory_identity = directory.stat()
        sidecar_pins = {}
        reader = None
        try:
            # Pin existing identities before SQLite opens them. A held read
            # transaction then prevents normal last-close sidecar churn while
            # backup and recovery use their existing short-lived connections.
            sidecar_paths = [directory/('prediction_arbitrage.sqlite3'+suffix) for suffix in ('-wal','-shm')]
            for path in sidecar_paths:
                if path.exists():
                    sidecar_pins[path] = locks.enter_context(_recovery_file(path))
            reader = sqlite3.connect((directory/'prediction_arbitrage.sqlite3').as_uri()+'?mode=ro', uri=True)
            if reader.execute('PRAGMA journal_mode').fetchone() != ('wal',):
                raise ValueError('existing WAL recovery database required')
            reader.execute('BEGIN')
            reader.execute('SELECT generation FROM lp_preparation WHERE singleton=1').fetchall()
            for path in sidecar_paths:
                if path not in sidecar_pins:
                    # SQLite's initial read creates missing WAL/SHM. Observe
                    # them now, before backup or any recovery transaction.
                    sidecar_pins[path] = locks.enter_context(_recovery_file(path))
                _check_recovery_file(path, sidecar_pins[path])
            from .prediction_arbitrage_store import PredictionArbitrageStore, read_minimum_reader_generation
            if read_minimum_reader_generation(c.runtime_root/'data') > release_identity(c)['reader_generation']:
                raise ValueError('release cannot read recovery database')
            store = PredictionArbitrageStore(c.runtime_root/'data', initialize=False)
            database_identity = store.path.stat()
            before = _preparation_projection(store)
            if before['generation'] != expected_generation or before['paused'] is not True or before['state'] != 'paused':
                raise ValueError('paused preparation generation mismatch')
            try:
                backup = _backup_stopped_preparation(c, config, backup_root, controls)
            except PreparationRecoveryBlocked as error:
                error.evidence.update(before=before, after=before)
                backup = Path(error.evidence['backup']) if error.evidence['backup'] else None
                raise
            except Exception:
                raise PreparationRecoveryBlocked(phase='backup', before=before, after=before) from None
            # Revalidate before another SQLite connection can touch a replaced
            # sidecar; failed identity checks do not attempt a state reread.
            _stopped_recovery_policy(c, config, controls)
            _recovery_files(c)
            for path, pin in sidecar_pins.items():
                _check_recovery_file(path, pin)
            for path, fd in [*held, (store.path, None)]:
                observed = path.stat()
                expected = os.fstat(fd) if fd is not None else database_identity
                if (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
                    raise ValueError('recovery owner or database identity changed')
            try:
                recovered = store.lp_recover_preparation_items(expected_generation=expected_generation)
                committed = True
                after = _preparation_projection(store)
            except Exception:
                try:
                    after = _preparation_projection(store)
                except Exception:
                    after = None
                raise PreparationRecoveryBlocked(phase='transaction', backup=backup, before=before,
                    after=after, committed=committed, recovered_item_count=len(recovered)) from None
            result = dict(status='PREPARATION_RECOVERED', git_sha=c.expected_sha, before=before,
                        after=after, backup=str(backup), recovery_committed=True,
                        recovered_item_count=len(recovered))
        except PreparationRecoveryBlocked:
            raise
        except Exception:
            raise PreparationRecoveryBlocked(phase='validation', backup=backup, before=before,
                after=after, committed=committed, recovered_item_count=len(recovered)) from None
        finally:
            active_error = sys.exc_info()[1]
            try:
                if reader is not None:
                    reader.close()
                sidecars = _restore_recovery_sidecars(c, user, directory_identity, sidecar_pins)
            except Exception:
                raise PreparationRecoveryBlocked(phase='storage_ownership', backup=backup, before=before,
                    after=after, committed=committed, recovered_item_count=len(recovered)) from None
            if isinstance(active_error, PreparationRecoveryBlocked):
                active_error.evidence['sidecars'] = sidecars
        return {**result, 'sidecars': sidecars}



def operate(c: CloudConfig, action: str) -> dict:
    if action == 'preflight':
        preflight(c)
        return {'status': 'PRECHECK_OK', 'git_sha': c.expected_sha, 'mode': c.mode,
                'n_leg_paused': c.n_leg_paused, 'credential_backend': credential_backend(c),
                'candidate_exclusions': c.candidate_exclusions}
    if action == 'status' and unit_state()['MainPID'] == '0':
        absent(c)
        installed_unit(c)
        verified_record(c, ('stopped',))
        return {'status': 'STOPPED', 'git_sha': c.expected_sha, 'candidate_exclusions': c.candidate_exclusions}
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
                     'credential_backend': credential_backend(c), 'candidate_exclusions': c.candidate_exclusions}
        return {'status': 'RUNNING' if action == 'status' else 'BACKEND_SMOKE_OK',
                **evidence, **component, **({'display_snapshot':display_evidence} if display_evidence else {})}
    if os.geteuid() != 0:
        raise ValueError('systemd mutations require root')
    # Same global lock for all runtime roots/configurations of this unit.
    with OPERATION_LOCK.open('a') as lock:
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
            if c.mode == 'shadow' and c.n_leg_paused == 1:
                from .prediction_shadow_resources import require_host_headroom
                require_host_headroom(c.memory_max_bytes)
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
    parser.add_argument('action', choices=['render','preflight','install','start','status','stop','smoke','recover-preparation'])
    parser.add_argument('--config', type=Path, default=CONFIG_PATH)
    parser.add_argument('--expected-sha')
    parser.add_argument('--expected-generation', type=int)
    parser.add_argument('--backup-root', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action != 'recover-preparation' and any(
            value is not None for value in (args.expected_sha, args.expected_generation, args.backup_root)
        ):
            raise ValueError('recovery arguments require recover-preparation')
        if args.action == 'recover-preparation':
            if args.expected_sha is None or args.expected_generation is None or args.backup_root is None:
                raise ValueError('explicit SHA, generation and backup root required')
            print(json.dumps(recover_stopped_preparation(args.config, expected_sha=args.expected_sha,
                             expected_generation=args.expected_generation, backup_root=args.backup_root)))
            return 0
        if args.action != 'render':
            trusted_config(args.config)
        config = load_config(args.config)
        if args.action == 'render':
            print(render_unit(config), end='')
        else:
            print(json.dumps({**operate(config, args.action),
                              'mode': config.mode, 'candidate_exclusions': config.candidate_exclusions,
                              'release_root': str(config.release_root),
                              'runtime_root': str(config.runtime_root)}))
        return 0
    except PreparationRecoveryBlocked as error:
        print(json.dumps({'status': 'BLOCKED', 'action': args.action, **error.evidence}))
        return 2
    except Exception:
        # Never expose config/credential values via an unexpected exception chain.
        print(json.dumps({'status': 'BLOCKED', 'action': args.action,
                          'reason': 'cloud operation could not be verified'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
