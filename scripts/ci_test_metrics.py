"""Pytest plugin for auditable nodeids, phase durations and failure records.

The controller writes once, after serial or xdist execution. Workers never race
on artifact files. These records do not alter pytest outcomes or exit status.
"""
import json
import os
from pathlib import Path
import platform
import time
import subprocess

import pytest


def cpu_identity():
    model, source, unavailable = None, 'unavailable', 'unsupported-platform'
    try:
        if platform.system() == 'Linux':
            text = Path('/proc/cpuinfo').read_text()
            for line in text.splitlines():
                key, separator, value = line.partition(':')
                if separator and key.strip() in ('model name', 'Hardware') and value.strip():
                    model, source = value.strip(), '/proc/cpuinfo'
                    break
            unavailable = 'native-model-not-found'
        elif platform.system() == 'Darwin':
            result = subprocess.run(['/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string'],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                model, source = result.stdout.strip(), 'sysctl:machdep.cpu.brand_string'
            unavailable = 'native-model-unavailable'
    except (OSError, subprocess.TimeoutExpired):
        unavailable = 'native-read-failed'
    return dict(cpu_model=model, cpu_model_source=source,
                cpu_model_unavailable=None if model is not None else unavailable,
                architecture=platform.machine())


class Recorder:
    def __init__(self, config, output):
        self.config, self.output = config, Path(output)
        self.started = time.monotonic()
        self.selected, self.results = [], {}
        self.collection_errors = []
        self.worker_collections = []
        self.original_nodeids = {}

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, items):
        # Capture exact pytest identities before xdist adds loadgroup decoration.
        originals = {id(item): item.nodeid for item in items}
        yield
        self.original_nodeids.update({item.nodeid: originals[id(item)] for item in items})

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        identities = node.workeroutput.get('ci_original_nodeids', {})
        for decorated, original in identities.items():
            if decorated in self.original_nodeids and self.original_nodeids[decorated] != original:
                self.collection_errors.append(dict(nodeid=decorated, detail='worker nodeid identity mismatch'))
            self.original_nodeids[decorated] = original
        if error:
            self.collection_errors.append(dict(nodeid=node.gateway.id, detail=str(error)))

    def pytest_collection_finish(self, session):
        self.selected = sorted(item.nodeid for item in session.items)

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self.worker_collections.append(sorted(ids))
        self.selected = sorted(ids)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(dict(nodeid=report.nodeid, detail=str(report.longrepr)))

    def pytest_runtest_logreport(self, report):
        result = self.results.setdefault(report.nodeid, dict(nodeid=report.nodeid, outcome='passed', phases={}))
        result['phases'][report.when] = dict(duration=report.duration, outcome=report.outcome,
                                           worker=getattr(report, 'worker_id', 'serial'))
        if report.failed:
            result['outcome'] = 'failed'
        elif report.skipped and result['outcome'] != 'failed':
            result['outcome'] = 'skipped'

    def pytest_sessionfinish(self, session, exitstatus):
        if hasattr(self.config, 'workerinput'):
            self.config.workeroutput['ci_original_nodeids'] = self.original_nodeids
            return
        # Only explicit worker/item mappings remove decoration; parameter text
        # containing @ is never parsed or stripped.
        original_selected = sorted(self.original_nodeids.get(node, node) for node in self.selected)
        original_results = []
        for node in sorted(self.results):
            original_results.append({**self.results[node], 'nodeid': self.original_nodeids.get(node, node),
                                     'execution_nodeid': node})
        workers = int(os.environ.get('CI_TEST_WORKERS', '1'))
        metrics = dict(schema_version=1, source_sha=os.environ.get('CI_TEST_SOURCE_SHA', ''), workers=workers,
                       python=platform.python_version(), platform=platform.platform(), **cpu_identity(),
                       cpu_count=os.cpu_count(), selected_nodeids=original_selected,
                       results=sorted(original_results, key=lambda result: result['nodeid']),
                       collection_errors=self.collection_errors, exit_status=int(exitstatus),
                       complete=int(exitstatus) in (0, 1) and not self.collection_errors
                       and all(ids == self.selected for ids in self.worker_collections)
                       and set(self.results) == set(self.selected),
                       wall_seconds=time.monotonic()-self.started)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps(metrics, sort_keys=True, indent=2)+'\n')
        self.output.with_name('selected-nodeids.json').write_text(json.dumps(original_selected, indent=2)+'\n')


def pytest_configure(config):
    output = os.environ.get('CI_TEST_METRICS')
    if output:
        config.pluginmanager.register(Recorder(config, output), 'ci-test-recorder')
