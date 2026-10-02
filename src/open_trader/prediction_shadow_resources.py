"""Fail closed on resource pressure in an explicitly capped Linux Shadow unit."""
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import signal
import threading


HOST_RESERVE = 350 * 1024**2
SERVICE_RESERVE = 64 * 1024**2
logger = logging.getLogger(__name__)


def host_memory(proc=Path('/proc')):
    values = {key: int(value.split()[0]) * 1024
              for key, value in (line.split(':', 1) for line in (proc/'meminfo').read_text().splitlines())}
    return {'available': values['MemAvailable'],
            'swap_used': values['SwapTotal'] - values['SwapFree']}


def require_host_headroom(maximum):
    sample = host_memory()
    if sample['available'] < maximum + HOST_RESERVE or sample['swap_used']:
        raise ValueError('insufficient shared-host memory headroom for Shadow startup')


def read_resources(proc=Path('/proc'), groups=Path('/sys/fs/cgroup')):
    paths = {}
    for line in (proc/'self/cgroup').read_text().splitlines():
        _, controllers, path = line.split(':', 2)
        if not path.startswith('/') or '..' in Path(path).parts:
            raise ValueError('invalid resource controller path')
        for controller in controllers.split(','):
            paths[controller] = path.lstrip('/')
    if '' in paths:
        root = groups/paths['']
        current = int((root/'memory.current').read_text())
        limit = int((root/'memory.max').read_text())
        events = dict(line.split() for line in (root/'memory.events').read_text().splitlines())
        failures = sum(int(events[key]) for key in ('max', 'oom', 'oom_kill'))
        quota, period = map(int, (root/'cpu.max').read_text().split())
        tasks = int((root/'pids.max').read_text())
    else:
        memory = groups/'memory'/paths['memory']
        current = int((memory/'memory.usage_in_bytes').read_text())
        limit = int((memory/'memory.limit_in_bytes').read_text())
        failures = int((memory/'memory.failcnt').read_text())
        cpu_root = groups/'cpu'
        if not cpu_root.exists():
            cpu_root = groups/'cpu,cpuacct'
        cpu = cpu_root/paths['cpu']
        quota = int((cpu/'cpu.cfs_quota_us').read_text())
        period = int((cpu/'cpu.cfs_period_us').read_text())
        tasks = int((groups/'pids'/paths['pids']/'pids.max').read_text())
    if not (0 < limit <= 1_000_000_000 and 0 < quota <= period and 0 < tasks <= 96
            and current >= 0 and failures >= 0):
        raise ValueError('effective kernel resource limits are missing or excessive')
    return {**host_memory(proc), 'current': current, 'limit': limit, 'failures': failures}


def stop_reason(sample, maximum):
    if sample['limit'] > maximum:
        return 'resource_limit_mismatch'
    if sample['failures']:
        return 'cgroup_memory_limit_hit'
    if sample['current'] >= sample['limit'] - SERVICE_RESERVE:
        return 'service_memory_headroom'
    if sample['available'] < HOST_RESERVE:
        return 'host_memory_headroom'
    if sample['swap_used']:
        return 'swap_pressure'
    return None


def watch_resources(done, maximum, failure):
    while not done.wait(1):
        sample = {}
        try:
            sample = read_resources()
            reason = stop_reason(sample, maximum)
        except Exception:
            reason = 'resource_evidence_unknown'
        if done.is_set():
            return
        if reason:
            failure.append(reason)
            logger.error('shadow_resource_guard_stop reason=%s sample=%s', reason, sample)
            # This guard only exists in paused Shadow. Signal this process,
            # never another service, and bound an unresponsive shutdown too.
            os.kill(os.getpid(), signal.SIGTERM)
            if not done.wait(20):
                os.kill(os.getpid(), signal.SIGKILL)
            return


@contextmanager
def shadow_resource_guard(mode):
    raw = os.environ.get('OPEN_TRADER_SHADOW_MEMORY_MAX_BYTES')
    if raw is None:
        yield
        return
    maximum = int(raw)
    if (mode != 'shadow' or os.environ.get('OPEN_TRADER_NLEG_PAUSED') != '1'
            or not SERVICE_RESERVE < maximum <= 1_000_000_000):
        raise ValueError('resource guard requires explicitly capped paused Shadow')
    sample = read_resources()
    reason = stop_reason(sample, maximum)
    if reason or sample['available'] < sample['limit'] - sample['current'] + HOST_RESERVE:
        raise ValueError(reason or 'insufficient startup memory headroom')
    logger.info('shadow_resource_guard_verified sample=%s', sample)
    done = threading.Event()
    failure = []
    worker = threading.Thread(target=watch_resources, args=(done, maximum, failure),
                              name='prediction-shadow-resources', daemon=True)
    worker.start()
    try:
        yield
    finally:
        done.set()
        worker.join(timeout=2)
    if failure:
        raise RuntimeError('Shadow stopped by resource protection')
