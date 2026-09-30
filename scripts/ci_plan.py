#!/usr/bin/env python3
"""Deterministic, conservative changed-path routing and fail-closed CI verdict."""
import argparse
import fnmatch
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
import sys

SCOPES = ('gateway', 'legacy', 'account', 'prediction', 'trend-curve')
ACCOUNT = ('account', 'futu_account', 'tiger_account', 'holding_snapshot',
           'statement_import', 'real_holding_input', 'fx', 'cutover_us_tiger_to_futu')
PREDICTION = ('prediction', 'predict', 'polymarket', 'relation', 'lp_',
              'run_nleg', 'mechanical_relations', 'market_scope',
              'problem_canonicalization', 'validation_eat')
# Keep exact-vs-wildcard distinctions aligned with Makefile. Tests check parity.
ACCOUNT_TESTS = ('test_account*.py', 'test_futu_account.py', 'test_tiger_account.py',
                 'test_holding_snapshot*.py', 'test_statement_import.py',
                 'test_real_holding_input.py', 'test_fx.py', 'test_cutover_us_tiger_to_futu.py')
PREDICTION_TESTS = ('test_prediction*.py', 'test_predict*.py', 'test_polymarket*.py',
                    'test_relation*.py', 'test_lp_*.py', 'test_run_nleg*.py',
                    'test_mechanical_relations.py', 'test_market_scope.py',
                    'test_problem_canonicalization.py', 'test_validation_eat*.py')
TREND_TESTS = {'test_trend_curve_research.py', 'test_trend_curve_backtest.py', 'test_trend_curve_cli.py'}
TREND_SOURCES = {'trend_curve_research.py', 'trend_curve_backtest.py'}
NLEG = ('n_leg', 'nleg', 'solver', 'resolver', 'executable_cost', 'market_solution',
        'monitor_selection', 'partial_fill', 'snapshot_scheduler')
# These modules are shared within Prediction, including its omitted N-leg tests.
PREDICTION_SHARED = ('prediction_arbitrage', 'prediction_runtime', 'prediction_service',
                     'prediction_release', 'prediction_read_model', 'prediction_shadow')
# Test ownership is not source ownership: these modules have cross-service consumers.
SHARED_SOURCES = {'market_scope.py', 'fx.py', 'account_http.py', 'account_snapshot.py',
                  'account_sync_state.py', 'futu_account.py', 'tiger_account.py',
                  'dashboard.py', 'dashboard_quotes.py'}
DOC_ROOTS = {'README.md', 'CHANGELOG.md', 'AGENTS.md', 'CONTEXT-MAP.md'}
DOC_SUFFIXES = {'.md', '.rst', '.txt', '.png', '.jpg', '.jpeg', '.svg'}


def is_doc(path):
    return path in DOC_ROOTS or (path.startswith('docs/') and PurePosixPath(path).suffix in DOC_SUFFIXES)


def route(paths):
    paths = sorted(set(paths))
    scopes = set()
    nleg = False
    broad = not paths
    for path in paths:
        if is_doc(path):
            continue
        name = PurePosixPath(path).name
        stem = name.removeprefix('test_')
        is_test = path.startswith('tests/test_') and path.endswith('.py') and '/' not in path[6:]
        is_module = path.startswith('src/open_trader/') and '/' not in path[len('src/open_trader/'):]
        if is_module and name in SHARED_SOURCES:
            broad = True
        elif (is_test and name in TREND_TESTS) or (is_module and name in TREND_SOURCES):
            scopes.add('trend-curve')
        elif (is_test or is_module) and stem.startswith('frontend_gateway'):
            scopes.add('gateway')
        elif (is_test and any(fnmatch.fnmatchcase(name, pattern) for pattern in ACCOUNT_TESTS)) or (is_module and stem.startswith(ACCOUNT)):
            scopes.add('account')
        elif (is_test and any(fnmatch.fnmatchcase(name, pattern) for pattern in PREDICTION_TESTS)) or (is_module and stem.startswith(PREDICTION)) or path.startswith(('scripts/run_nleg', 'benchmarks/prediction_solver/', 'tests/fixtures/prediction')):
            scopes.add('prediction')
            # Prediction source families feed omitted N-leg components; only the
            # isolated LP family retains the ordinary paused-development default.
            nleg |= (any(token in path for token in NLEG)
                     or stem.startswith(PREDICTION_SHARED)
                     or path.startswith('tests/fixtures/')
                     or (is_module and not stem.startswith('polymarket_lp')))
        elif (is_module and stem.startswith('dashboard')) or path.startswith('src/open_trader/dashboard_static/') or is_test:
            scopes.add('legacy')
        else:
            broad = True
    if broad:
        scopes.update(SCOPES[:4])
        nleg = True
    # Broad legacy already includes standalone trend-curve test files.
    if 'legacy' in scopes:
        scopes.discard('trend-curve')
    docs_only = bool(paths) and all(is_doc(path) for path in paths)
    return {'scopes': [scope for scope in SCOPES if scope in scopes],
            'test_n_leg': '1' if nleg else '0', 'docs_only': docs_only,
            'reason': 'documentation-only exemption' if docs_only else
                      'shared, unknown, or unavailable diff: conservative coverage' if broad else
                      'affected service paths'}


def changed_paths(base, head, cwd=None):
    # No rename detection: both old and new paths route; NUL protects odd filenames.
    raw = subprocess.check_output(['git', 'diff', '--name-only', '--no-renames', '-z', base, head, '--'], cwd=cwd)
    return [path.decode('utf-8', errors='surrogateescape') for path in raw.split(b'\0') if path]


def required(needs):
    try:
        if needs['plan']['result'] != 'success':
            return False, 'plan did not succeed'
        plan = json.loads(needs['plan']['outputs']['plan'])
        scopes = plan['scopes']
        docs_only = plan['docs_only']
        if (not isinstance(scopes, list) or len(set(scopes)) != len(scopes)
                or any(scope not in SCOPES for scope in scopes)
                or type(docs_only) is not bool or docs_only != (not scopes)
                or plan['test_n_leg'] not in ('0', '1') or not plan['reason']):
            return False, 'invalid or empty non-exempt plan'
        for scope in SCOPES:
            result = needs[scope.replace('-', '_')]['result']
            expected = 'success' if scope in scopes else 'skipped'
            if result != expected:
                return False, f'{scope}: expected {expected}, got {result}'
        return True, 'documentation-only exemption; routing tests passed' if docs_only else 'all selected service checks succeeded'
    except (KeyError, TypeError, ValueError):
        return False, 'missing or malformed plan/job results'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'required'])
    args = parser.parse_args()
    if args.command == 'required':
        try:
            ok, reason = required(json.load(sys.stdin))
        except ValueError:
            ok, reason = False, 'invalid job-result JSON'
        print(('PASS: ' if ok else 'FAIL: ') + reason)
        return 0 if ok else 1
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    if head != os.environ['GITHUB_SHA']:
        raise SystemExit('checkout SHA does not match event candidate')
    base = os.environ.get('BASE_SHA', '')
    if not re.fullmatch('[0-9a-f]{40}', base) or base == '0' * 40:
        paths = []
    else:
        try:
            paths = changed_paths(base, head)
        except subprocess.CalledProcessError:
            paths = []  # First push/force push/unavailable history cannot claim exemption.
    plan = route(paths)
    plan.update({'sha': head, 'base_sha': base, 'changed_paths': paths})
    encoded = json.dumps(plan, ensure_ascii=True, separators=(',', ':'))
    print(encoded)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write(f'plan={encoded}\n')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
        stream.write(f'## CI routing\nCandidate SHA: `{head}`\n\nBase SHA: `{base}`\n\n'
                     f'Scopes: {", ".join(plan["scopes"]) or "none"}; TEST_N_LEG={plan["test_n_leg"]}\n\n'
                     f'{plan["reason"]}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
