#!/usr/bin/env python3
"""Small, exact-run test evidence consumed by the deployment preflight."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

BACKEND_SCOPES = ('gateway', 'legacy', 'account', 'prediction')
MARKER = 'not pressure and not browser'


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def service_partitions(root):
    output = subprocess.check_output(
        ['make', '--no-print-directory', '-s', 'ci-test-files'], cwd=root, text=True)
    parts = {}
    for line in output.splitlines():
        scope, paths = line.split(':', 1)
        if scope not in BACKEND_SCOPES or scope in parts:
            raise ValueError('invalid or duplicate service partition')
        parts[scope] = paths.split()
        if not parts[scope] or parts[scope] != sorted(set(parts[scope])):
            raise ValueError('empty, unsorted or duplicate service selection')
    if set(parts) != set(BACKEND_SCOPES):
        raise ValueError('incomplete service partitions')
    return parts


def expected_selection(scope, root):
    if scope == 'portable':
        roots = ['acceptance/test_prediction_arbitrage_scenarios.py']
    elif scope == 'trend-curve':
        roots = ['tests/test_trend_curve_research.py', 'tests/test_trend_curve_backtest.py',
                 'tests/test_trend_curve_cli.py']
    elif scope in BACKEND_SCOPES:
        roots = service_partitions(root)[scope]
    else:
        raise ValueError('unknown CI scope')
    return {'roots': roots, 'marker': MARKER, 'keyword': 'not LIVE' if scope == 'portable' else ''}


def build_evidence(root, directory, scope, nleg, workers, status, env):
    if nleg != '1' or workers != (2 if scope == 'prediction' else 1):
        raise ValueError('CI requires N-leg and the declared serial/loadgroup policy')
    for key, pattern in [('GITHUB_SHA', '[0-9a-f]{40}'), ('GITHUB_RUN_ID', '[1-9][0-9]*'),
                         ('GITHUB_RUN_ATTEMPT', '[1-9][0-9]*')]:
        if not re.fullmatch(pattern, env.get(key, '')):
            raise ValueError('missing or invalid ' + key)
    for key in ('GITHUB_REPOSITORY', 'GITHUB_WORKFLOW_REF', 'GITHUB_EVENT_NAME', 'GITHUB_REF'):
        if not env.get(key):
            raise ValueError('missing ' + key)
    environment = {'runner_os': env.get('RUNNER_OS', ''), 'runner_arch': env.get('RUNNER_ARCH', '')}
    for field, filename in [('dependency_manifest_sha256', 'dependency-manifest.json'),
                            ('image_inspect_sha256', 'image.json')]:
        path = Path(directory) / filename
        environment[field] = file_sha256(path) if path.is_file() else None
    if scope == 'portable':
        path = Path(directory) / 'partition.json'
        environment['partition_sha256'] = file_sha256(path) if path.is_file() else None
    # Missing evidence can never describe a successful run, even if invoked incorrectly.
    if status == 0 and any(value is None for value in environment.values()):
        raise ValueError('successful CI requires complete environment/partition evidence')
    return {
        'schema_version': 1, 'evidence_role': 'test-only', 'source_sha': env['GITHUB_SHA'],
        'repository': env['GITHUB_REPOSITORY'], 'workflow_ref': env['GITHUB_WORKFLOW_REF'],
        'event_name': env['GITHUB_EVENT_NAME'], 'ref': env['GITHUB_REF'],
        'run_id': env['GITHUB_RUN_ID'], 'run_attempt': env['GITHUB_RUN_ATTEMPT'],
        'scope': scope, 'test_n_leg': nleg, 'workers': workers,
        'lock_sha256': file_sha256(Path(root) / 'uv.lock'),
        'status': 'success' if status == 0 else 'failure', 'exit_status': status,
        'selection': expected_selection(scope, root), 'environment': environment,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scope')
    parser.add_argument('nleg')
    parser.add_argument('workers', type=int)
    parser.add_argument('status', type=int)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    evidence = build_evidence(root, args.directory, args.scope, args.nleg, args.workers, args.status, os.environ)
    (args.directory / 'evidence.json').write_text(json.dumps(evidence, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
