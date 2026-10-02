#!/usr/bin/env python3
"""Prove actual collected backend nodeids equal the four required CI partitions.

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


def verify_partition(complete, partitions):
    if set(partitions) != set(SCOPES) or not complete or any(not partitions[key] for key in SCOPES):
        raise ValueError('missing or empty backend collection')
    selected = [node for scope in SCOPES for node in partitions[scope]]
    if len(complete) != len(set(complete)) or len(selected) != len(set(selected)):
        raise ValueError('duplicate collected nodeids')
    missing, extra = set(complete) - set(selected), set(selected) - set(complete)
    if missing or extra:
        raise ValueError(f'backend partition mismatch: missing={sorted(missing)!r}; extra={sorted(extra)!r}')
    return {'schema_version': 1, 'status': 'success', 'marker': MARKER,
            'complete_count': len(complete), 'partition_count': len(selected),
            'counts': {scope: len(partitions[scope]) for scope in SCOPES},
            'complete_sha256': fingerprint(complete), 'partition_sha256': fingerprint(selected)}


def collect(root, paths, output):
    env = dict(os.environ, CI_COLLECTION_OUTPUT=str(output), PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=os.pathsep.join([str(root), str(root / 'src')]))
    command = [sys.executable, '-m', 'pytest', '--collect-only', '-q', '-p', 'scripts.ci_partition',
               '-m', MARKER, '-o', 'cache_dir=/tmp/open-trader-collection-cache', *paths]
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'collection failed ({result.returncode}):\n{result.stdout}\n{result.stderr}')
    return json.loads(output.read_text())


def main():
    from ci_evidence import service_partitions
    root = Path(__file__).resolve().parents[1]
    selections = service_partitions(root)
    with tempfile.TemporaryDirectory(prefix='open-trader-ci-collection-') as directory:
        directory = Path(directory)
        # Match the global backend command's configured testpaths, including future additions.
        complete = collect(root, [], directory / 'complete.json')
        partitions = {scope: collect(root, selections[scope], directory / f'{scope}.json') for scope in SCOPES}
    print(json.dumps(verify_partition(complete, partitions), sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
