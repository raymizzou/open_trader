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
                    'tests/test_account_api.py', 'tests/test_prediction_runtime.py',
                    'tests/test_lp_d2_service_lifecycle.py'] + sorted({node.split('::', 1)[0] for node in
                        json.loads((root/'scripts/ci_optional_tests.json').read_text())['optional']}):
                (root/path).write_text('def test_ok(): pass\n')
            (root / 'uv.lock').write_text('locked dependencies')
            commands = base / 'commands'
            stubs = {
                'git': '#!/bin/sh\ncase "$*" in\n  \'rev-parse --show-toplevel\') echo "$FAKE_ROOT" ;;\n  \'rev-parse HEAD\') echo "$GITHUB_SHA" ;;\n  \'status --porcelain --untracked-files=all\') [ "$FAKE_DIRTY" != 1 ] || echo \' M dirty\' ;;\n  *) exit 9 ;;\nesac\n',
                'make': '#!/bin/sh\nprintf \'make %s\\n\' "$*" >> "$FAKE_COMMANDS"\n[ "$FAKE_MAKE_FAILURE" != 1 ] || { echo synthetic test failure; exit 7; }\n',
                'docker': '#!' + os.sys.executable + '\n' + 'import json, os, sys\nimport xml.etree.ElementTree as ET\nfrom pathlib import Path\nsys.path.insert(0,str(Path(os.environ[\'FAKE_ROOT\'])/\'scripts\'))\nimport ci_evidence, ci_partition\nargs=sys.argv[1:]\nwith open(os.environ[\'FAKE_COMMANDS\'],\'a\') as stream: stream.write(\'docker \'+\' \'.join(args)+\'\\n\')\nif args[:2]==[\'image\',\'inspect\']: print(\'[{"Id":"sha256:image"}]\')\nelif \'scripts/dev_dependency_manifest.py\' in args: print(json.dumps(dict(source_sha=os.environ[\'GITHUB_SHA\'],source_state=\'clean\')))\nelif \'scripts/ci_partition.py\' in args:\n    if os.environ[\'FAKE_PROOF_FAILURE\']==\'1\': sys.exit(8)\n    parts={scope:[path+\'::test_ok\' for path in roots] for scope,roots in ci_evidence.service_partitions(Path(os.environ[\'FAKE_ROOT\'])).items()}\n    retired=[path+\'::test_ok\' for path in ci_evidence.retirement_policy(Path(os.environ[\'FAKE_ROOT\']))[\'retired_files\']]\n    optional=ci_evidence.optional_policy(Path(os.environ[\'FAKE_ROOT\']))[\'optional_nodeids\']\n    print(json.dumps(ci_partition.verify_partition(sum(parts.values(),[])+retired+optional,parts,retired,Path(os.environ[\'FAKE_ROOT\']),optional)))\nelif args[0]==\'cp\':\n    if os.environ[\'FAKE_COPY_FAILURE\']==\'1\': sys.exit(6)\n    scope=os.environ[\'FAKE_SCOPE\']; workers=2 if scope==\'prediction\' else 1\n    selection=ci_evidence.expected_selection(scope,Path(os.environ[\'FAKE_ROOT\']))\n    nodes=selection.get(\'nodeids\', [path+\'::test_ok\' for path in selection[\'roots\']])\n    destination=Path(args[-1]); destination.mkdir(exist_ok=True)\n    metrics=dict(schema_version=1,source_sha=os.environ[\'GITHUB_SHA\'],workers=workers,selected_nodeids=nodes,\n                 python=\'3.12.14\',platform=\'Linux/x86_64\',architecture=\'x86_64\',cpu_model=\'Example CPU\',\n                 cpu_model_source=\'/proc/cpuinfo\',cpu_model_unavailable=None,cpu_count=2,\n                 results=[dict(nodeid=node,execution_nodeid=node,outcome=\'passed\',phases={\n                    phase:dict(duration=0.1,outcome=\'passed\',worker=\'gw0\' if workers==2 else \'serial\')\n                    for phase in (\'setup\',\'call\',\'teardown\')}) for node in nodes],collection_errors=[],\n                 exit_status=0,complete=True,wall_seconds=1.0)\n    if scope==\'release-advisory\' and os.environ[\'FAKE_MAKE_FAILURE\']==\'1\':\n        metrics[\'exit_status\']=7; metrics[\'results\'][0][\'outcome\']=\'failed\'; metrics[\'results\'][0][\'phases\'][\'call\'][\'outcome\']=\'failed\'\n    (destination/\'selected-nodeids.json\').write_text(json.dumps(nodes))\n    xml=ET.Element(\'testsuites\')\n    suite=ET.SubElement(xml,\'testsuite\',tests=str(len(nodes)),errors=\'0\',failures=\'1\' if scope==\'release-advisory\' and os.environ[\'FAKE_MAKE_FAILURE\']==\'1\' else \'0\',skipped=\'0\')\n    for node in nodes:\n        file,name=node.split(\'::\',1)\n        case=ET.SubElement(suite,\'testcase\',classname=file[:-3].replace(\'/\',\'.\'),name=name,time=\'0.3\')\n        if scope==\'release-advisory\' and os.environ[\'FAKE_MAKE_FAILURE\']==\'1\': ET.SubElement(case,\'failure\',message=\'synthetic test failure\')\n    (destination/\'junit.xml\').write_bytes(ET.tostring(xml))\n    if os.environ[\'FAKE_MISSING_METRICS\']!=\'1\': (destination/\'metrics.json\').write_text(json.dumps(metrics))\nelif args[0]==\'rm\': pass\nelse: sys.exit(9)\n',
            }
            stubs['docker']=stubs['docker'].replace("scope=='prediction'", "scope.startswith('prediction')")
            stubs['docker']=stubs['docker'].replace("scope=='release-advisory'", "scope.startswith('release-advisory')")
            stubs['docker']=stubs['docker'].replace(
                "    destination=Path(args[-1]); destination.mkdir(exist_ok=True)",
                "    if 'shard' in selection:\n"
                "        base_scope='prediction' if scope.startswith('prediction-') else 'release-advisory'\n"
                "        if base_scope=='prediction': nodes=[path+'::test_case_'+str(i) for path in selection['roots'] for i in range(24)]\n"
                "        nodes=ci_partition.shard_nodeids(base_scope,nodes,selection['shard']['index'])\n"
                "    destination=Path(args[-1]); destination.mkdir(exist_ok=True)")
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
            run.ci_files = {path.name:path.read_text() for path in evidence.glob('*') if path.is_file()}
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

    def test_shard_runner_preserves_workers_selection_and_advisory_failure(self):
        required=[]
        for index in range(1,4):
            scope=f'prediction-{index}'
            run,evidence,commands=self.run_scope(scope)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertEqual(evidence['scope'],scope)
            self.assertEqual(evidence['workers'],2)
            self.assertEqual(evidence['selection']['shard'],dict(index=index,count=3))
            self.assertIn(f'make test SERVICE=prediction TEST_N_LEG=0 TEST_WORKERS=2 CI_TEST_SCOPE={scope}',commands)
            selected=set(evidence['selected_nodeids'])
            self.assertTrue(selected)
            self.assertFalse(any(selected & previous for previous in required))
            required.append(selected)
        from scripts.ci_partition import shard_nodeids
        all_nodes=sorted(set.union(*required))
        for index,nodes in enumerate(required,1):
            self.assertEqual(sorted(nodes),shard_nodeids('prediction',all_nodes,index))
        advisory=[]
        for index in range(1,5):
            scope=f'release-advisory-{index}'
            run,evidence,commands=self.run_scope(scope,fail_make=True)
            self.assertEqual(run.returncode,7,run.stdout+run.stderr)
            self.assertEqual(evidence['exit_status'],7)
            self.assertEqual(evidence['status'],'failure')
            self.assertEqual(evidence['workers'],1)
            self.assertIn(f'CI_TEST_SCOPE={scope}',commands)
            self.assertIn('synthetic test failure',run.ci_files['test.log'])
            self.assertTrue(json.loads(run.ci_files['metrics.json'])['results'])
            advisory.append(set(evidence['selected_nodeids']))
        self.assertEqual(sum(map(len,advisory)),len(set.union(*advisory)))
        self.assertEqual(sum(any('test_lp410_' in node for node in nodes) for nodes in advisory),2)

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
