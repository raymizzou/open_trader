"""Exact one-node advisory policy at CI's public selection and evidence seams.

Keep module import safe for the stock-host unittest discovery job.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

check = unittest.TestCase()

ROOT = Path(__file__).resolve().parents[1]
ROLLBACK = 'tests/test_lp_d2_service_lifecycle.py::test_supported_rollback_uses_current_api_without_disposable_record_audit'


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def policy_root(root):
    (root/'scripts').mkdir()
    (root/'tests').mkdir()
    shutil.copy(ROOT/'scripts/ci_optional_tests.json', root/'scripts/ci_optional_tests.json')
    shutil.copy(ROOT/'scripts/ci_nleg_retired.json', root/'scripts/ci_nleg_retired.json')
    for item in json.loads((root/'scripts/ci_nleg_retired.json').read_text())['retired']:
        (root/item['path']).write_text('def test_retired(): pass\n')
    for name in ('frontend_gateway', 'account_api', 'dashboard_web'):
        (root/('tests/test_'+name+'.py')).write_text('def test_required(): pass\n')
    (root/'tests/test_lp_d2_service_lifecycle.py').write_text(
        'def test_supported_rollback_uses_current_api_without_disposable_record_audit(): pass\n'
        'def test_supported_rollback_neighbor(): pass\n'
        'def test_new(): pass\n'
        'def test_supported_rollback_uses_current_api_without_disposable_record_audit_neighbor(): pass\n')


class OptionalPolicyTests:
    __test__ = True

    def test_only_declared_rollback_is_optional(self):
        ci = load('ci_evidence')
        selection = ci.expected_selection('prediction', ROOT)
        check.assertEqual(selection['deselect'], [ROLLBACK])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy_root(root)
            partition = load('ci_partition')
            full = partition.collect(root, ['tests/test_lp_d2_service_lifecycle.py'], root/'all.json')
            result = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q',
                '-p', 'scripts.ci_partition', '--ci-required', 'tests/test_lp_d2_service_lifecycle.py'],
                cwd=root, env=dict(os.environ, PYTHONPATH=str(ROOT),
                CI_COLLECTION_OUTPUT=str(root/'required.json')), capture_output=True, text=True)
            check.assertEqual(result.returncode, 0, result.stdout+result.stderr)
            check.assertEqual(sorted(json.loads((root/'required.json').read_text())), [
                'tests/test_lp_d2_service_lifecycle.py::test_new',
                'tests/test_lp_d2_service_lifecycle.py::test_supported_rollback_neighbor',
                ROLLBACK+'_neighbor'])
            check.assertEqual(set(full)-set(json.loads((root/'required.json').read_text())), {ROLLBACK})

    def test_invalid_missing_or_expanded_optional_declarations_fail(self):
        ci = load('ci_evidence')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy_root(root)
            path = root/'scripts/ci_optional_tests.json'
            original = json.loads(path.read_text())
            for nodes in ([], [ROLLBACK, ROLLBACK], [ROLLBACK+'*'],
                          ['tests/test_lp_d2_service_lifecycle.py::test_new'],
                          [ROLLBACK, 'tests/test_lp_d2_service_lifecycle.py::test_new']):
                with check.subTest(nodes=nodes):
                    path.write_text(json.dumps({**original, 'optional':nodes}))
                    check.assertRaises(ValueError, ci.expected_selection, 'prediction', root)
            for change in ({'policy':'unreviewed'}, {'schema_version':True}, {'unexpected':[]}):
                path.write_text(json.dumps({**original, **change}))
                check.assertRaises(ValueError, ci.expected_selection, 'prediction', root)
            path.write_text(json.dumps(original))
            (root/'tests/test_lp_d2_service_lifecycle.py').unlink()
            check.assertRaises(ValueError, ci.expected_selection, 'prediction', root)
            path.unlink()
            check.assertRaises((ValueError, FileNotFoundError), ci.expected_selection, 'prediction', root)

    def test_required_optional_retired_form_disjoint_complete_partition(self):
        partition = load('ci_partition')
        ci = load('ci_evidence')
        parts = {'gateway':['tests/test_frontend_gateway.py::test_required'],
                 'legacy':['tests/test_dashboard_web.py::test_required'],
                 'account':['tests/test_account_api.py::test_required'],
                 'prediction':['tests/test_lp_d2_service_lifecycle.py::test_new',
                     'tests/test_lp_d2_service_lifecycle.py::test_supported_rollback_neighbor']}
        retired = ['tests/test_prediction_solver.py::test_retired']
        complete = sum(parts.values(), [])+retired+[ROLLBACK]
        # This public verifier must refuse to count optional declaration as execution.
        with check.assertRaises(ValueError):
            partition.verify_partition(complete, {**parts, 'prediction':parts['prediction']+[ROLLBACK]}, retired, ROOT)
        proof = partition.verify_partition(complete, parts, retired, ROOT, optional=[ROLLBACK])
        check.assertEqual(proof['optional'], [ROLLBACK])
        check.assertEqual(proof['optional_count'], 1)
        check.assertEqual(proof['optional_status'], 'not-executed-advisory')
        check.assertEqual(proof['executed_count'], 5)
        check.assertEqual(proof['complete_count'], 7)
        check.assertEqual(proof['optional_policy'], ci.optional_policy(ROOT))
        check.assertEqual(ci.validate_partition(ROOT, proof), proof)
        for universe, executed, omitted in [
            (complete, parts, []), (complete, parts, [ROLLBACK, ROLLBACK]),
            (complete, {**parts, 'prediction':parts['prediction']+[ROLLBACK]}, [ROLLBACK]),
            (complete, parts, [ROLLBACK+'*']), (complete[:-1], parts, [ROLLBACK]),
            (complete+['tests/test_future.py::test_new'], parts, [ROLLBACK]),
        ]:
            with check.subTest(universe=universe, executed=executed, optional=omitted):
                check.assertRaises(ValueError, partition.verify_partition, universe, executed, retired, ROOT, optional=omitted)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy_root(root)
            full = partition.collect(root, ['tests/test_lp_d2_service_lifecycle.py'], root/'full.json')
            required = partition.collect(root, ['tests/test_lp_d2_service_lifecycle.py'], root/'required.json', required=True)
            advisory = partition.collect(root, [ROLLBACK], root/'optional.json')
            check.assertEqual(advisory, [ROLLBACK])
            check.assertEqual(set(full), set(required)|set(advisory))
            check.assertFalse(set(required)&set(advisory))


class OptionalRunnerTests:
    __test__ = True

    def test_optional_failure_preserves_exit_status_and_evidence(self):
        spec = importlib.util.spec_from_file_location('optional_runner_fixture', ROOT/'tests/test_ci_runner.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        runner = fixture.RunnerTests()
        for change in ({'fail_make':True}, {'fail_make':True, 'fail_copy':True}):
            run, evidence, commands = runner.run_scope('rollback-advisory', **change)
            check.assertEqual(run.returncode, 7, run.stdout+run.stderr)
            check.assertIn('synthetic test failure', run.stdout)
            check.assertEqual(evidence['status'], 'failure')
            check.assertEqual(evidence['exit_status'], 7)
            check.assertEqual(evidence['scope'], 'rollback-advisory')
            check.assertEqual(evidence['test_tier'], 'optional')
            check.assertEqual(evidence['workers'], 1)
            check.assertEqual(evidence['source_sha'], 'a'*40)
            check.assertEqual(evidence['run_attempt'], '2')
            check.assertIn('make test-ci-rollback-advisory', commands)
            check.assertNotIn(' -n ', commands)
            if not change.get('fail_copy'):
                check.assertEqual(evidence['selected_nodeids'], [ROLLBACK])
                metrics = json.loads(run.ci_files['metrics.json'])
                check.assertEqual(metrics['exit_status'], 7)
                check.assertEqual(metrics['results'][0]['phases']['call']['outcome'], 'failed')
                check.assertIn('synthetic test failure', run.ci_files['junit.xml'])
                for key in ('metrics_sha256', 'selected_nodeids_sha256', 'junit_sha256'):
                    check.assertIsNotNone(evidence['environment'][key])


class OptionalWorkflowTests:
    __test__ = True

    def test_advisory_runs_separately_without_required_dependency(self):
        import re
        path = ROOT/'.github/workflows/ci-rollback-advisory.yml'
        check.assertTrue(path.is_file(), 'missing independent Rollback advisory workflow')
        text = path.read_text()
        check.assertTrue(text.startswith('name: Rollback advisory\n'))
        events = text.split('on:\n', 1)[1].split('\npermissions:', 1)[0]
        check.assertEqual(events.strip(), 'push:\n    branches: [main]\n  workflow_dispatch:')
        check.assertIn('permissions:\n  contents: read', text)
        check.assertEqual(re.findall(r'^  ([\w-]+):$', text.split('jobs:\n',1)[1], re.M), ['rollback-advisory'])
        check.assertIn('runs-on: ubuntu-24.04', text)
        check.assertIn('timeout-minutes: 60', text)
        for forbidden in ('needs:', 'continue-on-error', 'secrets.', 'workflow_run', 'pull_request', 'schedule:'):
            check.assertNotIn(forbidden, text)
        check.assertIn('ref: ${{ github.sha }}', text)
        check.assertIn('persist-credentials: false', text)
        check.assertIn('scripts/run_ci_service.sh "rollback-advisory" "0"', text)
        check.assertIn('if: always()', text)
        check.assertIn('retention-days: 3', text)
        check.assertIn('name: ci-rollback-advisory-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}', text)
        required = (ROOT/'.github/workflows/ci.yml').read_text().split('  required:\n',1)[1]
        check.assertIn('needs: [plan, gateway, legacy, account, prediction, portable, trend_curve]', required)
        check.assertNotIn('rollback', required)
        for action in re.findall(r'uses: (\S+)', text):
            check.assertRegex(action, r'^[\w-]+/[\w-]+@[0-9a-f]{40}$')


class OptionalPreflightTests:
    __test__ = True

    def fixture(self, root):
        spec = importlib.util.spec_from_file_location('optional_preflight_fixture', ROOT/'tests/test_deployment_preflight.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fixture_root, api = module.evidence.__wrapped__(root)
        return module, fixture_root, api

    def test_required_success_accepts_explicit_optional_policy_without_optional_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            module, root, api = self.fixture(Path(tmp))
            # Advisory evidence is neither requested nor needed, even if an unrelated
            # advisory archive is listed. Required archives cannot claim it passed.
            api.artifacts.append({**api.artifacts[3], 'id':99,
                'name':'ci-rollback-advisory-'+module.SHA+'-22-2'})
            report = module.preflight.verify_ci(api, module.SHA, root, module.NOW)
            check.assertEqual(len(report['artifacts']), 5)
            check.assertFalse(any('advisory' in call or call=='download:99' for call in api.calls))
            files = module.preflight.read_archive(api.downloads[5])
            proof = json.loads(files['partition.json'])
            check.assertEqual(proof['optional'], [ROLLBACK])
            check.assertEqual(proof['optional_status'], 'not-executed-advisory')
            check.assertFalse(any(ROLLBACK in nodes for nodes in proof['partitions'].values()))

    def test_optional_policy_cannot_hide_other_missing_or_failed_tests(self):
        import hashlib
        mutations = ('optional_as_required', 'optional_policy_hash', 'manifest_bytes',
                     'missing_required', 'failed_required', 'wrong_sha', 'expanded_optional')
        for mutation in mutations:
            with tempfile.TemporaryDirectory() as tmp:
                module, root, api = self.fixture(Path(tmp))
                if mutation == 'optional_as_required':
                    module.mutate_metadata(api, lambda metadata: metadata.update(test_tier='optional'))
                elif mutation == 'optional_policy_hash':
                    module.mutate_metadata(api, lambda metadata: metadata['optional_policy'].update(manifest_sha256='0'*64))
                elif mutation == 'manifest_bytes':
                    path=root/'scripts/ci_optional_tests.json'
                    path.write_text(path.read_text()+'\n')
                elif mutation == 'failed_required':
                    api.jobs[1]['conclusion']='failure'
                elif mutation == 'wrong_sha':
                    module.mutate_metadata(api, lambda metadata: metadata.update(source_sha='b'*40))
                else:
                    index = 4 if mutation == 'expanded_optional' else 3
                    artifact = api.artifacts[index]
                    files = module.preflight.read_archive(api.downloads[artifact['id']])
                    metadata = json.loads(files['evidence.json'])
                    if mutation == 'expanded_optional':
                        proof = json.loads(files['partition.json'])
                        proof['optional'].append(proof['partitions']['prediction'][0])
                        files['partition.json']=json.dumps(proof).encode()
                        metadata['environment']['partition_sha256']=hashlib.sha256(files['partition.json']).hexdigest()
                    else:
                        nodes=json.loads(files['selected-nodeids.json']); missing=nodes.pop()
                        metrics=json.loads(files['metrics.json'])
                        metrics['selected_nodeids']=nodes
                        metrics['results']=[result for result in metrics['results'] if result['nodeid']!=missing]
                        metadata['selected_nodeids']=nodes
                        files['selected-nodeids.json']=json.dumps(nodes).encode()
                        files['metrics.json']=json.dumps(metrics).encode()
                        files['junit.xml']=module.literal_junit(metrics)
                        for field, name in [('metrics_sha256','metrics.json'),
                                            ('selected_nodeids_sha256','selected-nodeids.json'),('junit_sha256','junit.xml')]:
                            metadata['environment'][field]=hashlib.sha256(files[name]).hexdigest()
                    files['evidence.json']=json.dumps(metadata).encode()
                    api.downloads[artifact['id']]=module.pack(files)
                    artifact['digest']='sha256:'+hashlib.sha256(api.downloads[artifact['id']]).hexdigest()
                with check.assertRaises(module.preflight.PreflightError, msg=mutation):
                    module.preflight.verify_ci(api, module.SHA, root, module.NOW)


class OptionalFixtureTests:
    __test__ = True

    def actual_fixture(self, base):
        from tests.lp_d2_service_harness import offline_github_fixture
        root = base/'release'
        root.mkdir()
        policy_root(root)
        for name in ('ci_evidence.py', 'ci_partition.py'):
            shutil.copy(ROOT/'scripts'/name, root/'scripts'/name)
        for name in ('Dockerfile.dev', 'uv.lock'):
            shutil.copy(ROOT/name, root/name)
        # Distinct bytes establish that the actual release, rather than its seed
        # fixture's manifest hash, is bound into every reconstructed artifact.
        manifest = root/'scripts/ci_optional_tests.json'
        manifest.write_text(manifest.read_text()+'\n')
        for args in (['init','-q'], ['add','.'], ['-c','user.name=CI fixture',
                     '-c','user.email=ci@invalid','commit','-qm','disposable CI release']):
            subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
        sha = subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
        api = offline_github_fixture(root, base/'github')
        return root, sha, api, load('deployment_preflight')

    def test_actual_release_fixture_preserves_required_optional_proof(self):
        import hashlib
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as tmp:
            root, sha, api, verifier = self.actual_fixture(Path(tmp))
            report = verifier.verify_ci(api, sha, root, datetime.now(timezone.utc))
            check.assertEqual(report['run_attempt'], 2)
            files = verifier.read_archive(api.downloads[5])
            proof = json.loads(files['partition.json'])
            check.assertEqual(proof['optional'], [ROLLBACK])
            check.assertEqual(proof['optional_status'], 'not-executed-advisory')
            required = set(sum(proof['partitions'].values(), []))
            check.assertNotIn(ROLLBACK, required)
            check.assertEqual(set(proof['complete']), required|{ROLLBACK}|set(proof['retired']))
            check.assertFalse(required & set(proof['retired']))
            manifest_hash = hashlib.sha256((root/'scripts/ci_optional_tests.json').read_bytes()).hexdigest()
            check.assertNotEqual(manifest_hash, hashlib.sha256((ROOT/'scripts/ci_optional_tests.json').read_bytes()).hexdigest())
            for artifact in api.artifacts:
                files = verifier.read_archive(api.downloads[artifact['id']])
                metadata = json.loads(files['evidence.json'])
                check.assertEqual(metadata['source_sha'], sha)
                check.assertEqual(metadata['test_tier'], 'required')
                check.assertEqual(metadata['optional_policy']['manifest_sha256'], manifest_hash)
                check.assertEqual(metadata['optional_policy']['optional_nodeids'], [ROLLBACK])
                nodes = json.loads(files['selected-nodeids.json'])
                check.assertNotIn(ROLLBACK, nodes)
                check.assertEqual(json.loads(files['metrics.json'])['selected_nodeids'], nodes)
                if metadata['scope'] != 'portable':
                    check.assertEqual(nodes, proof['partitions'][metadata['scope']])

    def test_actual_release_fixture_rejects_missing_required_and_tampered_optional_policy(self):
        import copy
        import hashlib
        from datetime import datetime, timezone
        from tests.test_deployment_preflight import pack, literal_junit
        with tempfile.TemporaryDirectory() as tmp:
            root, sha, original, verifier = self.actual_fixture(Path(tmp))
            for mutation in ('missing_required', 'optional_identity', 'optional_hash',
                             'optional_as_required', 'optional_proof_identity'):
                api = copy.deepcopy(original)
                target = 5 if mutation == 'optional_proof_identity' else 4
                files = verifier.read_archive(api.downloads[target])
                metadata = json.loads(files['evidence.json'])
                if mutation == 'missing_required':
                    nodes = json.loads(files['selected-nodeids.json'])
                    missing = nodes.pop()
                    check.assertNotEqual(missing, ROLLBACK)
                    metrics = json.loads(files['metrics.json'])
                    metrics['selected_nodeids'] = nodes
                    metrics['results'] = [item for item in metrics['results'] if item['nodeid'] != missing]
                    files['metrics.json'] = json.dumps(metrics).encode()
                    files['selected-nodeids.json'] = json.dumps(nodes).encode()
                    files['junit.xml'] = literal_junit(metrics)
                    metadata['selected_nodeids'] = nodes
                elif mutation == 'optional_identity':
                    metadata['optional_policy']['optional_nodeids'] = [ROLLBACK+'_neighbor']
                elif mutation == 'optional_hash':
                    metadata['optional_policy']['manifest_sha256'] = '0'*64
                elif mutation == 'optional_as_required':
                    metadata['test_tier'] = 'optional'
                else:
                    proof = json.loads(files['partition.json'])
                    proof['optional'] = [ROLLBACK+'_neighbor']
                    files['partition.json'] = json.dumps(proof).encode()
                for field, name in [('metrics_sha256','metrics.json'),
                                    ('selected_nodeids_sha256','selected-nodeids.json'),
                                    ('junit_sha256','junit.xml'), ('partition_sha256','partition.json')]:
                    if name in files:
                        metadata['environment'][field] = hashlib.sha256(files[name]).hexdigest()
                files['evidence.json'] = json.dumps(metadata).encode()
                api.downloads[target] = pack(files)
                api.artifacts[target-1]['digest'] = 'sha256:'+hashlib.sha256(api.downloads[target]).hexdigest()
                with check.assertRaises(verifier.PreflightError, msg=mutation):
                    verifier.verify_ci(api, sha, root, datetime.now(timezone.utc))
