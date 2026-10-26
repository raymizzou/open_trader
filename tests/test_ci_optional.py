"""Exact reviewed release advisory policy at CI's public selection and evidence seams.

Keep module import safe for the stock-host unittest discovery job.
"""
import hashlib
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
    # Safe minimal source scaffolding for every declared optional path. Final real
    # repository collection independently proves these full parameter identities.
    grouped = {}
    for node in json.loads((root/'scripts/ci_optional_tests.json').read_text())['optional']:
        path, identity = node.split('::', 1)
        name, sep, parameter = identity.partition('[')
        grouped.setdefault(path, {}).setdefault(name, []).append(parameter[:-1] if sep else None)
    for path, functions in grouped.items():
        lines = ['import pytest\n']
        for name, parameters in functions.items():
            if parameters == [None]:
                lines.append(f'def {name}(): pass\n')
            else:
                lines.append(f'@pytest.mark.parametrize("_case", range({len(parameters)}), ids={parameters!r})\n'
                             f'def {name}(_case): pass\n')
        (root/path).write_text(''.join(lines))
    for name in ('frontend_gateway', 'account_api', 'dashboard_web'):
        (root/('tests/test_'+name+'.py')).write_text('def test_required(): pass\n')
    lifecycle = root/'tests/test_lp_d2_service_lifecycle.py'
    lifecycle.write_text(lifecycle.read_text()+
        'def test_supported_rollback_neighbor(): pass\n'
        'def test_new(): pass\n'
        'def test_supported_rollback_uses_current_api_without_disposable_record_audit_neighbor(): pass\n')


class OptionalPolicyTests:
    __test__ = True

    def test_only_reviewed_release_nodes_are_optional(self):
        ci = load('ci_evidence')
        selection = ci.expected_selection('prediction', ROOT)
        reviewed = selection['deselect']
        check.assertEqual(len(reviewed), 279)
        check.assertEqual(hashlib.sha256(json.dumps(sorted(reviewed), separators=(',', ':')).encode()).hexdigest(),
                          '607edf6f00872abe4e5c8de99d5d73828fd63a7915fbb313dde0a1c448071621')
        ready = 'tests/test_lp_d2_service_cutover_repairs.py::test_explicit_systemd_restart_recovers_absent_ready_or_failed_record[ready]'
        failed = 'tests/test_lp_d2_service_cutover_repairs.py::test_explicit_systemd_restart_recovers_absent_ready_or_failed_record[failed]'
        foreground = 'tests/test_lp_d2_service_cutover_repairs.py::test_foreground_operator_from_other_cwd_remains_manageable'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy_root(root)
            (root/'tests/test_lp_d2_service_cutover_repairs.py').write_text(
                'import pytest\n'
                '@pytest.mark.parametrize("state", ["ready", "failed", "future"])\n'
                'def test_explicit_systemd_restart_recovers_absent_ready_or_failed_record(state): pass\n'
                'def test_foreground_operator_from_other_cwd_remains_manageable(): pass\n'
                'def test_post_ready_failure_keeps_claim_until_actual_drain_and_human_recovery(): pass\n'
                'def test_foreground_operator_from_other_cwd_remains_manageable_neighbor(): pass\n')
            partition = load('ci_partition')
            paths = ['tests/test_lp_d2_service_lifecycle.py', 'tests/test_lp_d2_service_cutover_repairs.py']
            full = partition.collect(root, paths, root/'all.json')
            required = partition.collect(root, paths, root/'required.json', required=True)
            check.assertEqual(set(full)-set(required), {ROLLBACK, ready, failed, foreground})
            check.assertEqual(set(required), {
                'tests/test_lp_d2_service_lifecycle.py::test_new',
                'tests/test_lp_d2_service_lifecycle.py::test_supported_rollback_neighbor', ROLLBACK+'_neighbor',
                'tests/test_lp_d2_service_cutover_repairs.py::test_explicit_systemd_restart_recovers_absent_ready_or_failed_record[future]',
                'tests/test_lp_d2_service_cutover_repairs.py::test_post_ready_failure_keeps_claim_until_actual_drain_and_human_recovery',
                foreground+'_neighbor'})

    def test_invalid_missing_or_expanded_optional_declarations_fail(self):
        ci = load('ci_evidence')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy_root(root)
            path = root/'scripts/ci_optional_tests.json'
            original = json.loads(path.read_text())
            reviewed = original['optional']
            check.assertEqual(ci.optional_policy(root)['optional_nodeids'], reviewed)
            check.assertEqual(len(reviewed), 279)
            check.assertEqual(ci.optional_policy(root)['policy'], 'release-advisory-v1')
            changed_parameter = [node.replace('[ready]', '[future]') for node in reviewed]
            for nodes in ([], reviewed[:-1], reviewed+[reviewed[0]], changed_parameter,
                          [ROLLBACK, ROLLBACK], [ROLLBACK+'*'],
                          ['tests/test_lp_d2_service_lifecycle.py::test_new'],
                          [ROLLBACK, 'tests/test_lp_d2_service_lifecycle.py::test_new']):
                with check.subTest(nodes=nodes):
                    path.write_text(json.dumps({**original, 'optional':nodes}))
                    check.assertRaises(ValueError, ci.expected_selection, 'prediction', root)
            for change in ({'policy':'unreviewed'}, {'schema_version':True}, {'unexpected':[]}):
                path.write_text(json.dumps({**original, **change}))
                check.assertRaises(ValueError, ci.expected_selection, 'prediction', root)
            path.write_text(json.dumps(original))
            source = root/'tests/test_lp_d2_service_lifecycle.py'
            original_source = source.read_text()
            source.unlink()
            source.symlink_to(root/'tests/test_account_api.py')
            check.assertRaises(ValueError, ci.expected_selection, 'prediction', root)
            source.unlink()
            source.write_text(original_source)
            (root/'tests/test_lp_d2_service_lifecycle.py').unlink()
            check.assertRaises(ValueError, ci.expected_selection, 'prediction', root)
            path.unlink()
            check.assertRaises((ValueError, FileNotFoundError), ci.expected_selection, 'prediction', root)

    def test_required_optional_retired_form_disjoint_complete_partition(self):
        partition = load('ci_partition')
        ci = load('ci_evidence')
        optional = json.loads((ROOT/'scripts/ci_optional_tests.json').read_text())['optional']
        parts = {'gateway':['tests/test_frontend_gateway.py::test_required'],
                 'legacy':['tests/test_dashboard_web.py::test_required'],
                 'account':['tests/test_account_api.py::test_required'],
                 'prediction':['tests/test_lp_d2_service_lifecycle.py::test_new',
                     'tests/test_lp_d2_service_lifecycle.py::test_supported_rollback_neighbor']}
        retired = ['tests/test_prediction_solver.py::test_retired']
        complete = sum(parts.values(), [])+retired+optional
        # This public verifier must refuse to count optional declaration as execution.
        with check.assertRaises(ValueError):
            partition.verify_partition(complete, {**parts, 'prediction':parts['prediction']+[ROLLBACK]}, retired, ROOT)
        proof = partition.verify_partition(complete, parts, retired, ROOT, optional=optional)
        check.assertEqual(proof['optional'], optional)
        check.assertEqual(proof['optional_count'], 279)
        check.assertEqual(proof['optional_sha256'], '607edf6f00872abe4e5c8de99d5d73828fd63a7915fbb313dde0a1c448071621')
        check.assertEqual(proof['optional_status'], 'not-executed-advisory')
        check.assertEqual(proof['executed_count'], 5)
        check.assertEqual(proof['complete_count'], 285)
        check.assertEqual(proof['optional_policy'], ci.optional_policy(ROOT))
        check.assertEqual(ci.validate_partition(ROOT, proof), proof)
        wrong_parameter = [node.replace('[ready]', '[future]') for node in optional]
        for universe, executed, omitted in [
            (complete, parts, []), (complete, parts, optional+[ROLLBACK]),
            (complete, {**parts, 'prediction':parts['prediction']+[ROLLBACK]}, optional),
            (complete, parts, [ROLLBACK+'*']), (complete[:-1], parts, optional),
            (complete, parts, wrong_parameter), (complete, {**parts, 'prediction':parts['prediction'][:-1]}, optional),
            (complete+['tests/test_future.py::test_new'], parts, optional),
        ]:
            with check.subTest(universe=universe, executed=executed, optional=omitted):
                check.assertRaises(ValueError, partition.verify_partition, universe, executed, retired, ROOT, optional=omitted)
        for change in ({'optional_status':'passed'}, {'executed_count':229}):
            check.assertRaises(ValueError, ci.validate_partition, ROOT, {**proof, **change})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy_root(root)
            paths = sorted({node.split('::', 1)[0] for node in optional})
            full = partition.collect(root, paths, root/'full.json')
            required = partition.collect(root, paths, root/'required.json', required=True)
            advisory = partition.collect(root, optional, root/'optional.json')
            check.assertEqual(sorted(advisory), optional)
            check.assertEqual(len(advisory), 279)
            check.assertEqual(set(full), set(required)|set(advisory))
            check.assertFalse(set(required)&set(advisory))


class OptionalRunnerTests:
    __test__ = True

    def test_optional_failure_preserves_exit_status_and_evidence(self):
        spec = importlib.util.spec_from_file_location('optional_runner_fixture', ROOT/'tests/test_ci_runner.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        runner = fixture.RunnerTests()
        # Exercise Make's real shell serialization: parameter IDs contain spaces
        # and shell metacharacters, but each full identity must remain one argv.
        import shlex
        dryrun = subprocess.run(['make', '-n', 'test-ci-release-advisory',
                                 'TEST_WORKERS=1', 'CI_TEST_ARTIFACTS=1'],
                                cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True)
        check.assertEqual(dryrun.returncode, 0, dryrun.stdout+dryrun.stderr)
        command = next(line for line in dryrun.stdout.splitlines() if line.startswith('docker run '))
        args = shlex.split(command)
        check.assertEqual(args[args.index('--basetemp=/tmp/open-trader-pytest')+1:],
                          json.loads((ROOT/'scripts/ci_optional_tests.json').read_text())['optional'])
        check.assertNotIn('--ci-required', args)
        check.assertNotIn('-n', args)
        for control in ('--network none', '--cap-drop ALL', '--security-opt no-new-privileges'):
            check.assertIn(control, command)
        for change in ({'fail_make':True}, {'fail_make':True, 'fail_copy':True}):
            run, evidence, commands = runner.run_scope('release-advisory', **change)
            check.assertEqual(run.returncode, 7, run.stdout+run.stderr)
            check.assertIn('synthetic test failure', run.stdout)
            check.assertEqual(evidence['status'], 'failure')
            check.assertEqual(evidence['exit_status'], 7)
            check.assertEqual(evidence['scope'], 'release-advisory')
            check.assertEqual(evidence['test_tier'], 'optional')
            check.assertEqual(evidence['workers'], 1)
            check.assertEqual(evidence['source_sha'], 'a'*40)
            check.assertEqual(evidence['run_attempt'], '2')
            check.assertIn('make test-ci-release-advisory', commands)
            check.assertNotIn(' -n ', commands)
            if not change.get('fail_copy'):
                check.assertEqual(evidence['selected_nodeids'], json.loads((ROOT/'scripts/ci_optional_tests.json').read_text())['optional'])
                check.assertEqual(len(evidence['selected_nodeids']), 279)
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
        check.assertTrue(path.is_file(), 'missing independent Release advisory workflow')
        text = path.read_text()
        check.assertTrue(text.startswith('name: Release advisory\n'))
        events = text.split('on:\n', 1)[1].split('\npermissions:', 1)[0]
        check.assertEqual(events.strip(), 'push:\n    branches: [main]\n  workflow_dispatch:')
        check.assertIn('permissions:\n  contents: read', text)
        check.assertEqual(re.findall(r'^  ([\w-]+):$', text.split('jobs:\n',1)[1], re.M), ['release-advisory'])
        check.assertIn('runs-on: ubuntu-24.04', text)
        check.assertIn('timeout-minutes: 60', text)
        for forbidden in ('needs:', 'continue-on-error', 'secrets.', 'workflow_run', 'pull_request', 'schedule:'):
            check.assertNotIn(forbidden, text)
        check.assertIn('ref: ${{ github.sha }}', text)
        check.assertIn('persist-credentials: false', text)
        check.assertIn('scripts/run_ci_service.sh "release-advisory-${{ matrix.shard }}" "0"', text)
        check.assertIn('if: always()', text)
        check.assertIn('retention-days: 3', text)
        check.assertIn('name: ci-release-advisory-${{ matrix.shard }}-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}', text)
        required = (ROOT/'.github/workflows/ci.yml').read_text().split('  required:\n',1)[1]
        check.assertIn('needs: [plan, gateway, legacy, account, prediction, portable, trend_curve]', required)
        check.assertNotIn('advisory', required)
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
                'name':'ci-release-advisory-'+module.SHA+'-22-2'})
            report = module.preflight.verify_ci(api, module.SHA, root, module.NOW)
            check.assertEqual(len(report['artifacts']), 7)
            check.assertFalse(any('advisory' in call or call=='download:99' for call in api.calls))
            files = module.preflight.read_archive(api.downloads[7])
            proof = json.loads(files['partition.json'])
            check.assertEqual(proof['optional'], json.loads((ROOT/'scripts/ci_optional_tests.json').read_text())['optional'])
            check.assertEqual(proof['optional_count'], 279)
            check.assertEqual(proof['optional_policy']['policy'], 'release-advisory-v1')
            check.assertEqual(proof['optional_sha256'], '607edf6f00872abe4e5c8de99d5d73828fd63a7915fbb313dde0a1c448071621')
            check.assertEqual(proof['optional_status'], 'not-executed-advisory')
            check.assertFalse(set(proof['optional']) & set(sum(proof['partitions'].values(), [])))

    def test_optional_policy_cannot_hide_other_missing_or_failed_tests(self):
        import hashlib
        mutations = ('optional_as_required', 'optional_policy_hash', 'manifest_bytes',
                     'missing_required', 'failed_required', 'wrong_sha', 'expanded_optional',
                     'wrong_app', 'wrong_run_attempt', 'wrong_parameter')
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
                elif mutation == 'wrong_run_attempt':
                    module.mutate_metadata(api, lambda metadata: metadata.update(run_attempt='1'))
                elif mutation == 'wrong_app':
                    next(item for item in api.checks if item['name'] == 'required')['app']['id'] = 999
                elif mutation == 'wrong_parameter':
                    module.mutate_metadata(api, lambda metadata: metadata['optional_policy'].update(optional_nodeids=[
                        node.replace('[ready]', '[future]') for node in metadata['optional_policy']['optional_nodeids']]))
                else:
                    index = 6 if mutation == 'expanded_optional' else 3
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
        for name in ('ci_evidence.py', 'ci_partition.py', 'deployment_preflight.py'):
            shutil.copy(ROOT/'scripts'/name, root/'scripts'/name)
        for name in ('Dockerfile.dev', 'uv.lock'):
            shutil.copy(ROOT/name, root/name)
        (root/'tests/test_lp_fixture.py').write_text('def test_a(): pass\ndef test_b(): pass\ndef test_c(): pass\ndef test_d(): pass\n')
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
            files = verifier.read_archive(api.downloads[7])
            proof = json.loads(files['partition.json'])
            optional = json.loads((ROOT/'scripts/ci_optional_tests.json').read_text())['optional']
            check.assertEqual(proof['optional'], optional)
            check.assertEqual(proof['optional_count'], 279)
            check.assertEqual(proof['optional_sha256'], '607edf6f00872abe4e5c8de99d5d73828fd63a7915fbb313dde0a1c448071621')
            check.assertEqual(len({node.split('::')[0] for node in optional}), 10)
            check.assertIn('tests/test_lp_d2_service_cutover_repairs.py::test_explicit_systemd_restart_recovers_absent_ready_or_failed_record[ready]', optional)
            check.assertIn('tests/test_lp_d2_service_cutover_repairs.py::test_explicit_systemd_restart_recovers_absent_ready_or_failed_record[failed]', optional)
            check.assertEqual(proof['optional_status'], 'not-executed-advisory')
            required = set(sum(proof['partitions'].values(), []))
            check.assertNotIn(ROLLBACK, required)
            check.assertEqual(set(proof['complete']), required|set(optional)|set(proof['retired']))
            check.assertFalse(required & set(proof['retired']))
            manifest_hash = hashlib.sha256((root/'scripts/ci_optional_tests.json').read_bytes()).hexdigest()
            check.assertNotEqual(manifest_hash, hashlib.sha256((ROOT/'scripts/ci_optional_tests.json').read_bytes()).hexdigest())
            for artifact in api.artifacts:
                files = verifier.read_archive(api.downloads[artifact['id']])
                metadata = json.loads(files['evidence.json'])
                check.assertEqual(metadata['source_sha'], sha)
                check.assertEqual(metadata['test_tier'], 'required')
                check.assertEqual(metadata['optional_policy']['manifest_sha256'], manifest_hash)
                check.assertEqual(metadata['optional_policy']['optional_nodeids'], optional)
                nodes = json.loads(files['selected-nodeids.json'])
                check.assertNotIn(ROLLBACK, nodes)
                check.assertFalse(set(optional) & set(nodes))
                check.assertEqual(json.loads(files['metrics.json'])['selected_nodeids'], nodes)
                if metadata['scope'] != 'portable':
                    scope=metadata['scope']
                    expected=proof['partitions']['prediction' if scope.startswith('prediction-') else scope]
                    if scope.startswith('prediction-'):
                        expected=load('ci_partition').shard_nodeids('prediction',expected,int(scope[-1]))
                    check.assertEqual(nodes,expected)

    def test_actual_release_fixture_rejects_missing_required_and_tampered_optional_policy(self):
        import copy
        import hashlib
        from datetime import datetime, timezone
        from tests.test_deployment_preflight import pack, literal_junit
        with tempfile.TemporaryDirectory() as tmp:
            root, sha, original, verifier = self.actual_fixture(Path(tmp))
            for mutation in ('missing_required', 'optional_identity', 'optional_hash',
                             'optional_as_required', 'optional_proof_identity', 'optional_parameter_identity'):
                api = copy.deepcopy(original)
                target = 7 if mutation == 'optional_proof_identity' else 4
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
                elif mutation == 'optional_parameter_identity':
                    metadata['optional_policy']['optional_nodeids'] = [
                        node.replace('[ready]', '[future]') for node in metadata['optional_policy']['optional_nodeids']]
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
