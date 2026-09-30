"""Static contract tests for CI wiring; runtime verdict tests live in routing tests."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def test_events_security_and_exact_candidate(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('name: CI\n', workflow)
        self.assertIn('pull_request:', workflow)
        self.assertIn('push:', workflow)
        self.assertEqual(workflow.count('branches: [main]'), 2)
        self.assertNotIn('paths:', workflow)
        self.assertNotIn('pull_request_target', workflow)
        self.assertNotIn('secrets.', workflow)
        self.assertNotIn('self-hosted', workflow)
        self.assertIn('contents: read', workflow)
        self.assertIn('cancel-in-progress: true', workflow)
        for ref in re.findall(r'uses: (\S+)', workflow):
            self.assertRegex(ref, r'^[\w-]+/[\w-]+@[0-9a-f]{40}$')
        self.assertEqual(workflow.count('persist-credentials: false'), 7)
        self.assertEqual(workflow.count('ref: ${{ github.sha }}'), 7)
        self.assertEqual(workflow.count('runs-on: ubuntu-24.04'), 7)
        self.assertEqual(workflow.count('timeout-minutes:'), 7)
        self.assertIn('github.event.pull_request.base.sha', workflow)
        self.assertIn('github.event.before', workflow)

    def test_fixed_required_check_and_all_service_results(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text()
        required = workflow.split('  required:\n', 1)[1]
        self.assertIn('name: required', required)
        self.assertIn('if: always()', required)
        self.assertIn('needs: [plan, gateway, legacy, account, prediction, trend_curve]', required)
        self.assertIn('toJSON(needs)', required)
        self.assertIn('python3 scripts/ci_plan.py required', required)
        self.assertNotIn('continue-on-error', workflow)
        self.assertEqual(workflow.count('if-no-files-found: error'), 5)
        for scope in ['gateway', 'legacy', 'account', 'prediction', 'trend-curve']:
            self.assertIn(f'"{scope}"', workflow)
        self.assertIn('test_ci_*.py', workflow)

    def test_runner_uses_existing_offline_make_gate_and_records_evidence(self):
        script = (ROOT / 'scripts/run_ci_service.sh').read_text()
        self.assertIn('make test SERVICE=', script)
        self.assertIn('make test-trend-curve', script)
        self.assertIn('TEST_N_LEG="$nleg"', script)
        self.assertIn('TEST_WORKERS=2', script)
        self.assertIn('set -euo pipefail', script)
        self.assertIn('sha256sum uv.lock', script)
        self.assertIn('dev_dependency_manifest.py', script)
        self.assertIn('--network none --cap-drop ALL', script)
        self.assertIn('git status --porcelain', script)
        self.assertNotIn('candidate-acceptance', script)
        self.assertNotIn('production-smoke', script)


if __name__ == '__main__':
    unittest.main()
