#!/usr/bin/env python3
"""Prove backend nodeids equal active partitions plus explicit retired nodeids.

Runs collection only, in independent interpreters, with the same marker filter as
make test. Missing nested files, changed collection rules, and overlaps fail CI.
Also serves as the tiny pytest plugin that writes selected nodeids as JSON.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SCOPES = ('gateway', 'legacy', 'account', 'prediction')
MARKER = 'not pressure and not browser'


def pytest_collection_finish(session):
    output = os.environ.get('CI_COLLECTION_OUTPUT')
    if output:
        Path(output).write_text(json.dumps([item.nodeid for item in session.items]))


def fingerprint(nodes):
    return hashlib.sha256(json.dumps(sorted(nodes), separators=(',', ':')).encode()).hexdigest()


def verify_partition(complete, partitions, retired=None, root=None):
    from importlib.util import spec_from_file_location, module_from_spec
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    spec = spec_from_file_location('partition_selection', Path(__file__).with_name('ci_evidence.py'))
    selection = module_from_spec(spec)
    spec.loader.exec_module(selection)
    policy = selection.retirement_policy(root)
    retired = [] if retired is None else retired
    if set(partitions) != set(SCOPES) or not complete or any(not partitions[key] for key in SCOPES):
        raise ValueError('missing or empty backend collection')
    executed = [node for scope in SCOPES for node in partitions[scope]]
    for nodes in (complete, executed, retired):
        if not isinstance(nodes, list) or any(not isinstance(node, str) or '::' not in node for node in nodes):
            raise ValueError('malformed collected nodeids')
        if len(nodes) != len(set(nodes)):
            raise ValueError('duplicate collected nodeids')
    if set(executed) & set(retired):
        raise ValueError('executed and retired collections overlap')
    if any(node.split('::', 1)[0] not in policy['retired_files'] for node in retired):
        raise ValueError('undeclared retired nodeid')
    if any(node.split('::', 1)[0] in policy['retired_files'] for node in executed):
        raise ValueError('retired nodeid masquerades as active coverage')
    combined = executed + retired
    missing, extra = set(complete) - set(combined), set(combined) - set(complete)
    if missing or extra:
        raise ValueError(f'backend partition mismatch: missing={sorted(missing)!r}; extra={sorted(extra)!r}')
    return {'schema_version': 2, 'status': 'success', 'marker': MARKER, **policy,
            'retired_status': 'not-executed-permanent-retirement',
            'complete_count': len(complete), 'partition_count': len(executed),
            'executed_count': len(executed), 'retired_count': len(retired),
            'complete': sorted(complete), 'partitions': {scope: sorted(partitions[scope]) for scope in SCOPES},
            'retired': sorted(retired),
            'counts': {scope: len(partitions[scope]) for scope in SCOPES},
            'complete_sha256': fingerprint(complete), 'partition_sha256': fingerprint(executed),
            'retired_sha256': fingerprint(retired), 'coverage_sha256': fingerprint(combined)}


def collect(root, paths, output):
    env = dict(os.environ, CI_COLLECTION_OUTPUT=str(output), PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=os.pathsep.join([str(root), str(root / 'src'), str(Path(__file__).resolve().parents[1])]))
    command = [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'scripts.ci_partition',
               '-m', MARKER, '-o', 'cache_dir=/tmp/open-trader-collection-cache', *paths]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'collection failed ({result.returncode}):\n{result.stdout}\n{result.stderr}')
    return json.loads(output.read_text())


def main():
    from ci_evidence import service_partitions, retirement_policy
    root = Path(__file__).resolve().parents[1]
    selections = service_partitions(root)
    with tempfile.TemporaryDirectory(prefix='open-trader-ci-collection-') as directory:
        directory = Path(directory)
        # Match the global backend command's configured testpaths, including future additions.
        complete = collect(root, [], directory / 'complete.json')
        partitions = {scope: collect(root, selections[scope], directory / f'{scope}.json') for scope in SCOPES}
        retired = collect(root, retirement_policy(root)['retired_files'], directory / 'retired.json')
    print(json.dumps(verify_partition(complete, partitions, retired, root), sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
