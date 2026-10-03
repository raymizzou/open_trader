"""Portable tests for service routing and the fail-closed required check."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci_plan', ROOT / 'scripts/ci_plan.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class RoutingTests(unittest.TestCase):
    def test_every_changed_path_selects_full_backend_with_nleg(self):
        for path in ['README.md', 'README.zh-CN.md', 'docs/operations/ci.md',
                     'AGENTS.md', 'CHANGELOG.md', 'ops/release-deployment.md',
                     'src/open_trader/frontend_gateway.py',
                     'src/open_trader/account_api.py', 'tests/test_account_api.py',
                     'src/open_trader/polymarket_lp.py',
                     'src/open_trader/dashboard_web.py',
                     'src/open_trader/trend_curve_research.py',
                     'tests/test_trend_curve_cli.py',
                     'src/open_trader/prediction_n_leg.py',
                     'src/open_trader/prediction_solver_worker.py',
                     'src/open_trader/models.py', 'uv.lock', 'Makefile',
                     '.github/workflows/ci.yml', 'new_unmapped_module.py',
                     'tests/test_new_family.py', 'unknown\nfile.txt']:
            with self.subTest(path=path):
                plan = ci.route([path])
                self.assertEqual(plan['scopes'], list(ci.REQUIRED_SCOPES))
                self.assertEqual(plan['test_n_leg'], '1')
                self.assertNotIn('exemption', plan['reason'])

    def test_empty_and_mixed_paths_cannot_reduce_coverage(self):
        full = ci.route([])
        self.assertEqual(full['scopes'], list(ci.REQUIRED_SCOPES))
        self.assertEqual(full['test_n_leg'], '1')
        for paths in [['README.md'], ['tests/test_account_api.py',
                      'src/open_trader/frontend_gateway.py', 'README.md']]:
            self.assertEqual(ci.route(paths), full)
            self.assertEqual(ci.route(list(reversed(paths)) + paths), full)

    def test_full_backend_makefile_partition_covers_every_test_once(self):
        # Execute only Make's variable expansion, never a Docker/test recipe.
        with tempfile.NamedTemporaryFile(mode='w', suffix='.mk') as extra:
            extra.write('include Makefile\n.PHONY: ci-partition\nci-partition:\n')
            for scope in ci.SCOPES[:4]:
                extra.write(f'\t@echo {scope}:$(sort $(SERVICE_TESTS_{scope}))\n')
            extra.flush()
            output = subprocess.check_output(['make', '-sf', extra.name, 'ci-partition'],
                                             cwd=ROOT, text=True)
        selected = []
        for line in output.splitlines():
            scope, paths = line.split(':', 1)
            self.assertIn(scope, ci.route([])['scopes'])
            selected.extend(paths.split())
        actual = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / 'tests').glob('test_*.py'))
        self.assertEqual(sorted(selected), actual)
        self.assertEqual(len(selected), len(set(selected)))
        # Legacy already covers the standalone trend-curve tests; do not duplicate them.
        legacy = next(line for line in output.splitlines() if line.startswith('legacy:'))
        for name in ['research', 'backtest', 'cli']:
            self.assertIn(f'tests/test_trend_curve_{name}.py', legacy)

    def test_git_deleted_renamed_and_unusual_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args]).decode().strip()
            git('init', '-q'); git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            p = Path(tmp)
            (p / 'src/open_trader').mkdir(parents=True)
            (p / 'src/open_trader/account_api.py').write_text('old')
            (p / 'src/open_trader/frontend_gateway.py').write_text('delete')
            git('add', '.'); git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            git('mv', 'src/open_trader/account_api.py', 'src/open_trader/prediction_solver.py')
            git('rm', '-q', 'src/open_trader/frontend_gateway.py')
            (p / 'unknown\nfile.txt').write_text('new')
            git('add', '.'); git('commit', '-qm', 'change')
            paths = ci.changed_paths(base, 'HEAD', cwd=tmp)
            self.assertEqual(set(paths), {'src/open_trader/account_api.py',
                             'src/open_trader/prediction_solver.py',
                             'src/open_trader/frontend_gateway.py', 'unknown\nfile.txt'})
            self.assertEqual(ci.route(paths)['test_n_leg'], '1')

    def test_plan_cli_exact_candidate_and_unavailable_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], text=True).strip()
            git('init', '-q'); git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            root = Path(tmp)
            (root / 'README.md').write_text('base')
            git('add', '.'); git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            (root / 'README.md').write_text('docs-only change')
            git('add', '.'); git('commit', '-qm', 'change')
            head = git('rev-parse', 'HEAD')
            for event, before, expected_paths in [
                ('push', base, ['README.md']),
                ('pull_request', base, ['README.md']),
                ('push', '0' * 40, []),
                ('push', '', []),
                ('push', 'f' * 40, []),
                ('push', head, []),
            ]:
                with self.subTest(event=event, before=before):
                    output, summary = root / 'output', root / 'summary'
                    output.write_text(''); summary.write_text('')
                    env = dict(os.environ, GITHUB_EVENT_NAME=event, BASE_SHA=before,
                               GITHUB_SHA=head, GITHUB_OUTPUT=str(output),
                               GITHUB_STEP_SUMMARY=str(summary))
                    run = subprocess.run(['python3', str(ROOT / 'scripts/ci_plan.py'), 'plan'],
                                         cwd=tmp, env=env, text=True, capture_output=True)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    plan = json.loads(run.stdout)
                    self.assertEqual(plan['scopes'], list(ci.REQUIRED_SCOPES))
                    self.assertEqual(plan['test_n_leg'], '1')
                    self.assertEqual(plan['sha'], head)
                    self.assertEqual(plan['base_sha'], before)
                    self.assertEqual(plan['changed_paths'], expected_paths)
                    self.assertEqual(json.loads(output.read_text().removeprefix('plan=')), plan)
                    self.assertIn(head, summary.read_text())
            output.write_text(''); summary.write_text('')
            env['GITHUB_SHA'] = base
            run = subprocess.run(['python3', str(ROOT / 'scripts/ci_plan.py'), 'plan'],
                                 cwd=tmp, env=env, text=True, capture_output=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn('checkout SHA does not match', run.stderr)
            self.assertEqual(output.read_text(), '')


class RequiredTests(unittest.TestCase):
    def needs(self, paths):
        plan = ci.route(paths)
        needs = {'plan': {'result': 'success', 'outputs': {'plan': json.dumps(plan)}}}
        needs.update({scope.replace('-', '_'): {'result': 'success' if scope in plan['scopes'] else 'skipped'} for scope in ci.SCOPES})
        return needs

    def test_full_backend_success_including_documentation_changes(self):
        self.assertTrue(ci.required(self.needs(['src/open_trader/account_api.py']))[0])
        ok, reason = ci.required(self.needs(['docs/operations/ci.md']))
        self.assertTrue(ok)
        self.assertIn('all backend', reason)

    def test_failure_cancellation_skip_missing_fail_closed(self):
        for scope in ci.REQUIRED_SCOPES:
            for result in ['failure', 'cancelled', 'skipped', '', None]:
                needs = self.needs(['README.md'])
                needs[scope]['result'] = result
                self.assertFalse(ci.required(needs)[0], (scope, result))
            needs.pop(scope)
            self.assertFalse(ci.required(needs)[0], scope)

    def test_bad_or_failed_plan_fails_closed(self):
        for result in ['failure', 'cancelled', 'skipped']:
            needs = self.needs(['README.md']); needs['plan']['result'] = result
            self.assertFalse(ci.required(needs)[0])
        for plan in [{}, {'scopes': [], 'docs_only': False}, {'scopes': ['bogus'], 'docs_only': False}]:
            needs = self.needs(['README.md']); needs['plan']['outputs']['plan'] = json.dumps(plan)
            self.assertFalse(ci.required(needs)[0])

    def test_partial_paused_duplicate_and_exempt_plans_fail_closed(self):
        for changes in [{'scopes': []}, {'scopes': ['account']},
                        {'scopes': list(ci.SCOPES)},
                        {'scopes': list(ci.BACKEND_SCOPES)},
                        {'scopes': ['gateway', 'legacy', 'account', 'account']},
                        {'test_n_leg': '0'}, {'test_n_leg': 1}, {'reason': ''}]:
            needs = self.needs(['README.md'])
            plan = json.loads(needs['plan']['outputs']['plan'])
            plan.update(changes)
            needs['plan']['outputs']['plan'] = json.dumps(plan)
            self.assertFalse(ci.required(needs)[0], changes)

    def test_unselected_trend_job_must_be_skipped(self):
        for result in ['success', 'failure', 'cancelled']:
            needs = self.needs(['README.md'])
            needs['trend_curve']['result'] = result
            self.assertFalse(ci.required(needs)[0])

    def test_unexpected_failed_job_is_not_hidden_by_docs(self):
        needs = self.needs(['README.md']); needs['gateway']['result'] = 'failure'
        self.assertFalse(ci.required(needs)[0])

    def test_cli_injected_failure_returns_nonzero(self):
        needs = self.needs(['src/open_trader/account_api.py'])
        needs['account']['result'] = 'failure'
        run = subprocess.run(['python3', str(ROOT / 'scripts/ci_plan.py'), 'required'],
                             input=json.dumps(needs), text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('account', run.stdout)


if __name__ == '__main__':
    unittest.main()
