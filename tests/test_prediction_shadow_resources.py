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
    for maximum in (True, 0, -1, 67108864):
        with pytest.raises(ValueError):
            render_unit(replace(cfg, memory_max_bytes=maximum))
    production = render_unit(replace(cfg, mode='production', region='region',
                                     secret='secret', version='v1', role='role'))
    assert 'Restart=on-failure\n' in production and 'SendSIGKILL=no\n' in production
    assert 'OPEN_TRADER_SHADOW_MEMORY_MAX_BYTES' not in production


def test_shadow_unit_accepts_configured_two_gib_budget():
    cfg = CloudConfig(Path('/opt/release'), Path('/var/lib/prediction'),
                      Path('/opt/venv/bin/python'), 'prediction', 'a' * 40,
                      '', '', '', '', 'shadow', 1, memory_max_bytes=2147483648)
    unit = render_unit(cfg)
    assert 'MemoryMax=2147483648\n' in unit
    assert 'OPEN_TRADER_SHADOW_MEMORY_MAX_BYTES=2147483648\n' in unit
    assert 'CPUQuota=100%\n' in unit and 'TasksMax=96\n' in unit
    assert 'Restart=no\n' in unit


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_cgroup_reader_accepts_configured_two_gib_budget(tmp_path, version):
    from open_trader.prediction_shadow_resources import read_resources, stop_reason
    proc, groups = tmp_path/'proc', tmp_path/'cgroups'
    (proc/'self').mkdir(parents=True)
    (proc/'meminfo').write_text('MemAvailable: 3145728 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
    if version == 'v2':
        (proc/'self/cgroup').write_text('0::/system.slice/probe.service\n')
        controllers = {'system.slice': {'probe.service/memory.current':'536870912',
                                        'probe.service/memory.max':'2147483648',
                                        'probe.service/memory.events':'max 0\noom 0\noom_kill 0\n',
                                        'probe.service/cpu.max':'100000 100000',
                                        'probe.service/pids.max':'96'}}
    else:
        (proc/'self/cgroup').write_text('7:memory:/probe\n6:cpu,cpuacct:/probe\n5:pids:/probe\n')
        controllers = {'memory': {'probe/memory.usage_in_bytes':'536870912',
                                  'probe/memory.limit_in_bytes':'2147483648',
                                  'probe/memory.failcnt':'0'},
                       'cpu,cpuacct': {'probe/cpu.cfs_quota_us':'100000',
                                      'probe/cpu.cfs_period_us':'100000'},
                       'pids': {'probe/pids.max':'96'}}
    for controller, files in controllers.items():
        for name, value in files.items():
            path = groups/controller/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
    sample = read_resources(proc, groups)
    assert sample == dict(current=536870912, limit=2147483648, available=3221225472,
                          swap_used=0, failures=0)
    assert stop_reason(sample, 2147483648) is None


def test_shadow_guard_accepts_configured_two_gib_with_host_headroom(monkeypatch):
    from open_trader.prediction_shadow_resources import require_host_headroom, shadow_resource_guard
    monkeypatch.setenv('OPEN_TRADER_SHADOW_MEMORY_MAX_BYTES', '2147483648')
    monkeypatch.setenv('OPEN_TRADER_NLEG_PAUSED', '1')
    files = {'/proc/self/cgroup': '0::/system.slice/probe.service\n',
             '/proc/meminfo': 'MemAvailable: 3145728 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n',
             '/sys/fs/cgroup/system.slice/probe.service/memory.current': '536870912',
             '/sys/fs/cgroup/system.slice/probe.service/memory.max': '2147483648',
             '/sys/fs/cgroup/system.slice/probe.service/memory.events': 'max 0\noom 0\noom_kill 0\n',
             '/sys/fs/cgroup/system.slice/probe.service/cpu.max': '100000 100000',
             '/sys/fs/cgroup/system.slice/probe.service/pids.max': '96'}
    read_text = Path.read_text

    def read_fixture(path, *args, **kwargs):
        if str(path) in files:
            return files[str(path)]
        if path.is_relative_to('/proc') or path.is_relative_to('/sys/fs/cgroup'):
            raise AssertionError(f'unexpected host counter read: {path}')
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_fixture)
    require_host_headroom(2147483648)
    entered = False
    with shadow_resource_guard('shadow'):
        entered = True
    assert entered


@pytest.mark.parametrize(('field', 'value', 'reason'), [
    ('limit', 2147487744, 'resource_limit_mismatch'),
    ('limit', 9223372036854771712, 'resource_limit_mismatch'),
    ('current', 2080374784, 'service_memory_headroom'),
    ('available', 367001599, 'host_memory_headroom'),
    ('swap_used', 4096, 'swap_pressure'),
    ('failures', 1, 'cgroup_memory_limit_hit'),
    ('current', 2080374783, None),
])
def test_two_gib_budget_preserves_resource_guards(field, value, reason):
    from open_trader.prediction_shadow_resources import stop_reason
    sample = dict(current=536870912, limit=2147483648, available=3221225472,
                  swap_used=0, failures=0)
    assert stop_reason({**sample, field:value}, 2147483648) == reason


@pytest.mark.parametrize(('available_kib', 'swap_used_kib', 'accepted'), [
    (2455552, 0, True),  # 2514485248 bytes: 2GiB + 350MiB.
    (2455551, 0, False),  # 2514484224 bytes: 1KiB short.
    (2455552, 4, False),  # Full reserve, but 4096 bytes of used swap.
])
def test_two_gib_startup_requires_full_host_reserve(monkeypatch, available_kib,
                                                   swap_used_kib, accepted):
    from open_trader.prediction_shadow_resources import require_host_headroom
    read_text = Path.read_text

    def read_fixture(path, *args, **kwargs):
        if path == Path('/proc/meminfo'):
            return (f'MemAvailable: {available_kib} kB\n'
                    f'SwapTotal: {swap_used_kib} kB\nSwapFree: 0 kB\n')
        if path.is_relative_to('/proc') or path.is_relative_to('/sys/fs/cgroup'):
            raise AssertionError(f'unexpected host counter read: {path}')
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_fixture)
    if accepted:
        require_host_headroom(2147483648)
    else:
        with pytest.raises(ValueError, match='insufficient shared-host memory headroom'):
            require_host_headroom(2147483648)


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
