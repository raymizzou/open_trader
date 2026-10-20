"""Diagnostic failures remain observable without changing trading authority."""
from datetime import UTC, datetime
import json
import threading

import pytest

from tests.test_lp_d2_execution_bridge import rotation_runtime, wait_rotation_idle


def test_rotation_diagnostics_serializes_sdk_expiration(tmp_path, monkeypatch):
    live, execution, sdk, ids, pools, order, qualify = rotation_runtime(tmp_path, monkeypatch)
    expiration = datetime(2026, 10, 19, 1, 2, 3, tzinfo=UTC)
    try:
        sdk.orders = tuple(order(k).model_copy(update=dict(order_type='GTD', expires_at=expiration))
            for k in 'ABCDEF')
        result = execution.lp_auto_scheduled_check()
        for outcome in result.rotation_outcomes:
            if outcome.handle:
                outcome.handle.result(timeout=3)
        wait_rotation_idle(live)
        victim, = result.rotation.victims
        assert victim.expiration.value == expiration
        before = (sdk.balance_reads, sdk.order_reads, sdk.position_reads, list(sdk.posts), list(sdk.cancels))
        diagnostic = live.rotation_diagnostics()
        assert diagnostic['victims']['samples'][0]['expiration'] == '2026-10-19T01:02:03+00:00'
        assert json.loads(json.dumps(diagnostic)) == diagnostic
        diagnostic['victims']['samples'][0]['expiration'] = 'mutated'
        assert live.rotation_diagnostics()['victims']['samples'][0]['expiration'] == '2026-10-19T01:02:03+00:00'
        assert (sdk.balance_reads, sdk.order_reads, sdk.position_reads, sdk.posts, sdk.cancels) == before
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_rotation_diagnostics_serializes_value_types_and_isolates_unknown(tmp_path, monkeypatch, caplog):
    from datetime import date
    from decimal import Decimal
    from enum import Enum

    class State(Enum):
        UNKNOWN = 'UNKNOWN'

    live, execution, sdk, ids, pools, order, qualify = rotation_runtime(tmp_path, monkeypatch)
    try:
        live._rotation_summary = dict(day=date(2026, 10, 19), amount=Decimal('1.2300'), state=State.UNKNOWN)
        assert live.rotation_diagnostics() == dict(day='2026-10-19', amount='1.2300', state='UNKNOWN')
        live._rotation_summary = dict(unsupported=object())
        result = live.rotation_diagnostics()
        assert result['selection_state'] == 'UNKNOWN' and result['available'] is False
        assert result['error'] == 'diagnostics_unavailable'
        assert 'prediction_lp_rotation_diagnostics_failed' in caplog.text
        assert json.loads(json.dumps(result)) == result
        assert sdk.posts == [] and sdk.cancels == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('diagnostic_name', ['rotation_diagnostics', 'diagnostics'])
@pytest.mark.parametrize('failure', ['raise', 'unsupported'])
def test_auto_state_and_dashboard_isolate_diagnostic_failure(tmp_path, monkeypatch, caplog, diagnostic_name, failure):
    from tests.test_lp_auto_control import runtime_for
    from tests.test_prediction_service import _server, _response

    live, execution, sdk, ids, pools, order, qualify = rotation_runtime(tmp_path, monkeypatch, startup=False)
    runtime = runtime_for(tmp_path / 'http')
    runtime.execution = execution
    runtime._lp_auto_scheduler = None
    baseline = execution.lp_auto_state(include_intents=False)
    def unavailable():
        if failure == 'raise':
            raise TypeError('diagnostic fault sentinel')
        return dict(unsupported=object())
    monkeypatch.setattr(live, diagnostic_name, unavailable)
    monkeypatch.setattr(execution, 'lp_dashboard', lambda: dict(state='ready', authenticated=True))
    try:
        state = execution.lp_auto_state(include_intents=False)
        projection = state['diagnostics']['rotation'] if diagnostic_name == 'rotation_diagnostics' else state['diagnostics']
        assert projection['available'] is False and projection['error'] == 'diagnostics_unavailable'
        assert projection['selection_state' if diagnostic_name == 'rotation_diagnostics' else 'state'] == 'UNKNOWN'
        assert {k: v for k, v in state.items() if k != 'diagnostics'} == {k: v for k, v in baseline.items() if k != 'diagnostics'}
        assert state['new_capacity_usd'] is None  # no financial observation was invented
        with _server(runtime) as base:
            status, response = _response(base + '/api/prediction-arbitrage/lp/auto/state')
            assert status == 200 and response['diagnostics'] == state['diagnostics']
            status, response = _response(base + '/api/prediction-arbitrage/lp/dashboard')
            assert status == 200 and response['auto']['diagnostics'] == state['diagnostics']
        assert 'prediction_lp_diagnostics_failed' in caplog.text
        assert sdk.posts == [] and sdk.cancels == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


class ObservedWake(threading.Event):
    """Observe an actual monitor wait; test clocks never replace watchdog time."""
    def __init__(self):
        super().__init__()
        from threading import Event
        self.waiting = Event()
        self.delays = []

    def wait(self, timeout=None):
        self.delays.append(timeout)
        self.waiting.set()
        return super().wait(timeout)


@pytest.mark.parametrize('failure_site', ['before', 'finally', 'loop'])
def test_scheduler_state_failure_backs_off_and_recovers(monkeypatch, caplog, failure_site):
    from datetime import timedelta
    from threading import Event
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler

    now = datetime(2026, 10, 19, tzinfo=UTC)
    broken = [False]
    calls = []
    recovered, release = Event(), Event()
    class Execution:
        def lp_auto_state(self, **kwargs):
            if broken[0] and failure_site != 'loop':
                raise TypeError('state unreadable')
            return dict(api_retry_interval_seconds=17)

        def lp_auto_run_once(self):
            calls.append(now)
            if failure_site == 'finally' and len(calls) == 1:
                broken[0] = True
            else:
                recovered.set()
                assert release.wait(3), 'recovered check watchdog'

    scheduler = LPAutoScheduler(Execution(), clock=lambda: now)
    observed = ObservedWake()
    scheduler._wake = observed
    if failure_site == 'loop':
        run_due = scheduler.run_due
        def faulted_run_due():
            if broken[0]:
                raise TypeError('unexpected monitor fault')
            return run_due()
        monkeypatch.setattr(scheduler, 'run_due', faulted_run_due)
    broken[0] = failure_site != 'finally'
    scheduler.start()
    try:
        assert observed.waiting.wait(3), 'monitor did not survive to retry wait'
        snapshot = scheduler.snapshot()
        assert snapshot['scheduler_running'] is True
        assert snapshot['check_in_progress'] is False
        assert snapshot['last_check_error'] == 'TypeError'
        assert snapshot['next_check_at'] == '2026-10-19T00:00:17+00:00'
        assert observed.delays[0] == 17
        assert len(calls) == int(failure_site == 'finally')
        # A wake cannot turn unreadable state into permission for another check.
        scheduler.request_check()
        if failure_site != 'loop':
            assert scheduler.run_due() is False
        assert len(calls) == int(failure_site == 'finally')
        assert 'prediction_lp_auto_' in caplog.text and 'failed' in caplog.text
        broken[0] = False
        now += timedelta(seconds=17)
        scheduler.request_check()
        assert recovered.wait(3), 'monitor did not recover after retry'
    finally:
        release.set()
        scheduler.stop()
    assert scheduler.snapshot()['scheduler_running'] is False


@pytest.mark.parametrize('failure', ['read', 'deadline'])
def test_scheduler_survives_initial_state_failure(caplog, failure):
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler

    now = datetime(2026, 10, 19, tzinfo=UTC)
    calls = []
    class Execution:
        def lp_auto_state(self, **kwargs):
            if failure == 'read':
                raise TypeError('state unreadable at construction')
            return dict(api_retry_interval_seconds=17, plan_wait=dict(kind='dependencies', deadline='invalid'))

        def lp_auto_run_once(self):
            calls.append(True)

    scheduler = LPAutoScheduler(Execution(), clock=lambda: now)
    observed = ObservedWake()
    scheduler._wake = observed
    scheduler.start()
    try:
        assert observed.waiting.wait(3), 'initial state failure killed monitor'
        assert scheduler.snapshot()['scheduler_running'] is True
        assert scheduler.snapshot()['last_check_error'] == ('TypeError' if failure == 'read' else 'ValueError')
        assert observed.delays[0] == (60 if failure == 'read' else 17)
        assert calls == []
        assert 'prediction_lp_auto_' in caplog.text and 'failed' in caplog.text
    finally:
        scheduler.stop()
