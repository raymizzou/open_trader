"""Portable CI evidence and collection-equivalence contract tests."""
import importlib.util
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
        from types import SimpleNamespace
        module = load('ci_partition')
        parts = {scope: [f'tests/test_{scope}.py::test_ok'] for scope in module.SCOPES}
        selections = {scope: [f'tests/test_{scope}.py'] for scope in module.SCOPES}
        helper = SimpleNamespace(service_partitions=lambda root: selections)
        with patch.dict(sys.modules, ci_evidence=helper), patch.object(module, 'collect') as collect:
            collect.side_effect = [sum(parts.values(), []), *parts.values()]
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                module.main()
            self.assertEqual(collect.call_args_list[0].args[1], [])
            self.assertEqual(json.loads(output.getvalue())['status'], 'success')

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
    def test_selection_is_exact_make_partition_and_serial_portable(self):
        module = load('ci_evidence')
        parts = module.service_partitions(ROOT)
        for name in ('client', 'cloud', 'solver_benchmark'):
            self.assertIn(f'tests/test_prediction_{name}.py', parts['prediction'])
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
            (directory / 'partition.json').write_text('{}')
            env = dict(GITHUB_SHA='a' * 40, GITHUB_REPOSITORY='raymizzou/open_trader',
                       GITHUB_WORKFLOW_REF='raymizzou/open_trader/.github/workflows/ci.yml@refs/heads/main',
                       GITHUB_EVENT_NAME='push', GITHUB_REF='refs/heads/main', GITHUB_RUN_ID='123',
                       GITHUB_RUN_ATTEMPT='2', RUNNER_OS='Linux', RUNNER_ARCH='X64')
            evidence = module.build_evidence(ROOT, directory, 'portable', '1', 1, 0, env)
            self.assertEqual(evidence['schema_version'], 1)
            self.assertEqual(evidence['evidence_role'], 'test-only')
            self.assertEqual(evidence['run_id'], '123')
            self.assertEqual(evidence['run_attempt'], '2')
            self.assertEqual(evidence['source_sha'], 'a' * 40)
            self.assertEqual(evidence['status'], 'success')
            self.assertEqual(evidence['selection'], module.expected_selection('portable', ROOT))
            self.assertEqual(evidence['environment']['dependency_manifest_sha256'],
                             module.file_sha256(directory / 'dependency-manifest.json'))
            self.assertEqual(evidence['environment']['partition_sha256'], module.file_sha256(directory / 'partition.json'))
            self.assertEqual(module.build_evidence(ROOT, directory, 'portable', '1', 1, 7, env)['status'], 'failure')
            with self.assertRaises(ValueError):
                module.build_evidence(ROOT, directory, 'portable', '1', 2, 0, env)
            with self.assertRaises(ValueError):
                module.build_evidence(ROOT, directory, 'portable', '0', 1, 0, env)
            with self.assertRaises(ValueError):
                module.build_evidence(ROOT, directory, 'portable', '1', 1, 0, {**env, 'GITHUB_RUN_ATTEMPT': ''})


if __name__ == '__main__':
    unittest.main()
