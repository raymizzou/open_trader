"""Static release workflow boundaries plus explicit ordinary-CI test wiring."""
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]


class WorkflowContracts(unittest.TestCase):
    def test_trigger_permissions_and_no_publication(self):
        text=(ROOT/'.github/workflows/release.yml').read_text()
        self.assertIn('workflow_dispatch:',text)
        self.assertIn('tags: [\'v*\']',text)
        self.assertIn('cancel-in-progress: false',text)
        self.assertEqual(text.count('contents: write'),1)
        self.assertIn('needs: build',text)
        self.assertIn('actions: read',text)
        self.assertIn('persist-credentials: false',text)
        self.assertNotIn('secrets.PAT',text)
        self.assertNotIn('release: published',text)
        self.assertNotRegex(text,r'git tag|gh release create|--clobber|draft: false')
        for action in re.findall(r'uses: (\S+)',text):
            self.assertRegex(action,r'^[A-Za-z0-9_./-]+@[0-9a-f]{40}$')
        self.assertIn('SOURCE_TAG:',text)
        for line in text.splitlines():
            if '${{' in line:self.assertNotIn('run:',line)

    def test_release_regressions_always_run_in_ci_plan(self):
        text=(ROOT/'.github/workflows/ci.yml').read_text()
        self.assertIn("python3 -m unittest discover -s tests -p 'test_release_*.py' -v",text)

if __name__=='__main__':unittest.main()
