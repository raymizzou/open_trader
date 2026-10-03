"""Exercise CI runner success and failure without Docker, installs, or test suites."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40


class RunnerTests(unittest.TestCase):
    def run_scope(self, scope, *, fail_make=False, nleg='1', dirty=False, fail_proof=False):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, evidence, binary = base / 'repo', base / 'evidence', base / 'bin'
            (root / 'scripts').mkdir(parents=True)
            binary.mkdir()
            (root / 'uv.lock').write_text('locked dependencies')
            shutil.copy(ROOT / 'scripts/ci_evidence.py', root / 'scripts/ci_evidence.py')
            commands = base / 'commands'
            stubs = {
                'git': '''#!/bin/sh
case "$*" in
  'rev-parse --show-toplevel') echo "$FAKE_ROOT" ;;
  'rev-parse HEAD') echo "$GITHUB_SHA" ;;
  'status --porcelain --untracked-files=all') [ "$FAKE_DIRTY" != 1 ] || echo ' M dirty' ;;
  *) exit 9 ;;
esac
''',
                'make': '''#!/bin/sh
printf 'make %s\\n' "$*" >> "$FAKE_COMMANDS"
case "$*" in
  *ci-test-files*)
    for s in gateway legacy account prediction; do echo "$s:tests/test_$s.py"; done ;;
  *) [ "$FAKE_MAKE_FAILURE" != 1 ] || exit 7 ;;
esac
''',
                'docker': '''#!/bin/sh
printf 'docker %s\\n' "$*" >> "$FAKE_COMMANDS"
case "$*" in
  'image inspect '*) echo '[{"Id":"sha256:image"}]' ;;
  *dev_dependency_manifest.py*) printf '{"source_sha":"%s","source_state":"clean"}\\n' "$GITHUB_SHA" ;;
  *ci_partition.py*) [ "$FAKE_PROOF_FAILURE" != 1 ] || exit 8; echo '{"status":"success"}' ;;
  *) exit 9 ;;
esac
''',
            }
            for name, source in stubs.items():
                path = binary / name
                path.write_text(source)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                       FAKE_ROOT=str(root), FAKE_COMMANDS=str(commands),
                       FAKE_MAKE_FAILURE=str(int(fail_make)), FAKE_DIRTY=str(int(dirty)),
                       FAKE_PROOF_FAILURE=str(int(fail_proof)), GITHUB_SHA=SHA,
                       GITHUB_REPOSITORY='raymizzou/open_trader',
                       GITHUB_WORKFLOW_REF='raymizzou/open_trader/.github/workflows/ci.yml@refs/heads/main',
                       GITHUB_EVENT_NAME='push', GITHUB_REF='refs/heads/main',
                       GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='2', RUNNER_OS='Linux', RUNNER_ARCH='X64')
            env.pop('GITHUB_STEP_SUMMARY', None)
            run = subprocess.run(['bash', str(ROOT / 'scripts/run_ci_service.sh'), scope, nleg, str(evidence)],
                                 cwd=root, env=env, capture_output=True, text=True)
            metadata = json.loads((evidence / 'evidence.json').read_text()) if (evidence / 'evidence.json').exists() else None
            return run, metadata, commands.read_text() if commands.exists() else ''

    def test_all_required_scopes_record_exact_policy_and_run(self):
        for scope in ('gateway', 'legacy', 'account', 'prediction', 'portable'):
            with self.subTest(scope=scope):
                run, evidence, commands = self.run_scope(scope)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertEqual(evidence['source_sha'], SHA)
                self.assertEqual(evidence['run_attempt'], '2')
                self.assertEqual(evidence['status'], 'success')
                self.assertEqual(evidence['workers'], 2 if scope == 'prediction' else 1)
                if scope == 'portable':
                    self.assertIn('make test-ci-portable', commands)
                    self.assertIn('ci_partition.py', commands)
                else:
                    self.assertIn(f'make test SERVICE={scope} TEST_N_LEG=1 TEST_WORKERS={evidence["workers"]}', commands)

    def test_failure_cannot_be_masked_by_successful_manifest(self):
        run, evidence, commands = self.run_scope('prediction', fail_make=True)
        self.assertEqual(run.returncode, 7)
        self.assertEqual(evidence['exit_status'], 7)
        self.assertEqual(evidence['status'], 'failure')
        self.assertIn('dev_dependency_manifest.py', commands)

    def test_failed_partition_proof_fails_portable_job(self):
        run, evidence, commands = self.run_scope('portable', fail_proof=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(evidence['status'], 'failure')

    def test_paused_or_dirty_input_fails_before_build(self):
        for change in ({'nleg': '0'}, {'dirty': True}):
            with self.subTest(change=change):
                run, evidence, commands = self.run_scope('prediction', **change)
                self.assertNotEqual(run.returncode, 0)
                self.assertIsNone(evidence)
                self.assertEqual(commands, '')


if __name__ == '__main__':
    unittest.main()
