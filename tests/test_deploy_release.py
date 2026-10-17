"""Forward-deployment wiring; never invoke a real installer or production gate."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('deploy_release', ROOT / 'scripts/deploy_release.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DeploymentWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'scripts').mkdir()
        self.python = self.root / 'venv/bin/python'
        self.runtime = self.root / 'runtime'
        self.sha = 'a' * 40

    def args(self, *more):
        return ['--expected-sha', self.sha, '--release-root', str(self.root),
                '--runtime-root', str(self.runtime), '--python', str(self.python), *more]

    @patch.object(MODULE, 'run_preflight')
    @patch.object(MODULE.subprocess, 'run')
    def test_dashboard_binds_all_paths_without_shell(self, run, preflight):
        run.return_value.returncode = 0
        self.assertEqual(MODULE.main(self.args('dashboard', '--mode', 'gateway')), 0)
        preflight.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['bash', str(self.root / 'scripts/install_dashboard_launchd.sh')])
        for option, value in [('--repo-root', str(self.root)), ('--runtime-root', str(self.runtime)),
                              ('--python', str(self.python)), ('--mode', 'gateway')]:
            self.assertEqual(command[command.index(option) + 1], value)
        self.assertEqual(run.call_args.kwargs['cwd'], self.root)
        self.assertNotIn('shell', run.call_args.kwargs)

    @patch.object(MODULE, 'run_preflight', side_effect=ValueError('missing CI evidence'))
    @patch.object(MODULE.subprocess, 'run')
    def test_failed_preflight_never_invokes_installer(self, run, preflight):
        self.assertEqual(MODULE.main(self.args('account')), 2)
        run.assert_not_called()

    @patch.object(MODULE, 'run_preflight')
    @patch.object(MODULE.subprocess, 'run')
    def test_cloud_config_mismatch_blocks_before_preflight_or_install(self, run, preflight):
        config = self.root / 'cloud.json'
        config.write_text(json.dumps({'mode': 'production', 'expected_sha': 'b' * 40, 'release_root': str(self.root),
                                     'runtime_root': str(self.runtime), 'python': str(self.python)}))
        self.assertEqual(MODULE.main(self.args('prediction-systemd', '--config', str(config))), 2)
        preflight.assert_not_called()
        run.assert_not_called()

    @patch.object(MODULE, 'run_preflight')
    @patch.object(MODULE.subprocess, 'run')
    def test_cloud_binds_selected_interpreter_and_exact_config(self, run, preflight):
        nonce = 'selected-operation-nonce'
        run.side_effect = [SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='PREPARED', operation_nonce=nonce))),
                           SimpleNamespace(returncode=0)]
        config = self.root / 'cloud.json'
        config.write_text(json.dumps({'mode': 'production', 'expected_sha': self.sha, 'release_root': str(self.root),
                                     'runtime_root': str(self.runtime), 'python': str(self.python)}))
        self.assertEqual(MODULE.main(self.args('prediction-systemd', '--config', str(config), '--action', 'start')), 0)
        command = run.call_args.args[0]
        self.assertEqual(run.call_args_list[0].args[0][2], 'prepare-target')
        self.assertEqual(command[2:5], ['start', '--config', str(config)])
        self.assertEqual(command[-2:], ['--operation-nonce', nonce])
        preflight.assert_not_called()
        self.assertEqual(run.call_args.kwargs['env']['OPEN_TRADER_PYTHON'], str(self.python))

    @patch.object(MODULE, 'run_preflight')
    @patch.object(MODULE.subprocess, 'run')
    def test_install_failure_is_preserved_and_does_not_auto_rollback(self, run, preflight):
        run.return_value.returncode = 9
        self.assertEqual(MODULE.main(self.args('account')), 9)
        self.assertEqual(run.call_count, 1)

    @patch.object(MODULE, 'run_preflight')
    @patch.object(MODULE.subprocess, 'run')
    def test_changed_cloud_config_blocks_installer(self, run, preflight):
        config = self.root / 'cloud.json'
        initial = {'mode': 'production', 'expected_sha': self.sha, 'release_root': str(self.root),
                   'runtime_root': str(self.runtime), 'python': str(self.python)}
        config.write_text(json.dumps(initial))
        def prepared(*args, **kwargs):
            config.write_text('{}')
            return SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='PREPARED', operation_nonce='original-selected-nonce')))
        run.side_effect = prepared
        self.assertEqual(MODULE.main(self.args('prediction-systemd', '--config', str(config))), 2)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][2], 'prepare-target')
        preflight.assert_not_called()

    @patch.object(MODULE, 'run_preflight')
    @patch.object(MODULE.subprocess, 'run')
    def test_prediction_options_are_explicit_and_bound(self, run, preflight):
        run.return_value.returncode = 0
        config = self.root / 'config with spaces.json'
        self.assertEqual(MODULE.main(self.args('prediction-launchd', '--mode', 'shadow',
                                              '--config', str(config), '--n-leg-paused', '1',
                                              '--https-proxy', 'http://127.0.0.1:1082')), 0)
        command = run.call_args.args[0]
        self.assertIn(str(config), command)
        self.assertEqual(command[command.index('--expected-sha') + 1], self.sha)
        self.assertEqual(command[command.index('--n-leg-paused') + 1], '1')
        self.assertEqual(command[command.index('--https-proxy') + 1], 'http://127.0.0.1:1082')

    @patch.object(MODULE.subprocess, 'run')
    def test_preflight_uses_selected_release_python(self, run):
        run.return_value.returncode = 0
        args = SimpleNamespace(release_root=self.root, expected_sha=self.sha,
                               python=self.python, extra=['cloud-ssm'])
        with patch.object(MODULE.sys, 'executable', '/system/python3.9'):
            MODULE.run_preflight(args)
        self.assertEqual(run.call_args.args[0][:2], [str(self.python), '-B'])

    @patch.object(MODULE, 'run_preflight')
    @patch.object(MODULE.subprocess, 'run')
    def test_account_preserves_requested_install_evidence(self, run, preflight):
        run.return_value.returncode = 0
        evidence = self.runtime / 'logs/upgrade.json'
        self.assertEqual(MODULE.main(self.args('account', '--evidence-out', str(evidence))), 0)
        self.assertEqual(run.call_args.args[0][-2:], ['--evidence-out', str(evidence)])

    def test_unsupported_or_conflicting_command_is_rejected(self):
        for tail in [('dashboard', '--mode', 'single'), ('account', '--repo-root', '/other'),
                     ('prediction-systemd', '--config', '/unused', '--action', 'stop'),
                     ('sh', '-c', 'anything')]:
            with self.subTest(tail=tail), self.assertRaises(SystemExit):
                MODULE.main(self.args(*tail))
