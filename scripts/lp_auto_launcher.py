#!/usr/bin/env python3
"""User-local stable entry for the current managed macOS Prediction release.

This file is also the installed stdlib-only payload. It never imports the
application in the bootstrap interpreter or repairs service metadata.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
from http.client import HTTPException
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
from urllib.parse import urlsplit


LABEL = 'com.open-trader.prediction-service'
OWNED = ('launcher.py', 'config.json', 'receipt.json')
TRANSACTION = '.transaction.json'
NEXT = 'Check the managed Prediction deployment metadata; repair the launcher explicitly if its bootstrap is missing.'


class Unverified(Exception):
    reason = 'DEPLOYED_TARGET_UNVERIFIABLE'


class DeploymentBusy(Unverified):
    reason = 'DEPLOYMENT_IN_PROGRESS'


class SourceUnverified(Unverified):
    reason = 'LAUNCHER_SOURCE_UNVERIFIABLE'


class InstallationIncomplete(Unverified):
    reason = 'LAUNCHER_INSTALLATION_INCOMPLETE'


class InstallationBusy(Unverified):
    reason = 'INSTALLATION_IN_PROGRESS'
    next_action = 'Wait for the current local installation operation to finish, then explicitly retry.'


def require(condition):
    if not condition:
        raise Unverified()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(path):
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)))
    return path


def tool_environment():
    return {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'GIT_'))}


def run(command, *, cwd=None, env=None):
    result = subprocess.run(command, cwd=cwd, env=env or tool_environment(),
                            capture_output=True, text=True, timeout=10)
    require(result.returncode == 0)
    return result.stdout.strip()


def source_identity(git_tool, raw):
    """Bind published bytes to a committed blob, independently of the index."""
    source = Path(__file__).resolve()
    prefix = [git_tool, '--no-optional-locks', '-C', str(source.parent)]
    try:
        root = Path(run([*prefix, 'rev-parse', '--show-toplevel'])).resolve()
        relative = source.relative_to(root).as_posix()
        prefix = [git_tool, '--no-optional-locks', '-C', str(root)]
        sha = run([*prefix, 'rev-parse', 'HEAD'])
        require(bool(re.fullmatch('[0-9a-f]{40}', sha)))
        # A staged/hidden index cannot certify bytes against a different HEAD.
        tracked = run([*prefix, 'ls-tree', sha, '--', relative])
        require(tracked.startswith(('100644 blob ', '100755 blob ')))
        blob = subprocess.run([*prefix, 'cat-file', 'blob', f'{sha}:{relative}'],
                              env=tool_environment(), capture_output=True, timeout=10)
        require(blob.returncode == 0 and blob.stdout == raw)
        return sha
    except (Unverified, OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SourceUnverified() from exc


def receipt_valid(receipt, directory, binary):
    require(isinstance(receipt, dict) and receipt.get('schema') == 'open_trader.lpauto.install.v1')
    require(receipt.get('binary') == str(binary) and receipt.get('payload') == str(directory))
    for key, size in (('source_sha', 40), ('source_sha256', 64), ('config_sha256', 64), ('entry_sha256', 64)):
        require(isinstance(receipt.get(key), str) and re.fullmatch(f'[0-9a-f]{{{size}}}', receipt[key]))


def version_hash(version, name):
    if name == 'receipt.json':
        return version['receipt_sha256']
    return version['receipt'][{'launcher.py': 'source_sha256', 'config.json': 'config_sha256',
                               'lpauto': 'entry_sha256'}[name]]


def publication_state(directory, binary):
    """Accept only absent artifacts or bytes certified by owned receipts."""
    paths = {name: safe_path(directory / name) for name in (*OWNED, TRANSACTION)}
    paths['lpauto'] = safe_path(binary)
    snapshot = {}
    for name, path in paths.items():
        require(not path.exists() or path.is_file())
        snapshot[name] = digest(path.read_bytes()) if path.exists() else None
    if paths[TRANSACTION].exists():
        transaction = json.loads(paths[TRANSACTION].read_bytes())
        require(isinstance(transaction, dict) and transaction.get('schema') == 'open_trader.lpauto.transaction.v1')
        require(transaction.get('binary') == str(binary) and transaction.get('payload') == str(directory))
        versions = transaction.get('versions')
        require(isinstance(versions, list) and 1 <= len(versions) <= 5)
    elif paths['receipt.json'].exists():
        versions = [{'receipt': json.loads(paths['receipt.json'].read_bytes()),
                     'receipt_sha256': snapshot['receipt.json']}]
    else:
        require(not binary.exists() and (not directory.exists() or not any(directory.iterdir())))
        versions = []
    for version in versions:
        require(isinstance(version, dict) and set(version) == {'receipt', 'receipt_sha256'})
        receipt_valid(version['receipt'], directory, binary)
        require(isinstance(version['receipt_sha256'], str) and re.fullmatch('[0-9a-f]{64}', version['receipt_sha256']))
    for name in (*OWNED, 'lpauto'):
        require(snapshot[name] is None or any(version_hash(version, name) == snapshot[name] for version in versions))
    return versions, snapshot


def atomic_write(path, raw, mode=0o644):
    """Never truncate a published artifact or follow a destination symlink."""
    path = safe_path(path)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix='.lpauto-write-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish(directory, binary, contents, entry, versions, snapshot):
    directory.parent.mkdir(parents=True, exist_ok=True)
    binary.parent.mkdir(parents=True, exist_ok=True)
    # A failed staging write publishes no partial fresh payload. Temporary
    # files are separate from the flat owned installation and cleaned on error.
    with TemporaryDirectory(dir=directory.parent, prefix='.lpauto-stage-') as temporary:
        staged = Path(temporary) / 'payload'
        staged.mkdir()
        for name, raw in contents.items():
            atomic_write(staged / name, raw)
        require(publication_state(directory, binary) == (versions, snapshot))
        if not versions:
            # A complete receipt also proves ownership if entry publication is
            # interrupted after this atomic directory rename.
            os.replace(staged, directory)
        else:
            # Retain one known version for each currently present artifact, not
            # an unbounded history of failed repair attempts (at most four).
            retained = []
            for name in (*OWNED, 'lpauto'):
                if snapshot[name] is not None:
                    match = next(version for version in versions if version_hash(version, name) == snapshot[name])
                    if match not in retained:
                        retained.append(match)
            proposed = {'receipt': json.loads(contents['receipt.json']),
                        'receipt_sha256': digest(contents['receipt.json'])}
            if proposed not in retained:
                retained.append(proposed)
            transaction = {'schema': 'open_trader.lpauto.transaction.v1', 'binary': str(binary),
                           'payload': str(directory), 'versions': retained}
            atomic_write(directory / TRANSACTION, (json.dumps(transaction, sort_keys=True) + '\n').encode())
            for name in OWNED:
                os.replace(staged / name, safe_path(directory / name))
        atomic_write(binary, entry, 0o755)
        (directory / TRANSACTION).unlink(missing_ok=True)


def open_management_guard(path, expected):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        return os.open(path, flags)
    except FileNotFoundError:
        descriptor = None
        try:
            with NamedTemporaryFile(dir=path.parent, prefix='.lpauto-guard-') as handle:
                handle.write(expected)
                handle.flush()
                os.fsync(handle.fileno())
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    # link is atomic and never replaces an authoritative guard.
                    os.link(handle.name, path, follow_symlinks=False)
                except FileExistsError:
                    # Another creator published first; use its stable inode.
                    pass
                else:
                    descriptor = os.dup(handle.fileno())
            return descriptor if descriptor is not None else os.open(path, flags)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise


@contextmanager
def management_locks(directory, binary):
    """Serialize every manager that shares an entry or payload resource.

    Guards live outside the directories that publication/uninstall replace.
    They are persistent: unlinking a guard could split current and new holders.
    """
    specifications = [(safe_path(binary.parent.parent / f'.{binary.parent.name}.lpauto-entry.lock'), 'entry', binary),
                      (safe_path(directory.parent / f'.{directory.name}.lpauto-payload.lock'), 'payload', directory)]
    descriptors = []
    try:
        for path, kind, resource in sorted(specifications, key=lambda item: str(item[0])):
            path.parent.mkdir(parents=True, exist_ok=True)
            expected = (json.dumps({'schema': 'open_trader.lpauto.management-lock.v1',
                                    'kind': kind, 'resource': str(resource)}, sort_keys=True) + '\n').encode()
            descriptor = open_management_guard(path, expected)
            descriptors.append(descriptor)
            observed = os.fstat(descriptor)
            require(stat.S_ISREG(observed.st_mode) and observed.st_uid == os.getuid())
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallationBusy() from exc
            # The marker is immutable under this protocol. A killed publisher
            # may leave its private staging link: extra links to this same valid
            # inode do not split lock holders or authorize marker rewrites.
            os.lseek(descriptor, 0, os.SEEK_SET)
            require(os.read(descriptor, len(expected) + 1) == expected)
            current = os.stat(path, follow_symlinks=False)
            require((current.st_dev, current.st_ino) == (observed.st_dev, observed.st_ino))
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def install(args):
    parser = argparse.ArgumentParser(description='Explicit local lpauto installation management')
    parser.add_argument('mode', choices=('init', 'repair', 'uninstall'))
    parser.add_argument('--launcher-python')
    parser.add_argument('--bin-dir', default=str(Path.home() / '.local/bin'))
    parser.add_argument('--payload-dir', default=str(Path.home() / '.local/share/open-trader/lp-auto'))
    parser.add_argument('--plist', default=str(Path.home() / f'Library/LaunchAgents/{LABEL}.plist'))
    for name, default in (('launchctl', '/bin/launchctl'), ('lsof', '/usr/sbin/lsof'),
                          ('ps', '/bin/ps'), ('git', '/usr/bin/git')):
        parser.add_argument('--' + name, default=default)
    options = parser.parse_args(args)
    directory = safe_path(safe_path(options.payload_dir).resolve())
    binary = safe_path(safe_path(Path(options.bin_dir) / 'lpauto').resolve())
    with management_locks(directory, binary):
        return manage_installation(options, directory, binary)


def manage_installation(options, directory, binary):
    versions, snapshot = publication_state(directory, binary)
    if options.mode == 'uninstall':
        if versions:
            binary.unlink(missing_ok=True)
            for name in OWNED:
                (directory / name).unlink(missing_ok=True)
            (directory / TRANSACTION).unlink(missing_ok=True)
            if not any(directory.iterdir()):
                directory.rmdir()
        return 0
    bootstrap = options.launcher_python
    require(bootstrap and Path(bootstrap).is_absolute() and os.access(bootstrap, os.X_OK))
    config = {'plist': str(Path(options.plist).absolute()), 'bootstrap': bootstrap,
              'tools': {name: getattr(options, name) for name in ('launchctl', 'lsof', 'ps', 'git')}}
    for path in config['tools'].values():
        require(Path(path).is_absolute() and os.access(path, os.X_OK))
    raw = Path(__file__).read_bytes()
    source_sha = source_identity(config['tools']['git'], raw)
    error = json.dumps({'result': 'UNKNOWN', 'state': None, 'reason': 'LAUNCHER_BOOTSTRAP_MISSING', 'next_action': NEXT})
    entry = ('#!/bin/sh\n# open_trader.lpauto.install.v1\n'
             f'if [ ! -x {shlex.quote(bootstrap)} ]; then\n'
             f'  case " $* " in *" --json "*) printf "%s\\n" {shlex.quote(error)};;\n'
             f'  *) printf "%s\\n" "LAUNCHER_BOOTSTRAP_MISSING: explicit repair required" >&2;; esac\n'
             '  exit 2\nfi\n'
             f'exec {shlex.quote(bootstrap)} -I -B {shlex.quote(str(directory / "launcher.py"))} --installed "$@"\n').encode()
    config_raw = (json.dumps(config, sort_keys=True, indent=2) + '\n').encode()
    receipt = {'schema': 'open_trader.lpauto.install.v1', 'binary': str(binary), 'payload': str(directory),
               'source_sha': source_sha, 'source_sha256': digest(raw),
               'config_sha256': digest(config_raw), 'entry_sha256': digest(entry)}
    with release_lock(config):
        resolve(config)
        contents = {'launcher.py': raw, 'config.json': config_raw,
                    'receipt.json': (json.dumps(receipt, sort_keys=True, indent=2) + '\n').encode()}
        publish(directory, binary, contents, entry, versions, snapshot)
    return 0


def installed(args):
    if args in ([], ['--help'], ['-h']):
        print('lpauto: current managed macOS Prediction LP Auto CLI\n'
              'Usage: lpauto status|config|on|off|pause [LP options]\n'
              '       lpauto --version [--json]\n'
              'Installation and repair are explicit source-script operations.')
        return 0
    directory = safe_path(Path(__file__).parent)
    if (directory / TRANSACTION).exists():
        raise InstallationIncomplete()
    receipt = json.loads((directory / 'receipt.json').read_text())
    config_raw = (directory / 'config.json').read_bytes()
    require(digest(Path(__file__).read_bytes()) == receipt['source_sha256'])
    require(digest(config_raw) == receipt['config_sha256'])
    config = json.loads(config_raw)
    with release_lock(config) as lock_fd:
        return launch(config, receipt, args, lock_fd)


@contextmanager
def release_lock(config):
    lock_path = Path(config['plist']).parent / f'.{LABEL}.release.lock'
    lock_fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentBusy() from exc
        yield lock_fd
    finally:
        os.close(lock_fd)


def launch(config, receipt, args, lock_fd):
    urls = []
    for index, arg in enumerate(args):
        require(arg.split('=', 1)[0] not in ('--u', '--ur'))
        if arg == '--url':
            require(index + 1 < len(args))
            urls.append(url_identity(args[index + 1]))
        elif arg.startswith('--url='):
            urls.append(url_identity(arg.split('=', 1)[1]))
    target = resolve(config, urls)
    if args in (['--version'], ['--version', '--json']):
        version = {'launcher': {key: receipt[key] for key in ('source_sha', 'source_sha256')},
                   'deployment': target}
        if '--json' in args:
            print(json.dumps(version, sort_keys=True))
        else:
            print(f'Launcher source: {receipt["source_sha"]} ({receipt["source_sha256"]})\n'
                  f'Prediction release: {target["sha"]}\nRoot: {target["root"]}\n'
                  f'Code root: {target["code_root"]}\nInterpreter: {target["interpreter"]}')
        return 0
    env = tool_environment()
    env.update(PYTHONPATH=target['code_root'], PYTHONDONTWRITEBYTECODE='1')
    tail = list(args)
    if '--url' not in tail and not any(arg.startswith('--url=') for arg in tail):
        tail += ['--url', target['url']]
    os.chdir(target['root'])
    os.set_inheritable(lock_fd, True)
    os.execve(target['interpreter'], [target['interpreter'], '-B', '-s', '-P', '-m',
                                    'open_trader', 'prediction-arb', 'lp-auto', *tail], env)


def argument(args, flag):
    indices = [i for i, item in enumerate(args) if item == flag]
    require(len(indices) == 1 and indices[0] + 1 < len(args))
    return args[indices[0] + 1]


def url_identity(url):
    parsed = urlsplit(url)
    require(parsed.scheme == 'http' and parsed.username is None and parsed.password is None)
    require(parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
    address = ipaddress.ip_address(parsed.hostname or '')
    require(address.is_loopback)
    return str(address), parsed.port or 80


def resolve(config, urls=()):
    plist_raw = Path(config['plist']).read_bytes()
    plist = plistlib.loads(plist_raw)
    require(plist.get('Label') == LABEL)
    args = plist['ProgramArguments']
    root = str(Path(plist['WorkingDirectory']).resolve())
    interpreter = args[0]  # Keep the venv executable spelling, not its realpath.
    require(Path(interpreter).is_absolute() and os.access(interpreter, os.X_OK))
    require(argument(args, '-m') == 'open_trader' and args[args.index('-m') + 2] == 'prediction-service')
    require(argument(args, '--mode') == 'production')
    require(argument(args, '--host') == '127.0.0.1')
    port = int(argument(args, '--port'))
    require(0 < port < 65536)
    require(all(endpoint == ('127.0.0.1', port) for endpoint in urls))
    code_root = str(Path(root) / 'src')
    require(plist['EnvironmentVariables']['PYTHONPATH'] == code_root)
    record_path = Path(argument(args, '--data-dir')).parent / 'prediction-service-runtime.json'
    record_raw = record_path.read_bytes()
    record = json.loads(record_raw)
    require(record['schema_version'] == 'open_trader.prediction_service.runtime.v1' and record['state'] == 'ready')
    candidate = record['candidate']
    require(candidate['checkout'] == root and candidate['source_state'] == 'clean')
    def git(*arguments):
        return run([config['tools']['git'], '--no-optional-locks', '-C', root, *arguments])
    require(git('rev-parse', '--show-toplevel') == root)
    require(git('rev-parse', 'HEAD') == candidate['git_sha'])
    require(git('rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD')
    require(not git('status', '--porcelain', '--untracked-files=all'))
    require(all(line[0] not in 'Sabcdefghijklmnopqrstuvwxyz' for line in git('ls-files', '-v').splitlines()))
    require(not git('ls-files', '--others', '--ignored', '--exclude-standard', '--', 'src', 'scripts'))
    manifest_path = Path(argument(args, '--release-manifest'))
    require(manifest_path.is_absolute() and manifest_path.resolve() == manifest_path)
    relative_manifest = manifest_path.relative_to(root).as_posix()
    git('ls-files', '--error-unmatch', '--', relative_manifest)
    git('cat-file', '-e', 'HEAD:' + relative_manifest)
    manifest = json.loads(manifest_path.read_bytes())
    require(set(manifest) == {'schema_version', 'reader_generation', 'contract_generation'})
    require(manifest['schema_version'] == 'open_trader.prediction_service.release.v1')
    for key in ('reader_generation', 'contract_generation'):
        require(type(manifest[key]) is int and manifest[key] > 0 and candidate[key] == manifest[key])
    require(candidate.get('manifest', str(manifest_path)) == str(manifest_path))
    manager = run([config['tools']['launchctl'], 'print', f'gui/{os.getuid()}/{LABEL}'])
    loaded = parse_manager(manager)
    require(loaded['path'] == config['plist'] and loaded['cwd'] == root and loaded['args'] == args)
    pid = loaded['pid']
    cwd_output = run([config['tools']['lsof'], '-a', '-p', str(pid), '-d', 'cwd', '-Fn'])
    require([line[1:] for line in cwd_output.splitlines() if line.startswith('n')] == [root])
    listener = run([config['tools']['lsof'], '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-Fn'])
    require(lsof_names(listener) == [(pid, f'127.0.0.1:{port}')])
    runtime_lock = str(Path(argument(args, '--data-dir')) / 'prediction_arbitrage/runtime.lock')
    owners = run([config['tools']['lsof'], '-t', runtime_lock])
    require({int(line) for line in owners.splitlines()} == {pid})
    started = run([config['tools']['ps'], '-p', str(pid), '-o', 'lstart='])
    require(bool(started))
    datetime.strptime(started, '%a %b %d %H:%M:%S %Y')
    url = f'http://127.0.0.1:{port}'
    opener = build_opener(ProxyHandler({}), NoRedirects())
    with opener.open(url + '/healthz', timeout=5) as response:
        require(response.status == 200)
        health = json.loads(response.read(1024 * 1024))
    require(isinstance(health, dict))
    expected = {'schema_version': 'open_trader.prediction_service.health.v1', 'module': 'prediction_service',
                'status': 'running', 'mode': 'production', 'production_owner': True, 'mutations': 'enabled',
                'pid': pid, 'cwd': root, 'code_root': code_root, 'git_sha': candidate['git_sha'],
                'source_state': 'clean', 'release_schema_version': manifest['schema_version'],
                'reader_generation': manifest['reader_generation'], 'contract_generation': manifest['contract_generation']}
    require(all(health.get(key) == value and type(health.get(key)) is type(value) for key, value in expected.items()))
    health_start = datetime.fromisoformat(health['started_at'])
    require(health_start.tzinfo is not None)
    env = tool_environment()
    env.update(PYTHONPATH=code_root, PYTHONDONTWRITEBYTECODE='1')
    probe = 'import importlib.util; print(importlib.util.find_spec("open_trader").origin)'
    origin = run([interpreter, '-B', '-s', '-P', '-c', probe], cwd=root, env=env)
    require(origin == str(Path(code_root) / 'open_trader/__init__.py'))
    # Reobserve external identities after the probe, before version/delegation.
    # This is a consistency check, not a recovery or a retry of an LP command.
    require(Path(config['plist']).read_bytes() == plist_raw and record_path.read_bytes() == record_raw)
    require(parse_manager(run([config['tools']['launchctl'], 'print', f'gui/{os.getuid()}/{LABEL}'])) == loaded)
    require(run([config['tools']['ps'], '-p', str(pid), '-o', 'lstart=']) == started)
    require(run([config['tools']['lsof'], '-a', '-p', str(pid), '-d', 'cwd', '-Fn']) == cwd_output)
    require(lsof_names(run([config['tools']['lsof'], '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-Fn'])) == [(pid, f'127.0.0.1:{port}')])
    require({int(line) for line in run([config['tools']['lsof'], '-t', runtime_lock]).splitlines()} == {pid})
    require(git('rev-parse', 'HEAD') == candidate['git_sha'] and not git('status', '--porcelain', '--untracked-files=all'))
    saved = record['ready']
    require(isinstance(saved, dict))
    return {'root': root, 'sha': candidate['git_sha'], 'code_root': code_root,
            'interpreter': interpreter, 'url': url, 'record': str(record_path), 'pid': pid,
            'process_started_at': health['started_at'],
            'saved_observation_current': saved.get('pid') == pid and saved.get('process_started_at') == health['started_at']}


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_manager(text):
    def unquote(value):
        return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value

    def field(name):
        values = re.findall(r'^\s*' + re.escape(name) + r'\s*=\s*(.+?)\s*$', text, re.M)
        require(len(values) == 1)
        return unquote(values[0])
    blocks = re.findall(r'^\s*arguments\s*=\s*\{\s*\n(.*?)^\s*\}', text, re.M | re.S)
    require(len(blocks) == 1)
    args = [unquote(line.strip()) for line in blocks[0].splitlines() if line.strip()]
    pid = int(field('pid'))
    require(pid > 0)
    return {'path': field('path'), 'cwd': field('working directory'), 'args': args, 'pid': pid}


def lsof_names(text):
    pid = None
    names = []
    for line in text.splitlines():
        if line.startswith('p'):
            pid = int(line[1:])
        elif line.startswith('n'):
            names.append((pid, line[1:]))
    return names


def main():
    args = sys.argv[1:]
    try:
        if args[:1] == ['--installed']:
            return installed(args[1:])
        return install(args)
    except (Unverified, OSError, ValueError, KeyError, TypeError, IndexError, HTTPException, subprocess.SubprocessError) as exc:
        reason = exc.reason if isinstance(exc, Unverified) else 'DEPLOYED_TARGET_UNVERIFIABLE'
        advice = getattr(exc, 'next_action', NEXT)
        if '--json' in args:
            print(json.dumps({'result': 'UNKNOWN', 'state': None, 'reason': reason, 'next_action': advice}))
        else:
            print(f'{reason}: {advice}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
