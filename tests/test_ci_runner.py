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
    def run_scope(self, scope, *, fail_make=False, nleg='0', dirty=False, fail_proof=False,
                  fail_copy=False, missing_metrics=False):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, evidence, binary = base / 'repo', base / 'evidence', base / 'bin'
            shutil.copytree(ROOT / 'scripts', root / 'scripts')
            binary.mkdir()
            (root/'tests').mkdir()
            policy=json.loads((root/'scripts/ci_nleg_retired.json').read_text())
            for path in [item['path'] for item in policy['retired']] + [
                    'tests/test_frontend_gateway.py', 'tests/test_dashboard_web.py',
                    'tests/test_account_api.py', 'tests/test_prediction_runtime.py']:
                (root/path).write_text('def test_ok(): pass\n')
            (root / 'uv.lock').write_text('locked dependencies')
            commands = base / 'commands'
            stubs = {
                'git': '#!/bin/sh\ncase "$*" in\n  \'rev-parse --show-toplevel\') echo "$FAKE_ROOT" ;;\n  \'rev-parse HEAD\') echo "$GITHUB_SHA" ;;\n  \'status --porcelain --untracked-files=all\') [ "$FAKE_DIRTY" != 1 ] || echo \' M dirty\' ;;\n  *) exit 9 ;;\nesac\n',
                'make': '#!/bin/sh\nprintf \'make %s\\n\' "$*" >> "$FAKE_COMMANDS"\n[ "$FAKE_MAKE_FAILURE" != 1 ] || exit 7\n',
                'docker': '#!' + os.sys.executable + '\n' + 'import json, os, sys\nimport xml.etree.ElementTree as ET\nfrom pathlib import Path\nsys.path.insert(0,str(Path(os.environ[\'FAKE_ROOT\'])/\'scripts\'))\nimport ci_evidence, ci_partition\nargs=sys.argv[1:]\nwith open(os.environ[\'FAKE_COMMANDS\'],\'a\') as stream: stream.write(\'docker \'+\' \'.join(args)+\'\\n\')\nif args[:2]==[\'image\',\'inspect\']: print(\'[{"Id":"sha256:image"}]\')\nelif \'scripts/dev_dependency_manifest.py\' in args: print(json.dumps(dict(source_sha=os.environ[\'GITHUB_SHA\'],source_state=\'clean\')))\nelif \'scripts/ci_partition.py\' in args:\n    if os.environ[\'FAKE_PROOF_FAILURE\']==\'1\': sys.exit(8)\n    parts={scope:[path+\'::test_ok\' for path in roots] for scope,roots in ci_evidence.service_partitions(Path(os.environ[\'FAKE_ROOT\'])).items()}\n    retired=[path+\'::test_ok\' for path in ci_evidence.retirement_policy(Path(os.environ[\'FAKE_ROOT\']))[\'retired_files\']]\n    print(json.dumps(ci_partition.verify_partition(sum(parts.values(),[])+retired,parts,retired,Path(os.environ[\'FAKE_ROOT\']))))\nelif args[0]==\'cp\':\n    if os.environ[\'FAKE_COPY_FAILURE\']==\'1\': sys.exit(6)\n    scope=os.environ[\'FAKE_SCOPE\']; workers=2 if scope==\'prediction\' else 1\n    nodes=[path+\'::test_ok\' for path in ci_evidence.expected_selection(scope,Path(os.environ[\'FAKE_ROOT\']))[\'roots\']]\n    destination=Path(args[-1]); destination.mkdir(exist_ok=True)\n    metrics=dict(schema_version=1,source_sha=os.environ[\'GITHUB_SHA\'],workers=workers,selected_nodeids=nodes,\n                 python=\'3.12.14\',platform=\'Linux/x86_64\',architecture=\'x86_64\',cpu_model=\'Example CPU\',\n                 cpu_model_source=\'/proc/cpuinfo\',cpu_model_unavailable=None,cpu_count=2,\n                 results=[dict(nodeid=node,execution_nodeid=node,outcome=\'passed\',phases={\n                    phase:dict(duration=0.1,outcome=\'passed\',worker=\'gw0\' if workers==2 else \'serial\')\n                    for phase in (\'setup\',\'call\',\'teardown\')}) for node in nodes],collection_errors=[],\n                 exit_status=0,complete=True,wall_seconds=1.0)\n    (destination/\'selected-nodeids.json\').write_text(json.dumps(nodes))\n    xml=ET.Element(\'testsuites\')\n    suite=ET.SubElement(xml,\'testsuite\',tests=str(len(nodes)),errors=\'0\',failures=\'0\',skipped=\'0\')\n    for node in nodes:\n        file,name=node.split(\'::\',1)\n        ET.SubElement(suite,\'testcase\',classname=file[:-3].replace(\'/\',\'.\'),name=name,time=\'0.3\')\n    (destination/\'junit.xml\').write_bytes(ET.tostring(xml))\n    if os.environ[\'FAKE_MISSING_METRICS\']!=\'1\': (destination/\'metrics.json\').write_text(json.dumps(metrics))\nelif args[0]==\'rm\': pass\nelse: sys.exit(9)\n',
            }
            for name, source in stubs.items():
                path = binary / name; path.write_text(source); path.chmod(0o755)
            env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                       FAKE_ROOT=str(root), FAKE_COMMANDS=str(commands), FAKE_SCOPE=scope,
                       FAKE_MAKE_FAILURE=str(int(fail_make)), FAKE_DIRTY=str(int(dirty)),
                       FAKE_COPY_FAILURE=str(int(fail_copy)), FAKE_MISSING_METRICS=str(int(missing_metrics)),
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

    def test_active_runner_preserves_selection_artifacts_and_failure(self):
        run, evidence, commands = self.run_scope('prediction')
        self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
        self.assertEqual(evidence['test_n_leg'], '0')
        self.assertEqual(evidence['policy']['policy'], 'permanent-nleg-retirement-v1')
        self.assertTrue(evidence['selected_nodeids'])
        for name in ('metrics_sha256', 'selected_nodeids_sha256', 'junit_sha256'):
            self.assertIsNotNone(evidence['environment'][name])
        self.assertIn('CI_TEST_ARTIFACTS=1', commands)
        self.assertIn('docker cp', commands)
        for change, expected in [({'fail_make':True},7), ({'fail_copy':True},6),
                                 ({'missing_metrics':True},1), ({'fail_make':True,'fail_copy':True},7)]:
            run, evidence, commands = self.run_scope('prediction', **change)
            self.assertEqual(run.returncode, expected, run.stdout+run.stderr)
            if evidence is not None: self.assertEqual(evidence['status'],'failure')
        run, evidence, commands = self.run_scope('portable', fail_proof=True)
        self.assertNotEqual(run.returncode,0)
        run, evidence, commands = self.run_scope('prediction',nleg='1')
        self.assertNotEqual(run.returncode,0)
        self.assertEqual(commands,'')

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
                    self.assertIn(f'make test SERVICE={scope} TEST_N_LEG=0 TEST_WORKERS={evidence["workers"]}', commands)

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
        for change in ({'nleg': '1'}, {'dirty': True}):
            with self.subTest(change=change):
                run, evidence, commands = self.run_scope('prediction', **change)
                self.assertNotEqual(run.returncode, 0)
                self.assertIsNone(evidence)
                self.assertEqual(commands, '')


if __name__ == '__main__':
    unittest.main()
