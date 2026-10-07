#!/usr/bin/env python3
"""Small, exact-run test evidence consumed by the deployment preflight."""
import argparse
import hashlib
import math
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

BACKEND_SCOPES = ('gateway', 'legacy', 'account', 'prediction')
MARKER = 'not pressure and not browser'


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


POLICY = 'permanent-nleg-retirement-v1'
RETIREMENT_REASON = 'N-leg permanently retired'
# Reviewed exact allowlist. Names alone never retire a future test.
RETIRED_PATHS = (
    'tests/test_prediction_executable_cost.py',
    'tests/test_prediction_live_resolver.py',
    'tests/test_prediction_market_solution.py',
    'tests/test_prediction_monitor_selection.py',
    'tests/test_prediction_n_leg_canary_report.py',
    'tests/test_prediction_n_leg_confirm.py',
    'tests/test_prediction_n_leg_driver.py',
    'tests/test_prediction_n_leg_episodes.py',
    'tests/test_prediction_n_leg_execution.py',
    'tests/test_prediction_n_leg_fail_closed_e2e.py',
    'tests/test_prediction_n_leg_lineage_inheritance.py',
    'tests/test_prediction_n_leg_metrics.py',
    'tests/test_prediction_n_leg_mode.py',
    'tests/test_prediction_n_leg_oracle.py',
    'tests/test_prediction_n_leg_preflight.py',
    'tests/test_prediction_n_leg_read_model.py',
    'tests/test_prediction_n_leg_shadow.py',
    'tests/test_prediction_n_leg_terminal_check.py',
    'tests/test_prediction_n_leg_validation.py',
    'tests/test_prediction_n_leg_validation_books.py',
    'tests/test_prediction_partial_fill.py',
    'tests/test_prediction_snapshot_scheduler.py',
    'tests/test_prediction_solver.py',
    'tests/test_prediction_solver_backends.py',
    'tests/test_prediction_solver_benchmark.py',
    'tests/test_prediction_solver_server.py',
    'tests/test_prediction_solver_verified.py',
    'tests/test_prediction_solver_worker.py',
    'tests/test_run_nleg_no_submit_validation.py',
)


def retirement_policy(root, manifest=None):
    path = Path(manifest) if manifest else Path(root) / 'scripts/ci_nleg_retired.json'
    data = json.loads(path.read_text())
    if (set(data) != {'schema_version', 'policy', 'retired'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or data['policy'] != POLICY or not isinstance(data['retired'], list)):
        raise ValueError('invalid retirement manifest policy')
    paths = []
    for item in data['retired']:
        if (not isinstance(item, dict) or set(item) != {'path', 'reason'}
                or item['reason'] != RETIREMENT_REASON or item['path'] not in RETIRED_PATHS):
            raise ValueError('unreviewed retirement entry')
        test_path = Path(root) / item['path']
        if not test_path.is_file() or test_path.is_symlink() or not test_path.resolve().is_relative_to(Path(root).resolve()):
            raise ValueError('missing or unsafe retired test file')
        paths.append(item['path'])
    if paths != list(RETIRED_PATHS):
        raise ValueError('missing, unsorted or duplicate retirement entry')
    return {'policy': data['policy'], 'manifest_sha256': file_sha256(path),
            'retirement_reason': RETIREMENT_REASON,
            'retired_files': [item['path'] for item in data['retired']]}


def service_partitions(root, nleg='0', manifest=None):
    import fnmatch
    if nleg not in ('0', '1'):
        raise ValueError('TEST_N_LEG must be 0 or 1')
    retired = retirement_policy(root, manifest)['retired_files']
    patterns = {
        'gateway': ('test_frontend_gateway*.py',),
        'account': ('test_account*.py', 'test_futu_account.py', 'test_tiger_account.py',
                    'test_holding_snapshot*.py', 'test_statement_import.py', 'test_real_holding_input.py',
                    'test_fx.py', 'test_cutover_us_tiger_to_futu.py'),
        'prediction': ('test_prediction*.py', 'test_predict*.py', 'test_polymarket*.py',
                       'test_relation*.py', 'test_lp_*.py', 'test_run_nleg*.py',
                       'test_mechanical_relations.py', 'test_market_scope.py',
                       'test_problem_canonicalization.py', 'test_validation_eat*.py'),
    }
    parts = {scope: [] for scope in BACKEND_SCOPES}
    for path in sorted((Path(root) / 'tests').rglob('test_*.py')):
        relative = path.relative_to(root).as_posix()
        if nleg == '0' and relative in retired:
            continue
        scopes = [scope for scope, globs in patterns.items()
                  if any(fnmatch.fnmatchcase(path.name, glob) for glob in globs)]
        if len(scopes) > 1:
            raise ValueError('overlapping service file: ' + relative)
        parts[scopes[0] if scopes else 'legacy'].append(relative)
    if any(not paths for paths in parts.values()):
        raise ValueError('empty service partition')
    return parts


def expected_selection(scope, root):
    if scope == 'portable':
        roots = ['acceptance/test_prediction_arbitrage_scenarios.py']
    elif scope == 'trend-curve':
        roots = ['tests/test_trend_curve_research.py', 'tests/test_trend_curve_backtest.py',
                 'tests/test_trend_curve_cli.py']
    elif scope in BACKEND_SCOPES:
        roots = service_partitions(root)[scope]
    else:
        raise ValueError('unknown CI scope')
    return {'roots': roots, 'marker': MARKER, 'keyword': 'not LIVE' if scope == 'portable' else ''}


def selection_sha256(selection):
    return hashlib.sha256(json.dumps(selection, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_partition(root, proof):
    import importlib.util
    spec = importlib.util.spec_from_file_location('evidence_partition', Path(__file__).with_name('ci_partition.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = module.verify_partition(proof['complete'], proof['partitions'], proof['retired'], root)
    if proof != expected:
        raise ValueError('inconsistent active/retired partition proof')
    return proof


def finite_duration(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def validate_metrics(metrics, nodes, sha, workers, successful=True):
    if (not isinstance(metrics, dict) or type(metrics.get('schema_version')) is not int
            or metrics['schema_version'] != 1 or metrics.get('source_sha') != sha
            or type(metrics.get('workers')) is not int or metrics['workers'] != workers
            or metrics.get('selected_nodeids') != nodes or not isinstance(nodes, list)
            or any(not isinstance(node, str) or '::' not in node for node in nodes)
            or len(nodes) != len(set(nodes)) or not finite_duration(metrics.get('wall_seconds'))):
        raise ValueError('invalid test metrics identity/selection')
    if not successful:
        return metrics  # Partial failure evidence remains truthful, not successful coverage.
    results = metrics.get('results')
    if (not nodes or type(metrics.get('exit_status')) is not int or metrics['exit_status'] != 0
            or metrics.get('complete') is not True or metrics.get('collection_errors') != []
            or not isinstance(results, list) or len(results) != len(nodes)
            or any(not isinstance(item, dict) or not isinstance(item.get('nodeid'), str) for item in results)
            or {item.get('nodeid') for item in results} != set(nodes)):
        raise ValueError('incomplete or failed test execution')
    if any(not nonempty_text(metrics.get(key)) for key in ('python', 'platform', 'architecture')):
        raise ValueError('missing Python/platform/architecture measurement')
    cpu_keys = ('cpu_model', 'cpu_model_source', 'cpu_model_unavailable', 'cpu_count')
    if any(key not in metrics for key in cpu_keys):
        raise ValueError('missing CPU identity measurement')
    model, source, unavailable = (metrics[key] for key in cpu_keys[:3])
    if model is None:
        if source != 'unavailable' or not nonempty_text(unavailable):
            raise ValueError('ambiguous unavailable CPU identity')
    elif (not nonempty_text(model) or not nonempty_text(source)
          or source == 'unavailable' or unavailable is not None):
        raise ValueError('ambiguous native CPU identity')
    count = metrics['cpu_count']
    if count is not None and (type(count) is not int or count <= 0):
        raise ValueError('invalid CPU count measurement')
    allowed_workers = {'serial'} if workers == 1 else {'gw0', 'gw1'} if workers == 2 else set()
    executions = []
    for result in results:
        node, execution = result['nodeid'], result.get('execution_nodeid')
        if (not isinstance(execution, str) or not (execution == node or
                (workers == 2 and execution.startswith(node + '@') and len(execution) > len(node) + 1))):
            raise ValueError('missing or inconsistent canonical/execution identity')
        executions.append(execution)
        phases = result.get('phases')
        if not isinstance(phases, dict) or any(key not in ('setup', 'call', 'teardown') for key in phases):
            raise ValueError('invalid phase measurements')
        for phase in phases.values():
            if (not isinstance(phase, dict) or not finite_duration(phase.get('duration'))
                    or not isinstance(phase.get('worker'), str) or phase['worker'] not in allowed_workers
                    or phase.get('outcome') not in ('passed', 'skipped')):
                raise ValueError('invalid duration, outcome or phase worker')
        if len({phase['worker'] for phase in phases.values()}) != 1:
            raise ValueError('inconsistent phase worker identity')
        outcomes = {key: phase['outcome'] for key, phase in phases.items()}
        passed = {'setup':'passed', 'call':'passed', 'teardown':'passed'}
        setup_skip = {'setup':'skipped', 'teardown':'passed'}
        call_skip = {'setup':'passed', 'call':'skipped', 'teardown':'passed'}
        if not ((result.get('outcome') == 'passed' and outcomes == passed) or
                (result.get('outcome') == 'skipped' and outcomes in (setup_skip, call_skip))):
            raise ValueError('incomplete or inconsistent successful phase/outcome record')
    if len(executions) != len(set(executions)):
        raise ValueError('duplicate execution identities')
    return metrics


def junit_identity(execution_nodeid):
    # Pytest's XML address consists of a dotted file/class path and the exact
    # test name plus parameter text. Use the explicitly recorded execution ID;
    # never guess which @ characters are parameters or group decoration.
    head, bracket, parameters = execution_nodeid.partition('[')
    parts = head.split('::')
    file = parts[0].replace('/', '.')
    if file.endswith('.py'):
        file = file[:-3]
    return '.'.join([file, *parts[1:-1]]), parts[-1] + bracket + parameters


def validate_junit(data, metrics):
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, TypeError, ValueError) as error:
        raise ValueError('malformed JUnit XML') from error
    if root.tag == 'testsuite':
        suites = [root]
    elif root.tag == 'testsuites':
        suites = list(root.findall('testsuite'))
    else:
        raise ValueError('unsupported JUnit root')
    expected = {junit_identity(result['execution_nodeid']): result['outcome'] for result in metrics['results']}
    if len(expected) != len(metrics['results']) or not expected or not suites:
        raise ValueError('empty or ambiguous JUnit execution identities')
    seen = {}
    for suite in suites:
        cases = suite.findall('testcase')
        outcomes = {'passed':0, 'skipped':0, 'failed':0, 'error':0}
        for case in cases:
            identity = (case.get('classname'), case.get('name'))
            if identity not in expected or identity in seen:
                raise ValueError('missing, extra or duplicate JUnit testcase identity')
            try:
                duration = float(case.attrib['time'])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError('missing or invalid JUnit duration') from error
            if not finite_duration(duration):
                raise ValueError('nonfinite or negative JUnit duration')
            statuses = [tag for tag in ('skipped', 'failure', 'error') for child in case.findall(tag)]
            if len(statuses) > 1:
                raise ValueError('ambiguous JUnit testcase outcome')
            outcome = {'failure':'failed'}.get(statuses[0], statuses[0]) if statuses else 'passed'
            if expected[identity] != outcome:
                raise ValueError('JUnit outcome disagrees with phase-derived result')
            seen[identity] = outcome
            outcomes[outcome] += 1
        for key, expected_count in [('tests',len(cases)), ('failures',outcomes['failed']),
                                    ('errors',outcomes['error']), ('skipped',outcomes['skipped'])]:
            value = suite.get(key)
            if not isinstance(value, str) or not value.isdecimal() or int(value) != expected_count:
                raise ValueError('JUnit suite counts disagree with testcase records')
    if set(seen) != set(expected) or len(root.findall('.//testcase')) != len(seen):
        raise ValueError('JUnit does not cover every selected execution')
    return metrics


def build_evidence(root, directory, scope, nleg, workers, status, env):
    if nleg != '0' or workers != (2 if scope == 'prediction' else 1):
        raise ValueError('CI requires active retirement policy and the declared serial/loadgroup policy')
    for key, pattern in [('GITHUB_SHA', '[0-9a-f]{40}'), ('GITHUB_RUN_ID', '[1-9][0-9]*'),
                         ('GITHUB_RUN_ATTEMPT', '[1-9][0-9]*')]:
        if not re.fullmatch(pattern, env.get(key, '')):
            raise ValueError('missing or invalid ' + key)
    for key in ('GITHUB_REPOSITORY', 'GITHUB_WORKFLOW_REF', 'GITHUB_EVENT_NAME', 'GITHUB_REF'):
        if not env.get(key):
            raise ValueError('missing ' + key)
    environment = {'runner_os': env.get('RUNNER_OS', ''), 'runner_arch': env.get('RUNNER_ARCH', '')}
    if status == 0 and any(not nonempty_text(value) for value in environment.values()):
        raise ValueError('successful CI requires runner OS/architecture')
    for field, filename in [('dependency_manifest_sha256', 'dependency-manifest.json'),
                            ('image_inspect_sha256', 'image.json'),
                            ('metrics_sha256', 'metrics.json'), ('selected_nodeids_sha256', 'selected-nodeids.json'),
                            ('junit_sha256', 'junit.xml')]:
        path = Path(directory) / filename
        environment[field] = file_sha256(path) if path.is_file() else None
    if scope == 'portable':
        path = Path(directory) / 'partition.json'
        environment['partition_sha256'] = file_sha256(path) if path.is_file() else None
    # Missing evidence can never describe a successful run, even if invoked incorrectly.
    if status == 0 and any(value is None for value in environment.values()):
        raise ValueError('successful CI requires complete environment/partition evidence')
    selection = expected_selection(scope, root)
    metrics_path = Path(directory) / 'metrics.json'
    nodes_path = Path(directory) / 'selected-nodeids.json'
    nodes = json.loads(nodes_path.read_text()) if nodes_path.is_file() else []
    if metrics_path.is_file() and nodes_path.is_file():
        metrics = validate_metrics(json.loads(metrics_path.read_text()), nodes, env['GITHUB_SHA'], workers, status == 0)
        if status == 0:
            validate_junit((Path(directory) / 'junit.xml').read_bytes(), metrics)
        if any(node.split('::', 1)[0] not in selection['roots'] for node in nodes):
            raise ValueError('test nodeid outside declared selection')
    if status == 0 and scope == 'portable':
        validate_partition(root, json.loads((Path(directory) / 'partition.json').read_text()))
    return {
        'schema_version': 2, 'evidence_role': 'test-only', 'source_sha': env['GITHUB_SHA'],
        'repository': env['GITHUB_REPOSITORY'], 'workflow_ref': env['GITHUB_WORKFLOW_REF'],
        'event_name': env['GITHUB_EVENT_NAME'], 'ref': env['GITHUB_REF'],
        'run_id': env['GITHUB_RUN_ID'], 'run_attempt': env['GITHUB_RUN_ATTEMPT'],
        'scope': scope, 'test_n_leg': nleg, 'workers': workers,
        'lock_sha256': file_sha256(Path(root) / 'uv.lock'),
        'status': 'success' if status == 0 else 'failure', 'exit_status': status,
        'selection': selection, 'selection_sha256': selection_sha256(selection),
        'policy': retirement_policy(root), 'selected_nodeids': nodes, 'environment': environment,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    if len(os.sys.argv) > 1 and os.sys.argv[1] == 'select':
        parser.add_argument('command')
        parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
        parser.add_argument('--manifest', type=Path)
        parser.add_argument('--nleg', default='0')
        parser.add_argument('--services', nargs='*')
        args = parser.parse_args()
        parts = service_partitions(args.root, args.nleg, args.manifest)
        if args.services:
            if any(scope not in parts for scope in args.services):
                raise ValueError('Unknown SERVICE')
            print(' '.join(sorted({path for scope in args.services for path in parts[scope]})))
        else:
            for scope, paths in parts.items():
                print(scope + ':' + ' '.join(paths))
        return
    parser.add_argument('scope')
    parser.add_argument('nleg')
    parser.add_argument('workers', type=int)
    parser.add_argument('status', type=int)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    evidence = build_evidence(root, args.directory, args.scope, args.nleg, args.workers, args.status, os.environ)
    (args.directory / 'evidence.json').write_text(json.dumps(evidence, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
