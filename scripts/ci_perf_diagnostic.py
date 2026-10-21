#!/usr/bin/env python3
"""One diagnostic execution; never trusted CI/deployment evidence.

Observer phase bounds use a retained time.monotonic_ns callable, before tests
can patch clocks. Linux CLOCK_MONOTONIC shares an epoch across these same-host
processes. Bounds include pytest hook overhead, not just test code. Files are
flushed locally per event; controller receipt/serialization latency is not used
as execution time. No cross-host clock alignment is claimed. Subtest reports
are outcomes inside a parent call, not additional phase occupancy.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

MONOTONIC_NS = time.monotonic_ns
SOURCE_SHA = '0ad6e77fd0dbbc937dfc3e660bc7ae37d9ae89b1'
WORKERS = 6
LIMITS = (__doc__ + '\nResource samples are discrete observations, not exact peaks. '
          'Summed phase time and active interval union are not CPU utilization. '
          'Idle gaps include scheduling, fixture and observer overhead. '
          'A killed process can leave an unmatched phase_start or partial final line.')


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def pytest_configure(config):
    """Loaded only when explicitly selected with -p ci_perf_diagnostic."""
    directory = os.environ.get('CI_PERF_OBSERVATIONS')
    if not directory:
        return
    import pytest
    from _pytest.subtests import SubtestReport

    class Observer:
        def __init__(self):
            self.worker = getattr(config, 'workerinput', {}).get('workerid', 'controller')
            self.originals = {}
            self.bounds = {}
            Path(directory).mkdir(parents=True, exist_ok=True)
            self.stream = (Path(directory) / f'{self.worker}-{os.getpid()}.jsonl').open('a', buffering=1)
            self.emit('start', pid=os.getpid(), clock='CLOCK_MONOTONIC',
                      clock_resolution=time.get_clock_info('monotonic').resolution)

        def emit(self, event, **fields):
            try:
                self.stream.write(json.dumps(dict(event=event, worker=self.worker,
                                                  time_ns=MONOTONIC_NS(), **fields)) + '\n')
            except OSError as exc:
                # Observation must never change an assertion or pytest outcome.
                print(f'diagnostic observation write failed: {exc}', file=sys.stderr)

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_collection_modifyitems(self, items):
            originals = {id(item): item.nodeid for item in items}
            yield
            self.originals = {item.nodeid: originals[id(item)] for item in items}

        def pytest_collection_finish(self, session):
            self.emit('collection', nodeids=[self.originals.get(item.nodeid, item.nodeid)
                                             for item in session.items])

        def phase(self, item, when):
            start = MONOTONIC_NS()
            self.emit('phase_start', nodeid=self.originals.get(item.nodeid, item.nodeid),
                      execution_nodeid=item.nodeid, when=when, start_ns=start)
            yield
            self.bounds[item.nodeid, when] = (start, MONOTONIC_NS())

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_setup(self, item):
            yield from self.phase(item, 'setup')

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_call(self, item):
            yield from self.phase(item, 'call')

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_teardown(self, item):
            yield from self.phase(item, 'teardown')

        @pytest.hookimpl(hookwrapper=True, trylast=True)
        def pytest_runtest_makereport(self, item, call):
            result = yield
            report = result.get_result()
            bounds = self.bounds.pop((item.nodeid, report.when), None)
            # unittest asks for nested reports while the parent call hook is
            # still running. Its SubtestReport follows via logreport; only a
            # completed outer phase has our independent interval bounds.
            if bounds is None:
                return
            start, end = bounds
            self.emit('phase', nodeid=self.originals.get(item.nodeid, item.nodeid),
                      execution_nodeid=item.nodeid, when=report.when,
                      outcome=report.outcome, start_ns=start, end_ns=end)

        def pytest_runtest_logreport(self, report):
            if isinstance(report, SubtestReport):
                self.emit('subtest', nodeid=self.originals.get(report.nodeid, report.nodeid),
                          execution_nodeid=report.nodeid, when=report.when,
                          outcome=report.outcome, context=str(report.context))

        @pytest.hookimpl(optionalhook=True)
        def pytest_testnodedown(self, node, error):
            self.emit('worker_down', worker_id=node.gateway.id, error=str(error) if error else None)

        def pytest_sessionfinish(self, session, exitstatus):
            self.emit('finish', exit_status=int(exitstatus))
            self.stream.close()

    config.pluginmanager.register(Observer(), 'six-worker-diagnostic-observer')


def summarize(directory, exit_status, expected_workers=WORKERS):
    """Read partial streams without discarding their usable prefix."""
    events, errors = [], []
    for path in sorted(Path(directory).glob('*.jsonl')):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            try:
                events.append(json.loads(line))
            except (ValueError, TypeError) as exc:
                errors.append(f'{path.name}:{number}: {exc}')
    workers = {f'gw{i}': [] for i in range(expected_workers)}
    for event in events:
        if event['worker'] in workers:
            workers[event['worker']].append(event)
    phases = [e for e in events if e['event'] == 'phase' and e['worker'] in workers]
    first = min((e['start_ns'] for e in phases), default=None)
    last = max((e['end_ns'] for e in phases), default=None)
    selected = None
    report = {}
    complete = exit_status in (0, 1) and not errors
    controller = [e for e in events if e['worker'] == 'controller']
    complete &= sum(e['event'] == 'start' for e in controller) == 1
    complete &= any(e['event'] == 'finish' and e['exit_status'] == exit_status for e in controller)
    complete &= not any(e['event'] == 'worker_down' and e['error'] for e in controller)
    executed = []
    for worker, stream in workers.items():
        starts = [e for e in stream if e['event'] == 'start']
        finishes = [e for e in stream if e['event'] == 'finish']
        collections = [e['nodeids'] for e in stream if e['event'] == 'collection']
        worker_complete = len(starts) == len(finishes) == len(collections) == 1
        worker_complete &= all(e['exit_status'] in (0, 1) for e in finishes)
        if collections:
            if selected is None:
                selected = collections[0]
            worker_complete &= collections == [selected]
        rows = sorted((e for e in stream if e['event'] == 'phase'), key=lambda e: e['start_ns'])
        nodes = defaultdict(dict)
        totals = dict(setup=0.0, call=0.0, teardown=0.0)
        union, gaps, end = 0, [], None
        for row in rows:
            nodes[row['nodeid']][row['when']] = row
            totals[row['when']] += (row['end_ns'] - row['start_ns']) / 1e9
            if end is None:
                union += row['end_ns'] - row['start_ns']
            else:
                if row['start_ns'] > end:
                    gaps.append((row['start_ns'] - end) / 1e9)
                union += max(0, row['end_ns'] - max(end, row['start_ns']))
            end = max(end or 0, row['end_ns'])
        key = lambda e: (e['nodeid'], e['when'], e['start_ns'])
        worker_complete &= Counter(map(key, rows)) == Counter(key(e) for e in stream if e['event'] == 'phase_start')
        worker_complete &= len({(e['nodeid'], e['when']) for e in rows}) == len(rows)
        for node, parts in nodes.items():
            worker_complete &= 'setup' in parts and 'teardown' in parts
            if parts.get('setup', {}).get('outcome') == 'passed':
                worker_complete &= 'call' in parts
            executed.append(node)
        durations = {node: sum((p['end_ns'] - p['start_ns']) / 1e9 for p in parts.values())
                     for node, parts in nodes.items()}
        complete &= worker_complete
        report[worker] = dict(node_count=len(nodes), phase_seconds=totals,
                              active_union_seconds=union / 1e9, idle_gaps_seconds=gaps,
                              first_ns=rows[0]['start_ns'] if rows else None, last_ns=end,
                              idle_after_last_phase_seconds=(last-end)/1e9 if end is not None else None,
                              completion_seconds_from_first_phase=(end-first)/1e9 if end is not None else None,
                              final_node=rows[-1]['nodeid'] if rows else None,
                              longest_test=max(durations, key=durations.get) if durations else None,
                              longest_test_seconds=max(durations.values(), default=None),
                              complete=bool(worker_complete), phases=rows)
    complete &= selected is not None and sorted(executed) == sorted(selected)
    return dict(diagnostic_only=True, complete=bool(complete), exit_status=exit_status,
                expected_workers=expected_workers, selected_nodeids=sorted(selected or []),
                observed_nodeids=sorted(executed), first_ns=first, last_ns=last,
                phase_span_seconds=(last-first)/1e9 if first is not None else None,
                last_outstanding=sorted(phases, key=lambda e: e['end_ns'], reverse=True)[:6],
                workers=report, stream_errors=errors, limits=LIMITS)


def render_timeline(summary, output):
    """Six lanes of original node identities; raw nanosecond bounds stay in JSON."""
    import html
    first = summary['first_ns'] or 0
    span = max(1, (summary['last_ns'] or first) - first)
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="260" viewBox="0 0 1200 260">',
           '<rect width="1200" height="260" fill="white"/>',
           '<text x="10" y="20">Diagnostic phase occupancy (not CPU); hover for node / phase</text>']
    colors = {'setup': '#7c9fce', 'call': '#296d49', 'teardown': '#d99a38'}
    for index, (worker, row) in enumerate(summary['workers'].items()):
        y = 40 + index * 32
        svg.append(f'<text x="10" y="{y+16}">{worker}</text>')
        for phase in row['phases']:
            x = 70 + (phase['start_ns']-first)/span*1110
            width = max(.2, (phase['end_ns']-phase['start_ns'])/span*1110)
            title = html.escape(f"{phase['nodeid']} {phase['when']} {phase['outcome']}")
            svg.append(f'<rect x="{x}" y="{y}" width="{width}" height="22" fill="{colors[phase["when"]]}"><title>{title}</title></rect>')
    svg.append('</svg>')
    Path(output).write_text('\n'.join(svg))


def read_available(path):
    try:
        return Path(path).read_text()
    except OSError as exc:
        return f'unavailable: {exc}'


def inside():
    """Runs in the offline container; all injected files/evidence are in /tmp."""
    output = Path('/tmp/prediction-six-diagnostic')
    output.mkdir()
    observations = output / 'observations'
    env = {**os.environ, 'CI_PERF_OBSERVATIONS': str(observations),
           'CI_TEST_METRICS': str(output / 'metrics.json'), 'CI_TEST_SOURCE_SHA': SOURCE_SHA,
           'CI_TEST_WORKERS': str(WORKERS), 'PYTHONSAFEPATH': '1',
           'PYTHONPATH': '/tmp:/workspace:/workspace/src', 'PYTHONDONTWRITEBYTECODE': '',
           'PYTHONPYCACHEPREFIX': '/tmp/open-trader-bytecache', 'TEST_N_LEG': '0'}
    def status():
        return subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all'], text=True)
    before = status()
    write_json(output / 'runtime.json', dict(python=sys.version, platform=platform.platform(),
               cpu_count=os.cpu_count(), cpuinfo=read_available('/proc/cpuinfo'),
               cpu_max=read_available('/sys/fs/cgroup/cpu.max'),
               memory_max=read_available('/sys/fs/cgroup/memory.max'),
               cpuset=read_available('/sys/fs/cgroup/cpuset.cpus.effective'),
               source_sha=os.environ.get('OPEN_TRADER_TEST_SOURCE_SHA'),
               source_state=os.environ.get('OPEN_TRADER_TEST_SOURCE_STATE'), source_status_before=before))
    if (before or os.environ.get('OPEN_TRADER_TEST_SOURCE_SHA') != SOURCE_SHA
            or os.environ.get('OPEN_TRADER_TEST_SOURCE_STATE') != 'clean'):
        raise RuntimeError('measured container source identity/cleanliness mismatch')
    subprocess.run(['python', 'scripts/dev_dependency_manifest.py'], stdout=(output / 'dependency-manifest.json').open('w'), check=True)
    selection_command = ['python', 'scripts/ci_evidence.py', 'select', '--manifest',
                         'scripts/ci_nleg_retired.json', '--nleg', '0', '--services', 'prediction']
    selected = subprocess.check_output(selection_command, text=True).split()
    write_json(output / 'selected-files.json', selected)
    command = ['pytest', '-q', '-p', 'scripts.ci_test_metrics', '-p', 'ci_perf_diagnostic',
               f'--junitxml={output}/junit.xml', '-m', 'not pressure and not browser',
               '-o', 'cache_dir=/tmp/open-trader-pytest-cache', '--basetemp=/tmp/open-trader-pytest',
               *selected, '-n', '6', '--dist=loadgroup']
    write_json(output / 'command.json', dict(selection=selection_command, pytest=command,
                                           environment={k: env[k] for k in env if k.startswith(('CI_', 'PYTHON', 'TEST_'))}))
    result = subprocess.run(command, env=env).returncode
    # Later diagnostic failures must not mask the original pytest failure.
    try:
        after = status()
        summary = summarize(observations, result)
        write_json(output / 'observation.json', summary)
        render_timeline(summary, output / 'timeline.svg')
        write_json(output / 'result.json', dict(test_exit_status=result, source_status_after=after,
                                               observation_complete=summary['complete']))
        return result or (0 if summary['complete'] and not after else 1)
    except Exception:
        if result:
            return result
        raise


def run(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.is_relative_to(source) or Path(__file__).resolve().is_relative_to(source):
        raise ValueError('harness and evidence must be outside measured source')
    output.mkdir(parents=True, exist_ok=False)
    commands = (output / 'commands.jsonl').open('a', buffering=1)
    def execute(command, log, check=True):
        start = MONOTONIC_NS()
        with (output / log).open('a') as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
        end = MONOTONIC_NS()
        commands.write(json.dumps(dict(command=command, exit_status=result.returncode, log=log,
                                       start_ns=start, end_ns=end, wall_seconds=(end-start)/1e9))+'\n')
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command)
        return result.returncode
    def git(*args):
        return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()
    if git('rev-parse', 'HEAD') != SOURCE_SHA or git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('expected exact clean measured source')
    digest = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
    write_json(output / 'identity.json', dict(diagnostic_only=True, measured_source_sha=SOURCE_SHA,
               harness_sha=os.environ.get('GITHUB_SHA'), lock_sha256=digest(source/'uv.lock'),
               dockerfile_sha256=digest(source/'Dockerfile.dev'),
               injected_observer_path='/tmp/ci_perf_diagnostic.py', observer_sha256=digest(__file__),
               limits=LIMITS))
    tag = f'open-trader-diagnostic-six:{os.getpid()}'
    container = f'open-trader-diagnostic-six-{os.getpid()}'
    created, result, stop, monitor = False, 0, threading.Event(), None
    cleanup_errors = []
    def sample():
        with (output / 'resource-samples.jsonl').open('a', buffering=1) as stream:
            while not stop.is_set():
                try:
                    stats = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{json .}}', container],
                                           capture_output=True, text=True, timeout=8)
                    stream.write(json.dumps(dict(sampled=True, monotonic_ns=MONOTONIC_NS(),
                        docker_stats=stats.stdout, stats_exit=stats.returncode, stats_error=stats.stderr,
                        host_proc_stat=read_available('/proc/stat'), host_meminfo=read_available('/proc/meminfo'),
                        host_loadavg=read_available('/proc/loadavg'))) + '\n')
                except (OSError, subprocess.TimeoutExpired) as exc:
                    stream.write(json.dumps(dict(sampled=True, error=str(exc)))+'\n')
                stop.wait(5)
    try:
        execute(['docker', 'build', '--build-arg', f'SOURCE_SHA={SOURCE_SHA}', '--build-arg', 'SOURCE_STATE=clean',
                 '--target', 'dev', '--file', str(source/'Dockerfile.dev'), '--tag', tag, str(source)], 'build.log')
        execute(['docker', 'image', 'inspect', tag], 'image.json')
        execute(['docker', 'create', '--name', container, '--init', '--network', 'none', '--cap-drop', 'ALL',
                 '--security-opt', 'no-new-privileges', tag, 'python', '/tmp/ci_perf_diagnostic.py', 'inside'], 'create.log')
        created = True
        execute(['docker', 'cp', str(Path(__file__).resolve()), f'{container}:/tmp/ci_perf_diagnostic.py'], 'inject.log')
        monitor = threading.Thread(target=sample, name='diagnostic-resource-sampler')
        monitor.start()
        result = execute(['docker', 'start', '-a', container], 'test.log', check=False)
    except subprocess.CalledProcessError as exc:
        result = exc.returncode
    except Exception as exc:
        write_json(output / 'runner-error.json', dict(error=str(exc)))
        result = result or 1
    finally:
        stop.set()
        if monitor is not None:
            monitor.join()  # stats subprocess is bounded; no orphan sampling handles.
        if created:
            for command, log in [(['docker', 'inspect', container], 'container.json'),
                                 (['docker', 'cp', f'{container}:/tmp/prediction-six-diagnostic/.', str(output)], 'copy.log'),
                                 (['docker', 'rm', '-f', container], 'cleanup.log')]:
                try:
                    code = execute(command, log, check=False)
                    result = result or code
                except OSError as exc:
                    cleanup_errors.append(f'{log}: {exc}')
                    result = result or 1
        try:
            clean = not git('status', '--porcelain', '--untracked-files=all')
        except (OSError, subprocess.CalledProcessError) as exc:
            clean = None
            cleanup_errors.append(f'source cleanliness unknown: {exc}')
        result = result or (0 if clean else 1)
        try:
            write_json(output / 'runner-result.json', dict(exit_status=result, source_clean_after=clean,
                                                          cleanup_errors=cleanup_errors))
            commands.close()
        except OSError as exc:
            print(f'diagnostic final evidence failed: {exc}', file=sys.stderr)
            result = result or 1
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    launch = sub.add_parser('run')
    launch.add_argument('--source', required=True)
    launch.add_argument('--output', required=True)
    sub.add_parser('inside')
    args = parser.parse_args()
    raise SystemExit(run(args.source, args.output) if args.mode == 'run' else inside())
