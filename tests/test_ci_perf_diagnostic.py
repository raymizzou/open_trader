"""Focused offline contracts; safe for stock-Python unittest discovery."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OBSERVER = ROOT / 'scripts/ci_perf_diagnostic.py'


def harness():
    spec = importlib.util.spec_from_file_location('diagnostic_contract', OBSERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def child(tmp_path, source):
    sample = tmp_path / 'sample'
    sample.mkdir()
    (sample / 'test_sample.py').write_text(source)
    (sample / 'pytest.ini').write_text('[pytest]\n')
    observations = tmp_path / 'observations'
    env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
           'PYTHONPATH': os.pathsep.join([str(OBSERVER.parent), str(ROOT), str(ROOT/'src')]),
           'PYTHONDONTWRITEBYTECODE': '1', 'CI_PERF_OBSERVATIONS': str(observations),
           'CI_TEST_METRICS': str(tmp_path / 'metrics.json'), 'CI_TEST_WORKERS': '6'}
    command = [sys.executable, '-m', 'pytest', '-q', '-p', 'xdist.plugin',
               '-p', 'ci_perf_diagnostic', '-p', 'scripts.ci_test_metrics', '-n', '6',
               '--dist=loadgroup', '--max-worker-restart=0', '-o', 'cache_dir='+str(tmp_path/'cache'),
               '--junitxml='+str(tmp_path/'junit.xml')]
    result = subprocess.run(command, cwd=sample, env=env, capture_output=True, text=True, timeout=90)
    (tmp_path / 'child.log').write_text(result.stdout + result.stderr)
    (tmp_path / 'child-execution.json').write_text(json.dumps(dict(command=command, exit_status=result.returncode,
        intentional_fixture_failure=True, cwd=str(sample)), indent=2))
    summary = harness().summarize(observations, result.returncode)
    (tmp_path / 'summary.json').write_text(json.dumps(summary, indent=2))
    return result, summary, observations


def test_six_worker_observation_preserves_outcomes_and_groups(tmp_path):
    result, summary, observations = child(tmp_path, '''
import pytest
import unittest

@pytest.mark.parametrize('value', [pytest.param(f'{g}-{n}@literal',
    marks=pytest.mark.xdist_group(name=f'group-{g}')) for g in range(6) for n in range(2)])
def test_group(value):
    assert '@literal' in value

def test_failure():
    assert False, 'intentional diagnostic fixture failure, not a backend regression'

@pytest.mark.skip(reason='intentional fixture skip')
def test_skip():
    pass

class Cases(unittest.TestCase):
    def test_children(self):
        for value in (1, 2):
            with self.subTest(value=value):
                self.assertGreater(value, 0)
''')
    assert result.returncode == 1, result.stdout + result.stderr
    assert summary['exit_status'] == 1
    assert summary['complete'] is True
    expected = [f'test_sample.py::test_group[{g}-{n}@literal]' for g in range(6) for n in range(2)]
    expected += ['test_sample.py::test_failure', 'test_sample.py::test_skip', 'test_sample.py::Cases::test_children']
    assert summary['selected_nodeids'] == sorted(expected)
    assert summary['observed_nodeids'] == sorted(expected)
    metrics = json.loads((tmp_path/'metrics.json').read_text())
    assert metrics['complete'] and metrics['exit_status'] == 1
    results = {row['nodeid']: row for row in metrics['results']}
    assert results['test_sample.py::test_failure']['outcome'] == 'failed'
    assert results['test_sample.py::test_skip']['outcome'] == 'skipped'
    assert [node for node, row in results.items() if row['outcome'] == 'failed'] == ['test_sample.py::test_failure']
    assert len(results['test_sample.py::Cases::test_children']['subtests']) == 2
    assert all(row['outcome'] == 'passed' for row in results['test_sample.py::Cases::test_children']['subtests'])
    owners, phases = {}, []
    for worker, data in summary['workers'].items():
        assert data['complete']
        previous = None
        for row in data['phases']:
            assert row['end_ns'] >= row['start_ns']
            assert previous is None or row['start_ns'] >= previous
            previous = row['end_ns']
            owners[row['nodeid']] = worker
            phases.append(row)
        assert data['active_union_seconds'] <= sum(data['phase_seconds'].values()) + 1e-8
    for group in range(6):
        assert owners[f'test_sample.py::test_group[{group}-0@literal]'] == owners[f'test_sample.py::test_group[{group}-1@literal]']
    assert len([p for p in phases if p['nodeid'].endswith('Cases::test_children') and p['when'] == 'call']) == 1
    raw = [json.loads(line) for path in observations.glob('gw*.jsonl') for line in path.read_text().splitlines()]
    children = [event for event in raw if event['event'] == 'subtest']
    assert len(children) == 2 and all(event['outcome'] == 'passed' for event in children)
    assert all('start_ns' not in event for event in children)
    # A fixture clock patch cannot replace the observer's saved monotonic callable.
    module = harness()
    original = module.MONOTONIC_NS
    import time
    before = original()
    from unittest.mock import patch
    with patch.object(time, 'monotonic_ns', return_value=-1):
        assert module.MONOTONIC_NS() >= before
    module.render_timeline(summary, tmp_path/'timeline.svg')
    assert 'gw5' in (tmp_path/'timeline.svg').read_text()


def test_incomplete_observation_is_not_reported_as_complete(tmp_path):
    result, summary, observations = child(tmp_path, '''
import os

def test_crash():
    os._exit(7)
''')
    assert result.returncode != 0
    assert summary['complete'] is False
    assert summary['exit_status'] == result.returncode
    streams = list(observations.glob('gw*.jsonl'))
    assert streams and any('phase_start' in path.read_text() for path in streams)
    path = streams[0]
    prefix = path.read_text()
    with path.open('a') as stream:
        stream.write('{"partial":')
    partial = harness().summarize(observations, result.returncode)
    assert not partial['complete'] and partial['stream_errors']
    assert path.read_text().startswith(prefix)  # Usable stream evidence is never removed.
    missing = harness().summarize(tmp_path/'missing', 0)
    assert not missing['complete'] and missing['selected_nodeids'] == []


def test_workflow_is_scoped_and_separate_from_trusted_ci(tmp_path):
    workflow = (ROOT/'.github/workflows/ci-perf-diagnostic.yml').read_text()
    assert 'branches: [codex/ci-perf-six-20261021-s7o7ql]' in workflow
    assert "contains(github.event.head_commit.message, '[ci-perf-six-run]')" in workflow
    assert 'github.run_attempt == 1' in workflow
    assert 'pull_request:' not in workflow and 'workflow_dispatch:' not in workflow and 'schedule:' not in workflow
    assert 'contents: read' in workflow and 'write' not in workflow
    assert 'ubuntu-24.04' in workflow and 'timeout-minutes: 60' in workflow
    assert workflow.count('actions/checkout@11d5960a326750d5838078e36cf38b85af677262') == 2
    assert workflow.count('persist-credentials: false') == 2
    assert 'ref: ${{ github.sha }}' in workflow
    assert 'ref: 0ad6e77fd0dbbc937dfc3e660bc7ae37d9ae89b1' in workflow
    assert 'path: harness' in workflow and 'path: measured-source' in workflow
    assert 'if: always()' in workflow
    assert 'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02' in workflow
    assert 'name: diagnostic-prediction-six-' in workflow
    assert 'name: ci-prediction-' not in workflow
    # Record the process boundary, including isolation and exact original selection.
    module = harness()
    import inspect
    runner = inspect.getsource(module.run)
    inside = inspect.getsource(module.inside)
    for required in ["'--network', 'none'", "'--cap-drop', 'ALL'", "'no-new-privileges'", "'docker', 'create'", "'docker', 'cp'", "'docker', 'start', '-a'", "'SOURCE_STATE=clean'"]:
        assert required in runner
    for forbidden in ['--privileged', '--volume', '--mount', '--publish', 'docker.sock']:
        assert forbidden not in runner
    assert "'--nleg', '0', '--services', 'prediction'" in inside
    assert "'-n', '6', '--dist=loadgroup'" in inside
    assert "'not pressure and not browser'" in inside
    assert "'scripts.ci_test_metrics'" in inside
    assert 'junit.xml' in inside
    # The module imports with only stdlib (as required by stock-host unittest discovery).
    run = subprocess.run([sys.executable, '-S', '-c',
        'import runpy; runpy.run_path('+repr(str(Path(__file__)))+')'], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr

    # Simulate only the external Docker/Git boundary, never execute a backend.
    # A test failure must survive artifact/cleanup failures, with one test start.
    from unittest.mock import patch
    import threading
    source = tmp_path/'measured'
    source.mkdir()
    (source/'uv.lock').write_text('fixture lock')
    (source/'Dockerfile.dev').write_text('fixture image')
    calls = []
    def external(command, **kwargs):
        calls.append(command)
        code = 7 if command[:3] == ['docker', 'start', '-a'] else 0
        if command[:2] == ['docker', 'rm'] or (command[:2] == ['docker', 'cp'] and ':/' in command[2]):
            code = 9  # Intentional artifact/cleanup failure, not the test result.
        return subprocess.CompletedProcess(command, code, stdout='', stderr='')
    def git_identity(command, **kwargs):
        return module.SOURCE_SHA+'\n' if command[-2:] == ['rev-parse', 'HEAD'] else ''
    with patch.object(module.subprocess, 'run', side_effect=external), patch.object(module.subprocess, 'check_output', side_effect=git_identity):
        assert module.run(source, tmp_path/'runner-evidence') == 7
    starts = [index for index, command in enumerate(calls) if command[:3] == ['docker', 'start', '-a']]
    inject = [index for index, command in enumerate(calls) if command[:2] == ['docker', 'cp'] and command[-1].endswith(':/tmp/ci_perf_diagnostic.py')]
    assert len(starts) == len(inject) == 1 and inject[0] < starts[0]
    assert any(command[:2] == ['docker', 'rm'] for command in calls)
    assert not any(thread.name == 'diagnostic-resource-sampler' for thread in threading.enumerate())
    record = json.loads((tmp_path/'runner-evidence/runner-result.json').read_text())
    assert record['exit_status'] == 7 and record['source_clean_after'] is True
    commands = [json.loads(line) for line in (tmp_path/'runner-evidence/commands.jsonl').read_text().splitlines()]
    assert all(row['end_ns'] >= row['start_ns'] and row['wall_seconds'] >= 0 for row in commands)

    # Losing the final source-status query also cannot replace the test exit.
    queries = 0
    def missing_final_status(command, **kwargs):
        nonlocal queries
        queries += 1
        if queries > 2:
            raise subprocess.CalledProcessError(11, command)
        return git_identity(command, **kwargs)
    with patch.object(module.subprocess, 'run', side_effect=external), patch.object(module.subprocess, 'check_output', side_effect=missing_final_status):
        assert module.run(source, tmp_path/'runner-unknown-status') == 7
    record = json.loads((tmp_path/'runner-unknown-status/runner-result.json').read_text())
    assert record['exit_status'] == 7 and record['source_clean_after'] is None
    assert record['cleanup_errors']
