#!/usr/bin/env python3
"""Read-only deployment preflight: trusted CI, source identity, runtime lock match.

GitHub evidence is fetched directly through existing gh authentication, never from
caller-supplied proof files. CI images/artifacts are test-only. The deployable unit
is the clean detached source checkout plus the explicitly selected existing Python
environment. Installed package versions are checked, not wheel/build provenance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tomllib
import zipfile

REPOSITORY = 'raymizzou/open_trader'
WORKFLOW = '.github/workflows/ci.yml'
SCOPES = ('gateway', 'legacy', 'account', 'prediction', 'portable')
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


class PreflightError(ValueError):
    """Missing, stale, contradictory, or unavailable evidence blocks deployment."""


def require(condition, message):
    if not condition:
        raise PreflightError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def execute(command, **kwargs):
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=120, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PreflightError(f'Cannot run {command[0]}: {error}') from error
    require(result.returncode == 0, f'{command[0]} failed: {result.stderr.decode(errors="replace").strip()}')
    return result.stdout


class GitHub:
    """Pinned GitHub host/repository with read-only API operations and pagination."""

    def raw(self, path):
        require(path.startswith(f'repos/{REPOSITORY}/'), 'Untrusted API target')
        return execute(['gh', 'api', '--hostname', 'github.com', '--method', 'GET',
                        '-H', 'Accept: application/vnd.github+json',
                        '-H', 'X-GitHub-Api-Version: 2022-11-28', path])

    def json(self, path):
        return json.loads(self.raw(path))

    def items(self, path, key):
        values = []
        separator = '&' if '?' in path else '?'
        for page in range(1, 101):
            data = self.json(f'{path}{separator}per_page=100&page={page}')
            chunk = data[key]
            require(isinstance(chunk, list), f'Malformed {key} response')
            values.extend(chunk)
            total = data['total_count']
            require(type(total) is int and total >= len(values), f'Inconsistent {key} pagination')
            if len(values) == total:
                return values
            require(len(chunk) == 100, f'Incomplete {key} pagination')
        raise PreflightError(f'{key} pagination exceeded safety limit')

    def download(self, artifact_id):
        require(type(artifact_id) is int and artifact_id > 0, 'Invalid artifact ID')
        return self.raw(f'repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip')


def verify_checkout(root, expected_sha):
    require(bool(re.fullmatch('[0-9a-f]{40}', expected_sha)), 'Expected SHA must be 40 lowercase hex characters')
    root = Path(root)
    require(root.is_absolute() and root.is_dir(), 'Release root must be an existing absolute directory')
    def git(*args):
        return execute(['git', '--no-optional-locks', '-C', str(root), *args]).decode().strip()
    require(Path(git('rev-parse', '--show-toplevel')).resolve() == root.resolve(), 'Release root is not checkout root')
    require(git('rev-parse', 'HEAD') == expected_sha, 'Release checkout SHA mismatch')
    require(git('rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD', 'Release checkout must be detached')
    require(not git('status', '--porcelain', '--untracked-files=all'), 'Release checkout is dirty')
    require(all(line[0] not in 'Sabcdefghijklmnopqrstuvwxyz' for line in git('ls-files', '-v').splitlines()),
            'Release index hides tracked changes (skip-worktree/assume-unchanged)')
    require(not git('ls-files', '--others', '--ignored', '--exclude-standard', '--', 'src', 'scripts'),
            'Ignored files in source/scripts can change release behavior')


def expected_selection(scope, root):
    spec = importlib.util.spec_from_file_location('release_ci_evidence', Path(root) / 'scripts/ci_evidence.py')
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module.expected_selection(scope, Path(root))


def read_archive(data):
    require(len(data) <= MAX_ARCHIVE_BYTES, 'Evidence archive exceeds size limit')
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        require(sum(item.file_size for item in members) <= MAX_ARCHIVE_BYTES, 'Expanded evidence exceeds size limit')
        for item in members:
            name = item.filename
            require(not item.is_dir() and PurePosixPath(name).name == name and '\\' not in name
                    and name not in files and name not in ('.', '..'), 'Unsafe or duplicate evidence member')
            require((item.external_attr >> 16) & 0o170000 != 0o120000, 'Evidence symlinks are forbidden')
            files[name] = archive.read(item)
    return files


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None, 'Missing artifact time zone')
    return result


def validate_archive(data, scope, run, lock_hash, selection, bases):
    """Pure data validation shared by preflight and the non-executing release writer."""
    sha = run['head_sha']
    files = read_archive(data)
    metadata = json.loads(files['evidence.json'])
    fixed = dict(schema_version=1, source_sha=sha, repository=REPOSITORY,
                 workflow_ref=f'{REPOSITORY}/{WORKFLOW}@refs/heads/main', event_name='push', ref='refs/heads/main',
                 run_id=str(run['id']), run_attempt=str(run['run_attempt']), scope=scope, test_n_leg='1',
                 workers=2 if scope == 'prediction' else 1, lock_sha256=lock_hash, status='success', exit_status=0,
                 evidence_role='test-only', selection=selection)
    require(all(metadata.get(key) == value and type(metadata.get(key)) is type(value)
                for key, value in fixed.items()), f'{scope} test metadata mismatch')
    require(metadata['environment'].get('runner_os') == 'Linux'
            and metadata['environment'].get('runner_arch') == 'X64', f'{scope} runner environment mismatch')
    for filename, field in [('dependency-manifest.json', 'dependency_manifest_sha256'),
                            ('image.json', 'image_inspect_sha256')]:
        require(metadata['environment'].get(field) == sha256(files[filename]), f'{scope} environment hash mismatch')
    manifest = json.loads(files['dependency-manifest.json'])
    require(manifest.get('role') == 'test-only; synthetic Git snapshot is not a release SHA'
            and manifest.get('source_sha') == sha and manifest.get('source_state') == 'clean'
            and manifest.get('lock_sha256') == lock_hash, f'{scope} test environment identity mismatch')
    require(bases and all('@sha256:' in base for base in bases) and manifest.get('base_images') == bases,
            f'{scope} test base-image identity mismatch')
    require(manifest.get('python') == '3.12.14' and manifest.get('platform') == 'Linux/x86_64'
            and bool(manifest.get('dependencies')), f'{scope} test environment metadata missing or unsupported')
    images = json.loads(files['image.json'])
    require(len(images) == 1 and bool(re.fullmatch('sha256:[0-9a-f]{64}', images[0]['Id'])),
            f'{scope} image identity missing')
    config = images[0]['Config']
    require(config['Labels'].get('org.open-trader.image-role') == 'test-only'
            and f'OPEN_TRADER_TEST_SOURCE_SHA={sha}' in config['Env']
            and 'OPEN_TRADER_TEST_SOURCE_STATE=clean' in config['Env'], f'{scope} image is not exact-SHA test-only evidence')
    if scope == 'portable':
        require(metadata['environment'].get('partition_sha256') == sha256(files['partition.json']),
                'Partition evidence digest mismatch')
        partition = json.loads(files['partition.json'])
        counts = partition['counts']
        require(partition.get('schema_version') == 1 and partition.get('status') == 'success'
                and partition.get('marker') == 'not pressure and not browser'
                and set(counts) == set(SCOPES)-{'portable'} and all(type(n) is int and n > 0 for n in counts.values())
                and partition['complete_count'] == partition['partition_count'] == sum(counts.values())
                and bool(re.fullmatch('[0-9a-f]{64}', partition['complete_sha256']))
                and partition['complete_sha256'] == partition['partition_sha256'], 'Incomplete backend partition evidence')


def verify_artifact(api, artifact, scope, run, root, now):
    sha = run['head_sha']
    require(artifact.get('expired') is False, f'{scope} artifact expired or expiration unknown')
    created, expires = timestamp(artifact['created_at']), timestamp(artifact['expires_at'])
    require(created <= now < expires and timedelta(0) < expires-created <= timedelta(days=3),
            f'{scope} artifact is expired or exceeds three-day retention')
    identity = artifact['workflow_run']
    require(identity['id'] == run['id'] and identity['head_sha'] == sha and identity['head_branch'] == 'main',
            f'{scope} artifact run/source mismatch')
    data = api.download(artifact['id'])
    require(artifact.get('digest') == f'sha256:{sha256(data)}', f'{scope} artifact digest mismatch or missing')
    validate_archive(data, scope, run, sha256((root/'uv.lock').read_bytes()),
                     expected_selection(scope, root),
                     re.findall(r'^FROM (\S+)', (root/'Dockerfile.dev').read_text(), re.MULTILINE))
    return {'scope':scope, 'id':artifact['id'], 'digest':artifact['digest']}


def verify_main_membership(api, sha):
    prefix = f'repos/{REPOSITORY}'
    tip = api.json(f'{prefix}/git/ref/heads/main')['object']['sha']
    require(bool(re.fullmatch('[0-9a-f]{40}', tip)), 'Invalid live main reference')
    if tip != sha:
        comparison = api.json(f'{prefix}/compare/{sha}...{tip}')
        require(comparison['status'] == 'ahead' and comparison['merge_base_commit']['sha'] == sha,
                'Selected final-main SHA is no longer retained on main')


def verify_ci(api, sha, root, now):
    prefix = f'repos/{REPOSITORY}'
    verify_main_membership(api, sha)
    workflow = api.json(f'{prefix}/actions/workflows/ci.yml')
    require(workflow['path'] == WORKFLOW and workflow['state'] == 'active', 'Trusted CI workflow unavailable')
    runs_path = f'{prefix}/actions/workflows/{workflow["id"]}/runs?branch=main&event=push&head_sha={sha}'
    runs = api.items(runs_path, 'workflow_runs')
    require(bool(runs), 'No trusted CI main-push run for selected SHA')
    run = max(runs, key=lambda item:(item['run_number'], item['id']))
    require(run.get('status') == 'completed' and run.get('conclusion') == 'success',
            'Latest main-push CI run has not completed successfully')
    run = api.json(f'{prefix}/actions/runs/{run["id"]}')
    for key, value in dict(workflow_id=workflow['id'], path=WORKFLOW, event='push', head_branch='main',
                           head_sha=sha, status='completed', conclusion='success').items():
        require(run.get(key) == value, f'CI run {key} mismatch')
    require(run['repository']['full_name'] == REPOSITORY and run['head_repository']['full_name'] == REPOSITORY,
            'CI run repository mismatch')
    require(type(run['run_attempt']) is int and run['run_attempt'] > 0, 'CI attempt missing')
    jobs = api.items(f'{prefix}/actions/runs/{run["id"]}/attempts/{run["run_attempt"]}/jobs', 'jobs')
    needed = ('plan', *SCOPES, 'required')
    selected = {}
    for name in needed:
        matches = [job for job in jobs if job['name'] == name]
        require(len(matches) == 1, f'Missing or duplicate CI job: {name}')
        job = matches[0]
        require(job['run_id'] == run['id'] and job['head_sha'] == sha
                and job['status'] == 'completed' and job['conclusion'] == 'success', f'{name} job not successful for selected run/SHA')
        selected[name] = job
    checks = api.items(f'{prefix}/check-suites/{run["check_suite_id"]}/check-runs?filter=latest', 'check_runs')
    required = [check for check in checks if check['name'] == 'required']
    require(len(required) == 1, 'Missing or duplicate required check')
    check = required[0]
    require(check['app']['id'] == 15368 and check['check_suite']['id'] == run['check_suite_id']
            and check['head_sha'] == sha and check['status'] == 'completed' and check['conclusion'] == 'success'
            and selected['required']['check_run_url'] == f'https://api.github.com/{prefix}/check-runs/{check["id"]}',
            'Required check is not successful GitHub Actions evidence from selected run/suite/SHA')
    artifacts = api.items(f'{prefix}/actions/runs/{run["id"]}/artifacts', 'artifacts')
    evidence = []
    for scope in SCOPES:
        name = f'ci-{scope}-{sha}-{run["id"]}-{run["run_attempt"]}'
        matches = [artifact for artifact in artifacts if artifact['name'] == name]
        require(len(matches) == 1, f'Missing or ambiguous current-attempt artifact: {scope}')
        evidence.append(verify_artifact(api, matches[0], scope, run, root, now))
    # Close the obvious mutable-run and main-reference race before returning.
    require(api.json(f'{prefix}/actions/runs/{run["id"]}') == run, 'CI run changed during verification')
    latest = max(api.items(runs_path, 'workflow_runs'), key=lambda item:(item['run_number'], item['id']))
    require(latest['id'] == run['id'] and latest['run_attempt'] == run['run_attempt'], 'A newer CI run/attempt appeared')
    verify_main_membership(api, sha)
    return {'run_id':run['id'], 'run_attempt':run['run_attempt'], 'artifact_role':'test-only',
            'run_url':f'https://github.com/{REPOSITORY}/actions/runs/{run["id"]}', 'artifacts':evidence}


def verify_runtime_packages(lock, installed, environment, extras):
    # packaging is already a locked runtime dependency; never install it here.
    from packaging.markers import Marker
    from packaging.specifiers import SpecifierSet
    require(environment['python_version'] == '3.12'
            and environment['python_full_version'] in SpecifierSet(lock['requires-python']),
            'Existing runtime must use the tested Python 3.12 minor and satisfy uv.lock')
    normalize = lambda name: re.sub(r'[-_.]+', '-', name).lower()
    packages = {}
    for package in lock['package']:
        packages.setdefault(normalize(package['name']), []).append(package)
    roots = packages.get('open-trader', [])
    require(len(roots) == 1 and roots[0]['source'] == {'editable':'.'}, 'Unsupported project lock identity')
    project = roots[0]
    require(set(extras) <= {'cloud-ssm', 'browser'}, 'Unsupported runtime extra')
    pending = list(project.get('dependencies', []))
    for extra in extras:
        require(extra in project.get('optional-dependencies', {}), f'Runtime extra absent from lock: {extra}')
        pending.extend(project['optional-dependencies'][extra])
    expected = {}
    expanded = set()
    while pending:
        edge = pending.pop()
        require(set(edge) <= {'name','version','source','marker','extra'}, 'Unsupported lock dependency edge')
        if edge.get('marker') and not Marker(edge['marker']).evaluate(environment):
            continue
        name = normalize(edge['name'])
        choices = [p for p in packages.get(name, []) if ('version' not in edge or p['version'] == edge['version'])
                   and ('source' not in edge or p['source'] == edge['source'])
                   and (not p.get('resolution-markers') or any(Marker(m).evaluate(environment) for m in p['resolution-markers']))]
        require(len(choices) == 1, f'Ambiguous or missing locked runtime dependency: {name}')
        package = choices[0]
        require(package['source'] == {'registry':'https://pypi.org/simple'}, f'Unsupported runtime package source: {name}')
        require(installed.get(name) == package['version'], f'Runtime dependency missing or not locked: {name}=={package["version"]}')
        require(name not in expected or expected[name] == package['version'], f'Conflicting runtime dependency: {name}')
        expected[name] = package['version']
        selected_extras = edge.get('extra', [])
        require(isinstance(selected_extras, list), f'Unsupported dependency extras: {name}')
        key = (name, tuple(selected_extras))
        if key in expanded:
            continue
        expanded.add(key)
        pending.extend(package.get('dependencies', []))
        for extra in selected_extras:
            require(extra in package.get('optional-dependencies', {}), f'Missing dependency extra: {name}[{extra}]')
            pending.extend(package['optional-dependencies'][extra])
    return dict(sorted(expected.items()))


def verify_runtime_isolation(prefix, base_prefix):
    prefix, base_prefix = Path(prefix), Path(base_prefix)
    require(prefix.resolve() != base_prefix.resolve(), 'Runtime must be an existing isolated virtual environment')
    configuration = (prefix/'pyvenv.cfg').read_text()
    flags = re.findall(r'^include-system-site-packages\s*=\s*(\S+)\s*$', configuration, re.MULTILINE | re.IGNORECASE)
    require(len(flags) == 1 and flags[0].lower() == 'false', 'Runtime virtual environment must exclude system/user site packages')


def inspect_runtime(root, extras):
    from importlib.metadata import distributions
    from packaging.markers import default_environment
    verify_runtime_isolation(sys.prefix, sys.base_prefix)
    root = Path(root).resolve()
    sys.path.insert(0, str(root/'src'))  # Matches installers' explicit release PYTHONPATH.
    spec = importlib.util.find_spec('open_trader')
    require(spec is not None and spec.origin is not None
            and Path(spec.origin).resolve() == root/'src/open_trader/__init__.py', 'Runtime resolves another open_trader source')
    installed = {}
    for distribution in distributions():
        name = re.sub(r'[-_.]+', '-', distribution.metadata['Name']).lower()
        require(name not in installed, f'Ambiguous installed distribution: {name}')
        installed[name] = distribution.version
    environment = default_environment()
    lock = tomllib.loads((root/'uv.lock').read_text())
    expected = verify_runtime_packages(lock, installed, environment, extras)
    return {'python':sys.executable, 'python_version':environment['python_full_version'],
            'platform':environment['sys_platform']+'/'+environment['platform_machine'],
            'source_root':str(root), 'lock_sha256':sha256((root/'uv.lock').read_bytes()),
            'extras':sorted(extras), 'dependencies':expected,
            'provenance':'installed-version lock match; wheel/build provenance is not available'}


def probe_runtime(python, root, extras):
    require(not any(os.environ.get(key) for key in ('PYTHONHOME', 'PYTHONUSERBASE')),
            'PYTHONHOME/PYTHONUSERBASE overrides make runtime identity unverifiable')
    require(Path(python).is_absolute() and Path(python).is_file(), 'Existing runtime --python must be an absolute executable path')
    probe = ('import json,runpy,sys; ns=runpy.run_path(sys.argv[1]); '
             'print(json.dumps(ns["inspect_runtime"](sys.argv[2],json.loads(sys.argv[3]))))')
    result = execute([python, '-I', '-B', '-c', probe, str(Path(__file__).resolve()), str(root), json.dumps(extras)])
    return json.loads(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--release-root', required=True, type=Path)
    parser.add_argument('--python', required=True)
    parser.add_argument('--extra', action='append', default=[], choices=['cloud-ssm','browser'])
    args = parser.parse_args(argv)
    try:
        verify_checkout(args.release_root, args.expected_sha)
        runtime = probe_runtime(args.python, args.release_root, args.extra)
        ci = verify_ci(GitHub(), args.expected_sha, args.release_root, datetime.now(timezone.utc))
        verify_checkout(args.release_root, args.expected_sha)
        require(probe_runtime(args.python, args.release_root, args.extra) == runtime, 'Runtime environment changed during verification')
        print(json.dumps({'status':'PASS', 'source_sha':args.expected_sha, 'release_root':str(args.release_root.resolve()),
                          'deployment_unit':'source checkout plus existing Python environment', 'runtime':runtime, 'ci':ci}, sort_keys=True))
        return 0
    except (PreflightError, KeyError, TypeError, ValueError, OSError, zipfile.BadZipFile, ImportError) as error:
        print(f'Deployment preflight: FAIL: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
