"""Workflow wiring and executable Trend V3 verdict checks."""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_events_security_and_exact_candidate(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('name: CI\n', workflow)
        events = workflow.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertEqual(events, '  pull_request:\n    branches: [main]\n  push:\n    branches: [main]\n  workflow_dispatch:\n')
        self.assertNotIn('concurrency:', workflow)
        self.assertNotIn('inputs:', workflow)
        guard = "github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main'"
        self.assertIn('  trend_v3:\n    if: ' + guard, workflow)
        required = workflow.split('  required:\n', 1)[1]
        self.assertIn('if: always() && (' + guard + ')', required)
        self.assertNotIn('paths:', workflow)
        self.assertNotIn('pull_request_target', workflow)
        self.assertNotIn('secrets.', workflow)
        self.assertNotIn('self-hosted', workflow)
        self.assertIn('contents: read', workflow)
        self.assertNotIn('cancel-in-progress:', workflow)
        for ref in re.findall(r'uses: (\S+)', workflow):
            self.assertRegex(ref, r'^[\w-]+/[\w-]+@[0-9a-f]{40}$')
        self.assertEqual(workflow.count('uses: actions/checkout@'), 1)
        self.assertIn(
            '        with:\n          ref: ${{ github.sha }}\n          persist-credentials: false',
            workflow,
        )
        self.assertEqual(workflow.count('runs-on: ubuntu-24.04'), 2)
        self.assertEqual(workflow.count('timeout-minutes:'), 2)
        self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', workflow)

    def test_fixed_required_check_and_all_service_results(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        jobs = workflow.split('\njobs:\n', 1)[1]
        self.assertEqual(re.findall(r'^  (\w+):$', jobs, re.M), ['trend_v3', 'required'])
        trend, required = jobs.split('  required:\n', 1)
        self.assertIn('timeout-minutes: 3', trend)
        self.assertIn('working-directory: services/trend_v3', trend)
        for command in (
            "python3 -m venv .venv",
            ".venv/bin/python -m pip install '.[dev]'",
            ".venv/bin/python -m ruff check src tests",
            ".venv/bin/python -m pytest tests",
        ):
            self.assertIn(command, trend)
        self.assertIn('name: required', required)
        self.assertIn('if: always()', required)
        self.assertIn('needs: [trend_v3]', required)
        self.assertIn('TREND_V3_RESULT: ${{ needs.trend_v3.result }}', required)
        self.assertNotIn('continue-on-error', workflow)
        (verdict,) = re.findall(r'^        run: (.+)$', required, re.M)
        for result in ('success', 'failure', 'cancelled', 'skipped', '', 'unknown'):
            with self.subTest(result=result):
                env = {'TREND_V3_RESULT': result} if result else {}
                completed = subprocess.run(['bash', '-e', '-c', verdict], env=env, check=False)
                self.assertEqual(completed.returncode, 0 if result == 'success' else 1)

    def test_runner_uses_existing_offline_make_gate_and_records_evidence(self):
        script = (ROOT / 'scripts/run_ci_service.sh').read_text()
        self.assertIn('make test SERVICE=', script)
        self.assertIn('make test-trend-curve', script)
        self.assertIn('make test-ci-portable', script)
        self.assertIn('scripts/ci_partition.py', script)
        self.assertIn('scripts/ci_evidence.py', script)
        self.assertIn('TEST_N_LEG="$nleg"', script)
        self.assertIn('workers=1', script)
        self.assertIn('[[ "$service" != prediction ]] || workers=2', script)
        self.assertIn('TEST_WORKERS="$workers"', script)
        self.assertIn('set -euo pipefail', script)
        self.assertIn('sha256sum uv.lock', script)
        self.assertIn('dev_dependency_manifest.py', script)
        self.assertIn('--network none --cap-drop ALL', script)
        self.assertIn('git status --porcelain', script)
        self.assertNotIn('candidate-acceptance', script)
        self.assertNotIn('production-smoke', script)
        self.assertIn('case "$nleg" in 0)', script)
        self.assertIn('CI_TEST_ARTIFACTS=1', script)
        self.assertIn('docker cp', script)
        self.assertIn('failed $?', script)
        self.assertNotIn('--mount', script)
        self.assertNotIn('--volume', script)

    def test_serial_and_shared_session_contracts_are_preserved(self):
        makefile = (ROOT / 'Makefile').read_text()
        script = (ROOT / 'scripts/run_ci_service.sh').read_text()
        self.assertIn('workers=1', script)
        self.assertIn('[[ "$service" != prediction ]] || workers=2', script)
        self.assertIn('--dist=loadgroup', makefile)
        self.assertIn('scripts.ci_test_metrics', makefile)
        self.assertIn('--junitxml=/tmp/open-trader-ci-evidence/junit.xml', makefile)
        self.assertIn('CI_TEST_SOURCE_SHA=$(SOURCE_SHA)', makefile)
        self.assertIn('scripts/ci_nleg_retired.json', makefile)
        self.assertNotIn('N_LEG_TESTS := $(wildcard', makefile)
        portable = makefile.split('test-ci-portable:\n', 1)[1].split('\n\n', 1)[0]
        self.assertIn('acceptance/test_prediction_arbitrage_scenarios.py -k "not LIVE"', portable)
        self.assertNotIn(' -n ', portable)
        for name in ('client', 'cloud'):
            source = (ROOT / f'tests/test_prediction_{name}.py').read_text()
            self.assertIn('pytest.mark.xdist_group("prediction_cloud_ports")', source)
        benchmark = (ROOT / 'tests/test_prediction_solver_benchmark.py').read_text()
        self.assertIn('pytest.mark.xdist_group("solver_benchmark")', benchmark)


if __name__ == '__main__':
    unittest.main()
