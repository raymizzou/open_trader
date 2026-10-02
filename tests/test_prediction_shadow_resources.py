from dataclasses import replace
from pathlib import Path

import pytest

from open_trader.prediction_cloud import CloudConfig, render_unit


def test_shadow_unit_bounds_all_children_without_automatic_restart():
    cfg = CloudConfig(Path('/opt/release'), Path('/var/lib/prediction'),
                      Path('/opt/venv/bin/python'), 'prediction', 'a' * 40,
                      '', '', '', '', 'shadow', 1)
    unit = render_unit(cfg)
    assert 'MemoryAccounting=yes\n' in unit
    assert 'MemoryMax=805306368\n' in unit
    assert 'CPUQuota=100%\n' in unit and 'TasksMax=96\n' in unit
    assert 'Restart=no\n' in unit and 'SendSIGKILL=yes\n' in unit
    assert 'OPEN_TRADER_SHADOW_MEMORY_MAX_BYTES=805306368\n' in unit
    for maximum in (True, 0, -1, 1_000_000_001):
        with pytest.raises(ValueError):
            render_unit(replace(cfg, memory_max_bytes=maximum))
    production = render_unit(replace(cfg, mode='production', region='region',
                                     secret='secret', version='v1', role='role'))
    assert 'Restart=on-failure\n' in production and 'SendSIGKILL=no\n' in production
    assert 'OPEN_TRADER_SHADOW_MEMORY_MAX_BYTES' not in production


def test_cgroup_resource_checks_fail_closed_and_keep_host_headroom(tmp_path):
    from open_trader.prediction_shadow_resources import read_resources, stop_reason
    proc, groups = tmp_path/'proc', tmp_path/'cgroups'
    (proc/'self').mkdir(parents=True)
    (proc/'self/cgroup').write_text('0::/system.slice/probe.service\n')
    (proc/'meminfo').write_text('MemAvailable: 1000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
    group = groups/'system.slice/probe.service'
    group.mkdir(parents=True)
    for name, value in {'memory.current':'200000000', 'memory.max':'805306368',
                        'memory.events':'max 0\noom 0\noom_kill 0\n',
                        'cpu.max':'100000 100000', 'pids.max':'96'}.items():
        (group/name).write_text(value)
    sample = read_resources(proc, groups)
    assert stop_reason(sample, 805306368) is None
    assert stop_reason({**sample, 'current':800000000}, 805306368) == 'service_memory_headroom'
    assert stop_reason({**sample, 'available':300*1024**2}, 805306368) == 'host_memory_headroom'
    assert stop_reason({**sample, 'swap_used':4096}, 805306368) == 'swap_pressure'
    assert stop_reason({**sample, 'failures':1}, 805306368) == 'cgroup_memory_limit_hit'
    (group/'memory.max').write_text('max')
    with pytest.raises(ValueError):
        read_resources(proc, groups)


def test_legacy_cgroup_checks_real_limits_and_rejects_uncapped_cpu(tmp_path):
    from open_trader.prediction_shadow_resources import read_resources
    proc, groups = tmp_path/'proc', tmp_path/'cgroups'
    (proc/'self').mkdir(parents=True)
    (proc/'self/cgroup').write_text('7:memory:/probe\n6:cpu,cpuacct:/probe\n5:pids:/probe\n')
    (proc/'meminfo').write_text('MemAvailable: 1000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
    values = {'memory': {'memory.usage_in_bytes':'200000000', 'memory.limit_in_bytes':'805306368',
                         'memory.failcnt':'0'},
              'cpu,cpuacct': {'cpu.cfs_quota_us':'100000', 'cpu.cfs_period_us':'100000'},
              'pids': {'pids.max':'96'}}
    for controller, files in values.items():
        root = groups/controller/'probe'
        root.mkdir(parents=True)
        for name, value in files.items():
            (root/name).write_text(value)
    assert read_resources(proc, groups)['limit'] == 805306368
    (groups/'cpu,cpuacct/probe/cpu.cfs_quota_us').write_text('-1')
    with pytest.raises(ValueError):
        read_resources(proc, groups)


@pytest.mark.parametrize('unknown', [None, OSError, RuntimeError])
def test_guard_stops_only_its_own_process_and_bounds_shutdown(monkeypatch, unknown):
    import signal
    import open_trader.prediction_shadow_resources as guard
    from types import SimpleNamespace
    signals, waits, failure = [], [], []

    def sample():
        if unknown:
            raise unknown('resource counters unavailable')
        return dict(current=800000000, limit=805306368, available=1000000000,
                    swap_used=0, failures=0)

    monkeypatch.setattr(guard, 'read_resources', sample)
    monkeypatch.setattr(guard.os, 'kill', lambda pid, sig: signals.append((pid, sig)))
    done = SimpleNamespace(wait=lambda seconds: waits.append(seconds) or False, is_set=lambda: False)
    guard.watch_resources(done, 805306368, failure)
    assert failure == ['resource_evidence_unknown' if unknown else 'service_memory_headroom']
    assert signals == [(guard.os.getpid(), signal.SIGTERM), (guard.os.getpid(), signal.SIGKILL)]
    assert waits == [1, 20]


def test_guard_requires_explicit_shadow_scope_and_valid_prestart_evidence(monkeypatch):
    import open_trader.prediction_shadow_resources as guard
    monkeypatch.setenv('OPEN_TRADER_SHADOW_MEMORY_MAX_BYTES', '805306368')
    monkeypatch.setenv('OPEN_TRADER_NLEG_PAUSED', '1')
    with pytest.raises(ValueError, match='paused Shadow'):
        with guard.shadow_resource_guard('production'):
            pytest.fail('production entered a Shadow stop policy')
    monkeypatch.setattr(guard, 'read_resources', lambda: dict(
        current=100000000, limit=805306368, available=400*1024**2, swap_used=0, failures=0))
    with pytest.raises(ValueError, match='startup memory'):
        with guard.shadow_resource_guard('shadow'):
            pytest.fail('runtime started without host headroom')
