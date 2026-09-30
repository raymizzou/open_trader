"""Portable tests for service routing and the fail-closed required check."""
import importlib.util
import json
import fnmatch
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci_plan', ROOT / 'scripts/ci_plan.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class RoutingTests(unittest.TestCase):
    def test_docs_only_and_empty_are_distinct(self):
        plan = ci.route(['README.md', 'docs/operations/ci.md'])
        self.assertTrue(plan['docs_only'])
        self.assertEqual(plan['scopes'], [])
        self.assertFalse(ci.route([])['docs_only'])
        self.assertEqual(ci.route([])['scopes'], list(ci.SCOPES[:4]))

    def test_service_routes(self):
        for path, scope in [('src/open_trader/frontend_gateway.py', 'gateway'),
                            ('src/open_trader/account_api.py', 'account'),
                            ('tests/test_account_snapshot.py', 'account'),
                            ('src/open_trader/polymarket_lp.py', 'prediction'),
                            ('src/open_trader/dashboard_web.py', 'legacy'),
                            ('tests/test_dashboard_web.py', 'legacy')]:
            with self.subTest(path=path):
                plan = ci.route([path])
                self.assertEqual(plan['scopes'], [scope])
                self.assertEqual(plan['test_n_leg'], '0')

    def test_shared_unknown_and_dependency_changes_expand(self):
        for path in ['src/open_trader/models.py', 'uv.lock', 'Makefile',
                     'new_unmapped_module.py', 'docs/tool.py', '.github/workflows/ci.yml',
                     'src/open_trader/market_scope.py', 'src/open_trader/fx.py',
                     'src/open_trader/account_http.py', 'src/open_trader/account_sync_state.py',
                     'src/open_trader/account_snapshot.py', 'src/open_trader/futu_account.py',
                     'src/open_trader/tiger_account.py', 'src/open_trader/dashboard.py',
                     'src/open_trader/dashboard_quotes.py']:
            with self.subTest(path=path):
                self.assertEqual(ci.route([path])['scopes'], list(ci.SCOPES[:4]))
                self.assertEqual(ci.route([path])['test_n_leg'], '1')

    def test_nleg_solver_resolver_and_fixtures_restore_coverage(self):
        for path in ['src/open_trader/prediction_n_leg.py',
                     'src/open_trader/prediction_solver_worker.py',
                     'src/open_trader/prediction_live_resolver.py',
                     'src/open_trader/relation_catalog_v2.py',
                     'src/open_trader/polymarket_trading.py',
                     'src/open_trader/prediction_observation_monitor.py',
                     'tests/test_prediction_snapshot_scheduler.py',
                     'scripts/run_nleg_no_submit_validation.py',
                     'benchmarks/prediction_solver/case.json',
                     'tests/fixtures/prediction_n_leg_validation_frozen_n3.json']:
            with self.subTest(path=path):
                plan = ci.route([path])
                self.assertIn('prediction', plan['scopes'])
                self.assertEqual(plan['test_n_leg'], '1')

    def test_standalone_trend_curve_uses_dedicated_gate(self):
        for path in ['src/open_trader/trend_curve_research.py',
                     'tests/test_trend_curve_cli.py']:
            self.assertEqual(ci.route([path])['scopes'], ['trend-curve'])

    def test_new_test_names_do_not_fall_between_makefile_selections(self):
        for path in ['tests/test_futu_account_new.py', 'tests/test_tiger_account_new.py',
                     'tests/test_statement_import_new.py', 'tests/test_fx_new.py',
                     'tests/test_mechanical_relations_new.py',
                     'tests/test_problem_canonicalization_new.py', 'tests/test_trend_curve_new.py']:
            self.assertEqual(ci.route([path])['scopes'], ['legacy'], path)

    def test_routing_matches_makefile_test_partition(self):
        makefile = (ROOT / 'Makefile').read_text()
        patterns = {}
        for scope in ['gateway', 'account', 'prediction']:
            line = next(line for line in makefile.splitlines() if line.startswith(f'SERVICE_TESTS_{scope} := '))
            patterns[scope] = re.search(r'\$\(wildcard (.*?)\)', line).group(1).split()
        for path in sorted((ROOT / 'tests').glob('test_*.py')):
            name = path.relative_to(ROOT).as_posix()
            expected = next((scope for scope, globs in patterns.items()
                             if any(fnmatch.fnmatchcase(name, glob) for glob in globs)), 'legacy')
            actual = ci.route([name])['scopes']
            if actual == ['trend-curve']:
                self.assertIn(name, ['tests/test_trend_curve_research.py',
                                    'tests/test_trend_curve_backtest.py', 'tests/test_trend_curve_cli.py'])
            else:
                self.assertEqual(actual, [expected], name)

    def test_mixed_paths_have_stable_deduplicated_order(self):
        paths = ['tests/test_account_api.py', 'src/open_trader/frontend_gateway.py',
                 'README.md', 'src/open_trader/account_api.py']
        self.assertEqual(ci.route(paths), ci.route(list(reversed(paths))))
        self.assertEqual(ci.route(paths)['scopes'], ['gateway', 'account'])

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


class RequiredTests(unittest.TestCase):
    def needs(self, paths):
        plan = ci.route(paths)
        needs = {'plan': {'result': 'success', 'outputs': {'plan': json.dumps(plan)}}}
        needs.update({scope.replace('-', '_'): {'result': 'success' if scope in plan['scopes'] else 'skipped'} for scope in ci.SCOPES})
        return needs

    def test_selected_success_and_explained_docs_exemption(self):
        self.assertTrue(ci.required(self.needs(['src/open_trader/account_api.py']))[0])
        ok, reason = ci.required(self.needs(['docs/operations/ci.md']))
        self.assertTrue(ok)
        self.assertIn('documentation', reason)

    def test_failure_cancellation_skip_missing_fail_closed(self):
        for result in ['failure', 'cancelled', 'skipped', '', None]:
            needs = self.needs(['src/open_trader/account_api.py'])
            needs['account']['result'] = result
            self.assertFalse(ci.required(needs)[0])
        needs.pop('account')
        self.assertFalse(ci.required(needs)[0])

    def test_bad_or_failed_plan_fails_closed(self):
        for result in ['failure', 'cancelled', 'skipped']:
            needs = self.needs(['README.md']); needs['plan']['result'] = result
            self.assertFalse(ci.required(needs)[0])
        for plan in [{}, {'scopes': [], 'docs_only': False}, {'scopes': ['bogus'], 'docs_only': False}]:
            needs = self.needs(['README.md']); needs['plan']['outputs']['plan'] = json.dumps(plan)
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
