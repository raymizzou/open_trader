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

    def test_write_job_uses_trusted_workflow_code_and_authenticated_build_manifest(self):
        text = (ROOT/'.github/workflows/release.yml').read_text()
        build, draft = text.split('  draft:', 1)
        self.assertIn('manifest_sha256:', build)
        self.assertIn('release-manifest.json', build)
        self.assertIn('GITHUB_OUTPUT', build)
        self.assertIn('ref: ${{ github.workflow_sha }}', draft)
        self.assertIn('EXPECTED_MANIFEST_SHA256: ${{ needs.build.outputs.manifest_sha256 }}', draft)
        self.assertIn('--expected-manifest-sha256 "$EXPECTED_MANIFEST_SHA256"', draft)
        self.assertIn('SOURCE_SHA: ${{ needs.build.outputs.source_sha }}', draft)
        self.assertIn('--expected-tag "$SOURCE_TAG"', draft)
        self.assertIn('--expected-sha "$SOURCE_SHA"', draft)
        self.assertNotIn('ref: ${{ needs.build.outputs.source_sha }}', draft)

    def test_release_regressions_always_run_in_ci_plan(self):
        text=(ROOT/'.github/workflows/ci.yml').read_text()
        self.assertIn("python3 -m unittest discover -s tests -p 'test_release_*.py' -v",text)

if __name__=='__main__':unittest.main()
