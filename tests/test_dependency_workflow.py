"""Static guardrails for the deliberately ticket-scoped Actions check."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DependencyWorkflowTests(unittest.TestCase):
    def test_workflow_is_ticket_scoped_and_least_privilege(self):
        workflow = (ROOT / '.github/workflows/issue-212-dependencies.yml').read_text()
        self.assertIn("github.head_ref == 'fix/212-locked-dev'", workflow)
        self.assertIn('runs-on: ubuntu-24.04', workflow)
        self.assertIn('contents: read', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('github.event.pull_request.head.sha', workflow)
        self.assertNotIn('pull_request_target:', workflow)
        self.assertNotIn('secrets.', workflow)
        for ref in re.findall(r'uses: (\S+)', workflow):
            self.assertRegex(ref, r'^[\w-]+/[\w-]+@[0-9a-f]{40}$')
        self.assertIn('timeout-minutes: 30', workflow)

    def test_verifier_builds_clean_twice_and_records_exact_source(self):
        script = (ROOT / 'scripts/verify_dev_dependencies.sh').read_text()
        self.assertIn('for n in 1 2', script)
        self.assertIn('build --no-cache', script)
        self.assertIn('git archive HEAD', script)
        self.assertIn('git status --porcelain', script)
        self.assertIn('cmp "$evidence/manifest-1.json" "$evidence/manifest-2.json"', script)
        self.assertIn('docker inspect', script)
        self.assertIn('NetworkMode', script)
        self.assertIn('Mounts', script)
        self.assertIn('--network none --cap-drop ALL', script)
        self.assertIn('--security-opt no-new-privileges', script)
        self.assertIn('make test TEST=', script)
        self.assertNotIn('candidate-acceptance', script)


if __name__ == '__main__':
    unittest.main()
