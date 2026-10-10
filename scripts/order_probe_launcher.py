#!/usr/bin/env python3
"""Dedicated, stdlib-only lpprobe installation and trusted Tokyo transport."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

SCHEMA = 'open_trader.lpprobe.install.v1'
PROFILE = 'open_trader.lpprobe.tokyo.v1'


class Blocked(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'UNSAFE_PATH')
    for part in (path, *path.parents):
        require(not part.is_symlink(), 'UNSAFE_PATH')
    return path


def private(path, mode=0o600):
    path = canonical(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == mode and info.st_nlink == 1, 'UNSAFE_FILE')
    return path.read_bytes()


def profile(raw):
    data = json.loads(raw)
    require(isinstance(data, dict) and data.get('schema') == PROFILE, 'PROFILE_MISMATCH')
    require(set(data) == {'schema', 'ssh', 'host', 'bootstrap', 'cloud_config', 'user',
                          'runtime_root', 'credentials_file'}, 'PROFILE_MISMATCH')
    require(data['host'] == 'open-trader-tokyo-experiment' and data['user'] == 'prediction'
            and data['bootstrap'] == '/usr/bin/python3'
            and data['cloud_config'] == '/etc/open-trader/prediction-cloud.json'
            and data['runtime_root'] == '/var/lib/open-trader/prediction'
            and data['credentials_file'] == '/var/lib/open-trader/prediction-credentials/polymarket.json',
            'PROFILE_MISMATCH')
    tool = canonical(data['ssh'])
    require(tool.is_file() and os.access(tool, os.X_OK) and not tool.stat().st_mode & 0o022,
            'SSH_UNAVAILABLE')
    return data


# Sent over authenticated SSH stdin. Only fixed remote commands are shell parsed.
# Root reads protected nonsecret management metadata; exchange code runs as prediction.
REMOTE = r'''
import json, os, pathlib, pwd, re, signal, stat, subprocess, sys, time
class Invalid(Exception): pass
def need(ok):
    if not ok: raise Invalid()
def trusted(path, owner=0, mode=None, symlink=False):
    path=pathlib.Path(path)
    need(path.is_absolute() and '..' not in path.parts)
    for part in (path,*path.parents):
        info=part.lstat()
        need(not stat.S_ISLNK(info.st_mode) or (part == path and symlink))
        if part == path:
            need(info.st_uid == owner and (stat.S_ISLNK(info.st_mode) or not info.st_mode & 0o022))
            if mode is not None:
                need(stat.S_IMODE(info.st_mode) == mode)
                need(stat.S_ISDIR(info.st_mode) if mode == 0o700 else
                     stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        else:
            need(not info.st_mode & 0o022 or (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))
    return path
def read(path,owner=0,mode=0o600):
    path=trusted(path,owner,mode)
    need(stat.S_ISREG(path.lstat().st_mode))
    with path.open('rb') as file:
        raw=file.read(65537)
    need(len(raw)<=65536)
    return json.loads(raw)
def git(root,*args):
    result=subprocess.run(['/usr/bin/git','--no-optional-locks','-C',str(root),*args],
        env={'PATH':'/usr/bin:/bin','GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'safe.directory',
             'GIT_CONFIG_VALUE_0':str(root)},capture_output=True,text=True,timeout=5)
    need(result.returncode == 0)
    return result.stdout.strip()
def remote(p):
    started=time.monotonic()
    def expired(*_): raise Invalid()
    signal.signal(signal.SIGALRM,expired)
    signal.alarm(25)
    try:
        need(os.geteuid() == 0)
        c=read(p['cloud_config'])
        need(c['user'] == p['user'] and c['runtime_root'] == p['runtime_root']
             and c.get('credential_backend') == 'file' and c['credentials_file'] == p['credentials_file'])
        uid=pwd.getpwnam(c['user']).pw_uid
        need(uid != 0)
        root=trusted(c['release_root'])
        sha=c['expected_sha']
        need(re.fullmatch('[0-9a-f]{40}',sha) is not None)
        need(str(root) == '/opt/open-trader/releases/'+sha)
        python=pathlib.Path(c['python'])
        need(re.fullmatch(r'/opt/open-trader/venvs/[0-9a-f]{40}/bin/python',str(python)) is not None)
        trusted(python.parent)
        trusted(python,symlink=True)
        trusted(python.resolve(strict=True))
        for item in python.parent.parent.rglob('*'):
            trusted(item,symlink=True)
            trusted(item.resolve(strict=True))
        for item in root.rglob('*'): trusted(item)
        need(git(root,'rev-parse','HEAD') == sha and git(root,'rev-parse','--abbrev-ref','HEAD') == 'HEAD'
             and git(root,'status','--porcelain') == '')
        need(git(root,'ls-files','--error-unmatch','--','src/open_trader/polymarket_order_probe.py')
             == 'src/open_trader/polymarket_order_probe.py')
        runtime=trusted(c['runtime_root'],uid,0o700)
        trusted(runtime/'config',uid,0o700)
        account=trusted(runtime/'config/prediction_arbitrage.json',uid,0o600)
        saved=read(runtime/'prediction-systemd-release.json')
        need(saved.get('schema_version') == 'open_trader.prediction_service.runtime.v1'
             and saved.get('manager') == 'systemd'
             and saved.get('state') in ('ready','stopped','maintenance','failed')
             and saved.get('candidate') == {'checkout':str(root),'git_sha':sha})
        unit=trusted('/etc/systemd/system/open-trader-prediction.service',0,0o644).read_text()
        need(unit == saved.get('unit_text'))
        lines=unit.splitlines()
        need('User='+c['user'] in lines and 'Group='+c['user'] in lines
             and 'WorkingDirectory='+str(root) in lines
             and 'Environment=PYTHONPATH='+str(root/'src') in lines
             and any(line.startswith('ExecStart='+str(python)+' -m open_trader prediction-service ') for line in lines))
        # No secret contents read or copied. The service UID backend validates this reference.
        trusted(p['credentials_file'],uid,0o600)
        remaining=110-(time.monotonic()-started)
        need(remaining >= 40)
        signal.alarm(0)
        command=['/usr/sbin/runuser','-u',c['user'],'--','/usr/bin/env','-i','PATH=/usr/bin:/bin',
            'PYTHONPATH='+str(root/'src'),'PYTHONDONTWRITEBYTECODE=1',str(python),'-B',
            '-m','open_trader.polymarket_order_probe','self-test','--config',str(account),
            '--credential-backend','file','--credentials-file',p['credentials_file'],
            '--receipt-dir',str(runtime/'order-probe'),'--budget-seconds',str(remaining)]
        os.execv(command[0],command)
    except Exception:
        print(json.dumps({'result':'BLOCKED','reason':'REMOTE_RELEASE_UNVERIFIED',
            'summary_zh':'阻断：当前托管发布或运行身份无法核验。'}))
        raise SystemExit(2)
'''


def tool_env():
    return {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'GIT_', 'OPEN_TRADER_'))}


def committed(source, raw, git):
    tool = canonical(git)
    require(os.access(tool, os.X_OK), 'GIT_UNAVAILABLE')
    base = [str(tool), '--no-optional-locks', '-C', str(source.parent)]
    head = subprocess.run([*base, 'rev-parse', 'HEAD'], capture_output=True, text=True,
                          env=tool_env(), timeout=5)
    sha = head.stdout.strip()
    require(head.returncode == 0 and re.fullmatch('[0-9a-f]{40}', sha), 'SOURCE_UNCOMMITTED')
    blob = subprocess.run([*base, 'cat-file', 'blob', sha + ':scripts/order_probe_launcher.py'],
                          capture_output=True, env=tool_env(), timeout=5)
    require(blob.returncode == 0 and blob.stdout == raw, 'SOURCE_UNCOMMITTED')
    return sha


def install(args):
    p = argparse.ArgumentParser()
    p.add_argument('--profile', required=True, type=Path)
    p.add_argument('--launcher-python', required=True)
    p.add_argument('--git', default='/usr/bin/git')
    p.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    p.add_argument('--payload-dir', type=Path, default=Path.home() / '.local/share/open-trader/order-probe')
    a = p.parse_args(args)
    binary = canonical(a.bin_dir / 'lpprobe')
    directory = canonical(a.payload_dir)
    require(not binary.exists() and not directory.exists(), 'INSTALL_TARGET_EXISTS')
    bootstrap = Path(a.launcher_python)
    require(bootstrap.is_absolute() and os.access(bootstrap, os.X_OK), 'BOOTSTRAP_UNAVAILABLE')
    raw_profile = private(a.profile)
    profile(raw_profile)
    source = Path(__file__).resolve()
    raw = source.read_bytes()
    sha = committed(source, raw, a.git)
    missing = json.dumps(dict(result='BLOCKED', reason='BOOTSTRAP_UNAVAILABLE', summary_zh='阻断：既有启动 Python 不可用。'))
    entry = ('#!/bin/sh\n# ' + SCHEMA + '\nif [ ! -x ' + shlex.quote(str(bootstrap)) + ' ]; then\n'
             '  printf "%s\\n" ' + shlex.quote(missing) + '\n  exit 2\nfi\nexec ' + shlex.quote(str(bootstrap)) +
             ' -I -B ' + shlex.quote(str(directory / 'launcher.py')) + ' --installed "$@"\n').encode()
    # Exclusive creation: an unrelated target is never overwritten.
    a.bin_dir.mkdir(parents=True, exist_ok=True)
    directory.mkdir(parents=True, mode=0o700)
    receipt = dict(schema=SCHEMA, binary=str(binary), payload=str(directory), source_sha=sha,
                   hashes={'launcher.py': digest(raw), 'tokyo.json': digest(raw_profile), 'entry': digest(entry)})
    for name, contents in [('launcher.py', raw), ('tokyo.json', raw_profile),
                           ('installation.json', json.dumps(receipt, sort_keys=True).encode())]:
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as file:
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
    fd = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, 'wb') as file:
        file.write(entry)
        file.flush()
        os.fsync(file.fileno())
    require(private(binary, 0o700) == entry, 'INSTALL_READBACK_FAILED')
    validate_installation(directory)
    print(json.dumps(dict(result='INSTALLED', command=str(binary) + ' tokyo', source_sha=sha)))
    return 0


def validate_installation(directory):
    info = canonical(directory).lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700, 'UNSAFE_INSTALLATION')
    saved = json.loads(private(directory / 'installation.json'))
    require(saved.get('schema') == SCHEMA and saved.get('payload') == str(directory)
            and re.fullmatch('[0-9a-f]{40}', saved.get('source_sha', '')), 'INSTALLATION_MISMATCH')
    for name in ('launcher.py', 'tokyo.json'):
        require(digest(private(directory / name)) == saved['hashes'][name], 'INSTALLATION_MISMATCH')
    require(digest(private(Path(saved['binary']), 0o700)) == saved['hashes']['entry'], 'INSTALLATION_MISMATCH')
    return profile(private(directory / 'tokyo.json'))


def tokyo():
    p = validate_installation(Path(__file__).absolute().parent)
    command = [p['ssh'], '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ForwardAgent=no', '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1',
               '-o', 'ControlMaster=no', '-o', 'ControlPath=none', p['host'],
               'sudo -n /usr/bin/python3 -I -B -']
    script = REMOTE + '\nremote(' + repr(p) + ')\n'
    try:
        result = subprocess.run(command, input=script, text=True, capture_output=True,
                                env=tool_env(), timeout=130)
    except (OSError, subprocess.TimeoutExpired):
        raise Blocked('SSH_CONNECTION_UNKNOWN') from None
    try:
        report = json.loads(result.stdout)
        require(isinstance(report, dict) and report.get('result') in
                ('PASS', 'BLOCKED', 'REJECTED', 'PARTIAL', 'FILLED', 'UNKNOWN'), 'REMOTE_RESULT_UNKNOWN')
    except (ValueError, TypeError):
        raise Blocked('SSH_CONNECTION_UNKNOWN') from None
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0 if result.returncode == 0 and report['result'] == 'PASS' else 2


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args and args[0] == 'install':
            return install(args[1:])
        require(args == ['--installed', 'tokyo'], 'USAGE_LPPROBE_TOKYO')
        return tokyo()
    except Blocked as error:
        print(json.dumps(dict(result='UNKNOWN' if str(error).endswith('UNKNOWN') else 'BLOCKED',
                             reason=str(error), summary_zh='诊断未完成：请保留证据并核查所示原因。'), ensure_ascii=False))
    except Exception:
        print(json.dumps(dict(result='BLOCKED', reason='LOCAL_INSTALLATION_UNVERIFIED',
                             summary_zh='阻断：本地安装或配置无法核验。'), ensure_ascii=False))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
