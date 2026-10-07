"""Portable CI evidence and collection-equivalence contract tests."""
import importlib.util
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PartitionTests(unittest.TestCase):
    def test_executed_and_retired_collections_cover_universe(self):
        module = load('ci_partition')
        parts = {'gateway': ['tests/test_gateway.py::test_ok'],
                 'legacy': ['tests/test_legacy.py::test_ok'],
                 'account': ['tests/test_account.py::test_ok'],
                 'prediction': ['tests/test_lp_example.py::test_lp',
                                'tests/test_prediction_runtime.py::test_paused']}
        retired = ['tests/test_prediction_solver.py::test_solver']
        complete = sum(parts.values(), []) + retired
        proof = module.verify_partition(complete, parts, retired, ROOT)
        self.assertEqual(proof['executed_count'], 5)
        self.assertEqual(proof['retired_count'], 1)
        self.assertEqual(proof['complete_count'], 6)
        self.assertEqual(proof['retired_status'], 'not-executed-permanent-retirement')
        nested = 'tests/nested/test_new.py::test_new'
        self.assertRaises(ValueError, module.verify_partition, complete + [nested], parts, retired, ROOT)
        expanded = {**parts, 'legacy': parts['legacy'] + [nested]}
        self.assertEqual(module.verify_partition(complete+[nested], expanded, retired, ROOT)['complete_count'], 7)
        for universe, executed, omitted in [
            (complete, {**parts, 'prediction': parts['prediction'][:-1]}, retired),
            (complete + [complete[0]], parts, retired),
            (complete, {**parts, 'prediction': parts['prediction'] + retired}, retired),
            (complete, {**parts, 'legacy': parts['legacy'] + [nested]}, retired),
            (complete, parts, [parts['prediction'][0]]),
            (complete, parts, []),
        ]:
            with self.subTest(universe=universe, executed=executed, omitted=omitted):
                self.assertRaises(ValueError, module.verify_partition, universe, executed, omitted, ROOT)

    def test_exact_nodeid_partition_and_fingerprints(self):
        module = load('ci_partition')
        parts = {scope: [f'tests/test_{scope}.py::test_ok'] for scope in module.SCOPES}
        complete = sum(parts.values(), [])
        proof = module.verify_partition(complete, parts)
        self.assertEqual(proof['status'], 'success')
        self.assertEqual(proof['complete_count'], 4)
        self.assertEqual(proof['partition_count'], 4)
        self.assertEqual(proof['complete_sha256'], proof['partition_sha256'])
        self.assertEqual(proof, module.verify_partition(list(reversed(complete)), parts))

    def test_missing_nested_extra_duplicate_and_empty_fail(self):
        module = load('ci_partition')
        parts = {scope: [f'tests/test_{scope}.py::test_ok'] for scope in module.SCOPES}
        complete = sum(parts.values(), [])
        for global_nodes, change in [
            (complete + ['tests/nested/test_new.py::test_new'], {}),
            (complete[:-1], {}),
            (complete, {'gateway': parts['gateway'] + parts['account']}),
            (complete, {'gateway': []}),
            (complete, {'gateway': ['tests/test_other.py::test_ok']}),
        ]:
            with self.subTest(change=change, global_nodes=global_nodes):
                with self.assertRaises(ValueError):
                    module.verify_partition(global_nodes, {**parts, **change})
        with self.assertRaises(ValueError):
            module.verify_partition(complete + [complete[0]], parts)
        with self.assertRaises(ValueError):
            module.verify_partition(complete, {'gateway': complete})

    def test_global_collection_uses_configured_testpaths(self):
        module = load('ci_partition')
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'nodes.json'
            output.write_text('["tests/nested/test_new.py::test_new"]')
            with patch.object(module.subprocess, 'run') as run:
                run.return_value.returncode=0
                self.assertEqual(module.collect(ROOT, [], output), ['tests/nested/test_new.py::test_new'])
                command=run.call_args.args[0]
                self.assertEqual(command[-2:], ['-o', 'cache_dir=/tmp/open-trader-collection-cache'])
                self.assertNotIn('tests/test_', command)

    def test_collection_error_cannot_use_partial_output(self):
        module = load('ci_partition')
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'partial.json'
            output.write_text('["tests/test_a.py::test_a"]')
            with patch.object(module.subprocess, 'run') as run:
                run.return_value.returncode = 2
                run.return_value.stdout = 'collection failed'
                run.return_value.stderr = 'missing dependency'
                with self.assertRaisesRegex(RuntimeError, 'missing dependency'):
                    module.collect(ROOT, ['tests'], output)
                command = run.call_args.args[0]
                self.assertIn('--collect-only', command)
                self.assertIn('not pressure and not browser', command)

    def test_collection_hook_records_selected_nodeids_not_output_text(self):
        from types import SimpleNamespace
        module = load('ci_partition')
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'nodes.json'
            session = SimpleNamespace(items=[SimpleNamespace(nodeid='tests/test_a.py::test_a[x]')])
            with patch.dict(os.environ, CI_COLLECTION_OUTPUT=str(output)):
                module.pytest_collection_finish(session)
            self.assertEqual(json.loads(output.read_text()), ['tests/test_a.py::test_a[x]'])


class EvidenceTests(unittest.TestCase):
    def test_successful_metrics_require_complete_measurement_records(self):
        module = load('ci_evidence')
        node = 'tests/test_dashboard_web.py::test_ok'
        valid = dict(schema_version=1, source_sha='a'*40, workers=1,
                     python='3.12.14', platform='Linux-x86_64-fixture', architecture='x86_64',
                     cpu_model='Example CPU', cpu_model_source='/proc/cpuinfo', cpu_model_unavailable=None,
                     cpu_count=2, selected_nodeids=[node], collection_errors=[], exit_status=0,
                     complete=True, wall_seconds=0.3,
                     results=[dict(nodeid=node, execution_nodeid=node, outcome='passed', phases={
                         'setup':dict(duration=0.1, outcome='passed', worker='serial'),
                         'call':dict(duration=0.1, outcome='passed', worker='serial'),
                         'teardown':dict(duration=0.1, outcome='passed', worker='serial')})])
        self.assertEqual(module.validate_metrics(valid, [node], 'a'*40, 1), valid)
        for omitted in ('python', 'platform', 'architecture', 'cpu_model', 'cpu_model_source',
                        'cpu_model_unavailable', 'cpu_count'):
            broken = copy.deepcopy(valid); del broken[omitted]
            with self.subTest(omitted=omitted):
                self.assertRaises(ValueError, module.validate_metrics, broken, [node], 'a'*40, 1)
        changes = [{'python':''}, {'platform':None}, {'architecture':''}, {'cpu_model_source':'unavailable'},
                   {'cpu_model':None}, {'cpu_model_unavailable':'unexpected'}, {'cpu_count':True}, {'cpu_count':0}]
        changes += [{'wall_seconds':value} for value in (-1, float('nan'), float('inf'), False, '0.1')]
        for change in changes:
            with self.subTest(change=change):
                self.assertRaises(ValueError, module.validate_metrics, {**valid, **change}, [node], 'a'*40, 1)
        for phases in ({}, {'setup':valid['results'][0]['phases']['setup']}):
            broken = copy.deepcopy(valid); broken['results'][0]['phases'] = phases
            self.assertRaises(ValueError, module.validate_metrics, broken, [node], 'a'*40, 1)
        for duration in (-1, float('nan'), float('inf'), True, '0.1', None):
            broken = copy.deepcopy(valid); broken['results'][0]['phases']['call']['duration'] = duration
            with self.subTest(duration=duration):
                self.assertRaises(ValueError, module.validate_metrics, broken, [node], 'a'*40, 1)
        for field, value in [('worker','gw0'), ('outcome','skipped')]:
            broken = copy.deepcopy(valid); broken['results'][0]['phases']['call'][field] = value
            self.assertRaises(ValueError, module.validate_metrics, broken, [node], 'a'*40, 1)
        broken = copy.deepcopy(valid); del broken['results'][0]['execution_nodeid']
        self.assertRaises(ValueError, module.validate_metrics, broken, [node], 'a'*40, 1)
        for skip_phase in ('setup','call'):
            skipped = copy.deepcopy(valid); skipped['results'][0]['outcome']='skipped'
            skipped['results'][0]['phases'][skip_phase]['outcome']='skipped'
            if skip_phase=='setup': del skipped['results'][0]['phases']['call']
            self.assertEqual(module.validate_metrics(skipped,[node],'a'*40,1),skipped)
        unknown = {**valid, 'cpu_model':None, 'cpu_model_source':'unavailable',
                   'cpu_model_unavailable':'native-read-failed', 'cpu_count':None}
        self.assertEqual(module.validate_metrics(unknown,[node],'a'*40,1),unknown)
        unknown['cpu_model_unavailable']=''
        self.assertRaises(ValueError,module.validate_metrics,unknown,[node],'a'*40,1)
        parallel = copy.deepcopy(valid); parallel['workers']=2
        for phase in parallel['results'][0]['phases'].values(): phase['worker']='gw1'
        self.assertEqual(module.validate_metrics(parallel,[node],'a'*40,2),parallel)
        parallel['results'][0]['phases']['call']['worker']='gw2'
        self.assertRaises(ValueError,module.validate_metrics,parallel,[node],'a'*40,2)
        failed = copy.deepcopy(valid); failed.update(complete=False,exit_status=1,collection_errors=['bad'])
        failed['results'][0].update(outcome='failed',phases={})
        del failed['python']
        self.assertEqual(module.validate_metrics(failed,[node],'a'*40,1,False),failed)
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            for name, data in [('metrics.json',json.dumps(valid)),('selected-nodeids.json',json.dumps([node])),
                               ('junit.xml','<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0"><testcase classname="tests.test_dashboard_web" name="test_ok" time="0.3"/></testsuite></testsuites>'),('image.json','[{}]'),('dependency-manifest.json','{}')]:
                (directory/name).write_text(data)
            env=dict(GITHUB_SHA='a'*40,GITHUB_REPOSITORY='raymizzou/open_trader',
                     GITHUB_WORKFLOW_REF='raymizzou/open_trader/.github/workflows/ci.yml@refs/heads/main',
                     GITHUB_EVENT_NAME='push',GITHUB_REF='refs/heads/main',GITHUB_RUN_ID='123',
                     GITHUB_RUN_ATTEMPT='2',RUNNER_OS='Linux',RUNNER_ARCH='X64')
            self.assertEqual(module.build_evidence(ROOT,directory,'legacy','0',1,0,env)['status'],'success')
            for key in ('RUNNER_OS','RUNNER_ARCH'):
                missing={**env,key:''}
                self.assertRaises(ValueError,module.build_evidence,ROOT,directory,'legacy','0',1,0,missing)
            (directory/'metrics.json').write_text(json.dumps(failed))
            self.assertEqual(module.build_evidence(ROOT,directory,'legacy','0',1,7,env)['status'],'failure')

    def test_active_evidence_binds_policy_selection_and_run(self):
        module = load('ci_evidence')
        partition = load('ci_partition')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            env = dict(GITHUB_SHA='a'*40, GITHUB_REPOSITORY='raymizzou/open_trader',
                       GITHUB_WORKFLOW_REF='raymizzou/open_trader/.github/workflows/ci.yml@refs/heads/main',
                       GITHUB_EVENT_NAME='push', GITHUB_REF='refs/heads/main', GITHUB_RUN_ID='123',
                       GITHUB_RUN_ATTEMPT='2', RUNNER_OS='Linux', RUNNER_ARCH='X64')
            (directory/'dependency-manifest.json').write_text('{}')
            (directory/'image.json').write_text('[{}]')
            parts = {'gateway': ['tests/test_gateway.py::test_ok'], 'legacy': ['tests/test_legacy.py::test_ok'],
                     'account': ['tests/test_account.py::test_ok'],
                     'prediction': ['tests/test_lp_example.py::test_lp', 'tests/test_prediction_runtime.py::test_paused']}
            retired = ['tests/test_prediction_solver.py::test_solver']
            proof = partition.verify_partition(sum(parts.values(), [])+retired, parts, retired, ROOT)
            (directory/'partition.json').write_text(json.dumps(proof))
            node = 'acceptance/test_prediction_arbitrage_scenarios.py::test_ok'
            metrics = dict(schema_version=1, source_sha='a'*40, workers=1, selected_nodeids=[node],
                           python='3.12.14',platform='Linux/x86_64',architecture='x86_64',
                           cpu_model='Example CPU',cpu_model_source='/proc/cpuinfo',cpu_model_unavailable=None,cpu_count=2,
                           results=[dict(nodeid=node, execution_nodeid=node, outcome='passed', phases={'setup':dict(duration=0.0,outcome='passed',worker='serial'),
                               'call':dict(duration=0.1,outcome='passed',worker='serial'), 'teardown':dict(duration=0.0,outcome='passed',worker='serial')})],
                           collection_errors=[], complete=True, exit_status=0, wall_seconds=0.2)
            (directory/'metrics.json').write_text(json.dumps(metrics))
            (directory/'selected-nodeids.json').write_text(json.dumps([node]))
            (directory/'junit.xml').write_text('<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0"><testcase classname="acceptance.test_prediction_arbitrage_scenarios" name="test_ok" time="0.1"/></testsuite></testsuites>')
            evidence = module.build_evidence(ROOT, directory, 'portable', '0', 1, 0, env)
            self.assertEqual(evidence['policy'], module.retirement_policy(ROOT))
            self.assertEqual(evidence['policy']['manifest_sha256'], module.file_sha256(ROOT/'scripts/ci_nleg_retired.json'))
            self.assertEqual(evidence['test_n_leg'], '0')
            self.assertEqual(evidence['run_attempt'], '2')
            self.assertEqual(evidence['selected_nodeids'], [node])
            self.assertEqual(evidence['environment']['metrics_sha256'], module.file_sha256(directory/'metrics.json'))
            self.assertEqual(module.build_evidence(ROOT, directory, 'portable', '0', 1, 7, env)['status'], 'failure')
            for nleg, change in [('1', {}), ('0', {'GITHUB_SHA':'bad'}), ('0', {'GITHUB_RUN_ATTEMPT':''})]:
                self.assertRaises(ValueError, module.build_evidence, ROOT, directory, 'portable', nleg, 1, 0, {**env, **change})
            for field, value in [('complete', False), ('selected_nodeids', []), ('source_sha', 'b'*40), ('collection_errors', ['bad'])]:
                (directory/'metrics.json').write_text(json.dumps({**metrics, field:value}))
                self.assertRaises(ValueError, module.build_evidence, ROOT, directory, 'portable', '0', 1, 0, env)
            (directory/'metrics.json').write_text(json.dumps(metrics))
            (directory/'partition.json').write_text(json.dumps({**proof,'policy':'unknown'}))
            self.assertRaises(ValueError, module.build_evidence, ROOT, directory, 'portable', '0', 1, 0, env)

    def test_selection_is_exact_make_partition_and_serial_portable(self):
        module = load('ci_evidence')
        parts = module.service_partitions(ROOT)
        for name in ('client', 'cloud', 'monitor_selection_driver', 'n_leg_cutover'):
            self.assertIn(f'tests/test_prediction_{name}.py', parts['prediction'])
        self.assertNotIn('tests/test_prediction_solver_benchmark.py', parts['prediction'])
        self.assertIn('tests/test_prediction_solver_benchmark.py', module.service_partitions(ROOT,'1')['prediction'])
        for scope in module.BACKEND_SCOPES:
            self.assertEqual(module.expected_selection(scope, ROOT), {
                'roots': parts[scope], 'marker': 'not pressure and not browser', 'keyword': ''})
        self.assertEqual(module.expected_selection('portable', ROOT), {
            'roots': ['acceptance/test_prediction_arbitrage_scenarios.py'],
            'marker': 'not pressure and not browser', 'keyword': 'not LIVE'})
        with self.assertRaises(ValueError):
            module.expected_selection('bogus', ROOT)

    def test_evidence_binds_run_attempt_source_selection_environment_and_status(self):
        module = load('ci_evidence')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'dependency-manifest.json').write_text('{"source_state":"clean"}')
            (directory / 'image.json').write_text('[{}]')
            partition=load('ci_partition')
            parts={scope:[f'tests/test_{scope}.py::test_ok'] for scope in partition.SCOPES}
            (directory/'partition.json').write_text(json.dumps(partition.verify_partition(sum(parts.values(),[]),parts)))
            node='acceptance/test_prediction_arbitrage_scenarios.py::test_ok'
            (directory/'metrics.json').write_text(json.dumps(dict(schema_version=1,source_sha='a'*40,workers=1,
                python='3.12.14',platform='Linux/x86_64',architecture='x86_64',cpu_model='Example CPU',
                cpu_model_source='/proc/cpuinfo',cpu_model_unavailable=None,cpu_count=2,
                selected_nodeids=[node],results=[dict(nodeid=node,execution_nodeid=node,outcome='passed',
                phases={phase:dict(duration=0.1,outcome='passed',worker='serial') for phase in ('setup','call','teardown')})],collection_errors=[],
                complete=True,exit_status=0,wall_seconds=1.0)))
            (directory/'selected-nodeids.json').write_text(json.dumps([node]))
            (directory/'junit.xml').write_text('<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0"><testcase classname="acceptance.test_prediction_arbitrage_scenarios" name="test_ok" time="0.1"/></testsuite></testsuites>')
            env = dict(GITHUB_SHA='a' * 40, GITHUB_REPOSITORY='raymizzou/open_trader',
                       GITHUB_WORKFLOW_REF='raymizzou/open_trader/.github/workflows/ci.yml@refs/heads/main',
                       GITHUB_EVENT_NAME='push', GITHUB_REF='refs/heads/main', GITHUB_RUN_ID='123',
                       GITHUB_RUN_ATTEMPT='2', RUNNER_OS='Linux', RUNNER_ARCH='X64')
            evidence = module.build_evidence(ROOT, directory, 'portable', '0', 1, 0, env)
            self.assertEqual(evidence['schema_version'], 2)
            self.assertEqual(evidence['evidence_role'], 'test-only')
            self.assertEqual(evidence['run_id'], '123')
            self.assertEqual(evidence['run_attempt'], '2')
            self.assertEqual(evidence['source_sha'], 'a' * 40)
            self.assertEqual(evidence['status'], 'success')
            self.assertEqual(evidence['selection'], module.expected_selection('portable', ROOT))
            self.assertEqual(evidence['environment']['dependency_manifest_sha256'],
                             module.file_sha256(directory / 'dependency-manifest.json'))
            self.assertEqual(evidence['environment']['partition_sha256'], module.file_sha256(directory / 'partition.json'))
            self.assertEqual(module.build_evidence(ROOT, directory, 'portable', '0', 1, 7, env)['status'], 'failure')
            with self.assertRaises(ValueError):
                module.build_evidence(ROOT, directory, 'portable', '0', 2, 0, env)
            with self.assertRaises(ValueError):
                module.build_evidence(ROOT, directory, 'portable', '1', 1, 0, env)
            with self.assertRaises(ValueError):
                module.build_evidence(ROOT, directory, 'portable', '0', 1, 0, {**env, 'GITHUB_RUN_ATTEMPT': ''})


def test_real_collection_rejects_partial_results():
    module = load('ci_partition')
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root/'tests').mkdir()
        (root/'tests/test_ok.py').write_text('def test_ok(): pass\n')
        nodes = module.collect(root, ['tests'], root/'nodes.json')
        assert nodes == ['tests/test_ok.py::test_ok']
        (root/'tests/test_bad.py').write_text('import missing_ci_collection_dependency\n')
        try:
            module.collect(root, ['tests'], root/'nodes.json')
        except RuntimeError as error:
            assert 'missing_ci_collection_dependency' in str(error)
        else:
            raise AssertionError('collection error reused partial results')


if __name__ == '__main__':
    unittest.main()
