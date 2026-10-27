"""Low-lane recovery at HTTPX response ownership and public runtime seams."""
import asyncio
from dataclasses import replace
import threading

import httpx
import pytest

from test_lp_async_source_contract import independent_watchdog, book, reward
from test_lp_async_read_runtime_contract import Boundary, Clock, collect, demand
from test_lp_financial_read_recovery import FinancialBoundary, set_time
from open_trader.polymarket_lp_async_source import LPAsyncSourcePort
from open_trader.polymarket_lp_read_runtime import LPReadRuntime


class LowBoundary(FinancialBoundary):
    def __init__(self, clock):
        super().__init__(clock)
        self.held_orders = {'L'}
        self.held_once = set()
        self.low_requests = []
        self.low_clients = []
        self.low_ports = []
        self.low_cooling = None
        self.low_close_entered = threading.Event()
        self.low_close_release = threading.Event()
        self.low_close_exited = threading.Event()

    def factory(self, d):
        if d.kind in ('financial', 'buy', 'exact'):
            return super().factory(d)
        boundary = self

        class HeldBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.Event().wait()
                yield b''

            async def aclose(self):
                boundary.low_close_entered.set()
                await asyncio.get_running_loop().run_in_executor(None, boundary.low_close_release.wait)
                boundary.low_close_exited.set()

        async def http(request):
            source, order = request.headers['x-lp-source'], request.headers['x-lp-order']
            boundary.low_requests.append((order, source, boundary.clock()))
            boundary.record('entered', order, source)
            if order in boundary.held_orders and order not in boundary.held_once:
                boundary.held_once.add(order)
                return httpx.Response(200, stream=HeldBody())
            if order == boundary.low_cooling:
                boundary.low_cooling = None
                return httpx.Response(429, headers={'Retry-After': '20'})
            value = dict(book(), market=d.condition_id, asset_id=d.token_id,
                timestamp=str(int(boundary.clock.utc().timestamp()*1000)))
            if source == 'native_rewards':
                value = dict(data=[dict(reward(), condition_id=d.condition_id)], next_cursor='LTE=')
            boundary.record('finally_exited', order, source)
            return httpx.Response(200, json=value)

        clients = {name: httpx.AsyncClient(transport=httpx.MockTransport(http),
            base_url='http://127.0.0.1', trust_env=False) for name in ('gamma', 'clob')}
        self.low_clients.extend(clients.values())
        port = LPAsyncSourcePort(clients, clock=self.clock, utc_now=self.clock.utc)
        self.low_ports.append(port)
        return port


def low(kind='sell', order='L', **kwargs):
    return demand(order, kind=kind, sources=('book',), **kwargs)


def scope_for(runtime, order='L'):
    return next(s for s in runtime.status()['scopes'] if s['key'][4] == order)


@pytest.mark.parametrize('kind', ['sell', 'position', 'holding', 'income'])
def test_low_cleanup_completion_restores_existing_reader(kind):
    clock = Clock()
    boundary = LowBoundary(clock)
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    try:
        original, peer = low(kind), low(kind, 'peer')
        runtime.reconcile((original, peer))
        boundary.wait(lambda: boundary.low_requests == [('L', 'book', 0)])
        first = scope_for(runtime)['attempt']
        set_time(runtime, boundary, clock, 5)
        assert boundary.low_close_entered.wait(4)
        expired, = collect(runtime, boundary, bool)
        assert expired.attempt == first and expired.reason == 'timeout' and not expired.complete
        old_id = expired.sources['book'].source_id
        set_time(runtime, boundary, clock, 6)
        fault, = collect(runtime, boundary, bool)
        assert fault.attempt == first and fault.cleanup_fault and not fault.complete
        assert runtime.drain_results() == ()
        assert scope_for(runtime)['active'] and runtime.status()['cleanup_faults'] == 1
        assert not runtime.status()['new_buy_allowed']
        set_time(runtime, boundary, clock, 7)
        assert boundary.low_requests == [('L', 'book', 0)]
        assert not boundary.low_close_exited.is_set()
        boundary.low_close_release.set()
        assert boundary.low_close_exited.wait(4)
        boundary.wait(lambda: not scope_for(runtime)['active'])
        assert runtime.status()['cleanup_faults'] == 0
        assert runtime.status()['new_buy_allowed']
        expected = {'L', 'peer'} if kind in ('holding', 'income') else {'peer'}
        resumed = collect(runtime, boundary, lambda rows: expected <= {r.demand.order_id for r in rows})
        assert all(r.complete for r in resumed)
        assert not expired.complete and expired.reason == 'timeout'  # late close never repairs old facts
        cadence = {'sell': 30, 'position': 60}.get(kind, 5)
        if cadence > 7:
            assert not any(r.demand.order_id == 'L' for r in resumed)
            set_time(runtime, boundary, clock, cadence-0.001)
            assert sum(order == 'L' for order, _, _ in boundary.low_requests) == 1
            set_time(runtime, boundary, clock, cadence)
            resumed = collect(runtime, boundary, lambda rows: any(r.demand.order_id == 'L' for r in rows))
        fresh = next(r for r in resumed if r.demand.order_id == 'L')
        assert fresh.complete and fresh.attempt > first and fresh.started == max(7, cadence)
        assert fresh.sources['book'].source_id != old_id
        assert fresh.sources['book'].checked_at == clock.utc()
        assert len(boundary.low_ports) == 1 and len(boundary.low_clients) == 2
    finally:
        boundary.low_close_release.set()
        runtime.reconcile(())
        assert runtime.close(4)
        assert all(c.is_closed for c in boundary.low_clients)


def test_low_recovery_preserves_mailbox_and_provider_cooldown():
    clock = Clock()
    boundary = LowBoundary(clock)
    boundary.low_cooling = 'L'
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    original = replace(low('holding'), sources=('book', 'native_rewards'), observation_id='old-ticket')
    try:
        runtime.reconcile((original,))
        boundary.wait(lambda: len(boundary.low_requests) == 2 and runtime.status()['managed_tasks'] == 2)
        first = scope_for(runtime)['attempt']
        set_time(runtime, boundary, clock, 5)
        assert boundary.low_close_entered.wait(4)
        set_time(runtime, boundary, clock, 6)
        assert runtime.status()['cleanup_faults'] == 1
        set_time(runtime, boundary, clock, 7)
        boundary.low_close_release.set()
        assert boundary.low_close_exited.wait(4)
        boundary.wait(lambda: not scope_for(runtime)['active'])
        assert runtime.status()['cleanup_faults'] == 0 and runtime.status()['ready_count'] == 1
        runtime.reconcile((replace(original, observation_id='new-ticket'),))
        set_time(runtime, boundary, clock, 19)
        assert len(boundary.low_requests) == 2 and scope_for(runtime)['attempt'] == first
        expired, = runtime.drain_results()
        assert expired.demand.observation_id == 'old-ticket' and expired.attempt == first
        assert not expired.complete and expired.reason == 'timeout' and expired.cleanup_fault
        assert expired.sources['native_rewards'].retry_at == 20
        assert runtime.drain_results() == ()
        boundary.wait(lambda: runtime.status()['next_dependency_monotonic'] == 20)
        set_time(runtime, boundary, clock, 20)
        fresh, = collect(runtime, boundary, bool)
        assert fresh.attempt > first and fresh.demand.observation_id == 'new-ticket'
        assert fresh.complete and fresh.started == 20
        assert len(boundary.low_requests) == 4 and len(boundary.low_ports) == 1
        assert runtime.drain_results() == ()

        # An unconsumed fault-only notice must also retain the old identity.
        boundary.held_once.clear()
        boundary.low_close_release.clear()
        boundary.low_close_entered.clear()
        boundary.low_close_exited.clear()
        runtime.reconcile((low('holding', observation_id='second-old-ticket'),))
        set_time(runtime, boundary, clock, 25)
        boundary.wait(lambda: len(boundary.low_requests) == 5)
        second = scope_for(runtime)['attempt']
        set_time(runtime, boundary, clock, 30)
        assert boundary.low_close_entered.wait(4)
        timeout, = runtime.drain_results()
        assert timeout.attempt == second and timeout.reason == 'timeout' and not timeout.cleanup_fault
        set_time(runtime, boundary, clock, 31)
        assert runtime.status()['cleanup_faults'] == 1
        runtime.reconcile((low('holding', observation_id='second-new-ticket'),))
        set_time(runtime, boundary, clock, 32)
        boundary.low_close_release.set()
        assert boundary.low_close_exited.wait(4)
        boundary.wait(lambda: not scope_for(runtime)['active'])
        # Supervisor observed completion without replacing the unread mailbox.
        set_time(runtime, boundary, clock, 33)
        assert len(boundary.low_requests) == 5 and scope_for(runtime)['attempt'] == second
        fault, = runtime.drain_results()
        assert fault.reason == 'cleanup_fault' and fault.cleanup_fault and not fault.complete
        assert fault.attempt == second and fault.demand.observation_id == 'second-old-ticket'
        recovered, = collect(runtime, boundary, bool)
        assert recovered.complete and recovered.attempt > second
        assert recovered.demand.observation_id == 'second-new-ticket'
        assert runtime.drain_results() == () and len(boundary.low_ports) == 1
    finally:
        boundary.low_close_release.set()
        runtime.reconcile(())
        assert runtime.close(4)


def _verify_port_close_latch():
    """A failing external transport is isolated because failed close stays owned."""
    class FailedClose(httpx.MockTransport):
        async def aclose(self):
            raise OSError('private transport close failure')

    clock = Clock()
    boundary = Boundary(clock)
    async def http(request):
        return httpx.Response(200, json=book())
    def factory(d):
        return LPAsyncSourcePort({'clob': httpx.AsyncClient(transport=FailedClose(http),
            base_url='http://127.0.0.1', trust_env=False)}, clock=clock, utc_now=clock.utc)
    runtime = LPReadRuntime(factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    runtime.reconcile((low(),))
    result, = collect(runtime, boundary, bool)
    assert result.complete
    assert runtime.close(.02) is False
    boundary.wait(lambda: runtime.status()['cleanup_faults'] == 1)
    before = runtime.status()
    assert not before['new_buy_allowed'] and before['pool_count'] == 1
    detail, = before['cleanup_fault_details']['items']
    assert dict(detail) == dict(lane='low', kind='sell', since=clock.utc().isoformat(),
        active=True, blocks_new_buy=True, reason='port_close_failure')
    assert before['new_buy_block_reasons'] == ('cleanup_fault',)
    set_time(runtime, boundary, clock, 60)
    assert runtime.status()['cleanup_faults'] == 1
    assert not runtime.status()['new_buy_allowed']
    # No forced release or production repair: process exit owns this test-only
    # daemon runtime after proving the intentionally terminal close failure.


def test_low_recovery_keeps_other_faults_latched():
    import os
    import subprocess
    import sys
    clock = Clock()
    boundary = LowBoundary(clock)
    boundary.holds = {('B', 'book'), ('E', 'order')}
    boundary.mode = {key: 'swallow' for key in boundary.holds}
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((low(), demand('B', sources=('book',)),
            demand('E', kind='exact', sources=('order',))))
        boundary.wait(lambda: len([e for e in boundary.events if e[0] == 'entered']) == 3)
        set_time(runtime, boundary, clock, 5)
        expired = collect(runtime, boundary, lambda rows: len(rows) == 3)
        assert {r.demand.order_id for r in expired} == {'L', 'B', 'E'}
        assert all(not r.complete and r.reason == 'timeout' for r in expired)
        assert boundary.low_close_entered.wait(4)
        set_time(runtime, boundary, clock, 6)
        faults = collect(runtime, boundary, lambda rows: len(rows) == 3)
        assert all(r.cleanup_fault for r in faults)
        assert runtime.status()['cleanup_faults'] == 3 and not runtime.status()['new_buy_allowed']
        boundary.low_close_release.set()
        assert boundary.low_close_exited.wait(4)
        boundary.wait(lambda: not scope_for(runtime)['active'])
        assert runtime.status()['cleanup_faults'] == 2
        assert scope_for(runtime, 'B')['active'] and scope_for(runtime, 'E')['active']
        assert not runtime.status()['new_buy_allowed']
        boundary.release.set()
        boundary.wait(lambda: all(not s['active'] for s in runtime.status()['scopes']))
        assert scope_for(runtime, 'B')['cleanup_fault'] and scope_for(runtime, 'E')['cleanup_fault']
        assert not scope_for(runtime)['cleanup_fault']
        assert runtime.status()['cleanup_faults'] == 2 and not runtime.status()['new_buy_allowed']
        details = runtime.status()['cleanup_fault_details']
        assert details['total'] == 2 and details['omitted'] == 0
        assert {row['lane'] for row in details['items']} == {'buy', 'exact'}
        assert all(row['since'] == clock.utc().isoformat() and row['reason'] == 'delayed_read_cleanup'
            for row in details['items'])
        assert runtime.drain_results() == ()
    finally:
        boundary.low_close_release.set()
        boundary.release.set()
        runtime.reconcile(())
        assert runtime.close(4)
    result = subprocess.run([sys.executable, '-c',
        'from test_lp_low_read_recovery import _verify_port_close_latch; _verify_port_close_latch()'],
        env={**os.environ, 'PYTHONPATH': 'src:tests'}, capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stdout + result.stderr


def test_low_recovery_requires_current_financial_authority(tmp_path, monkeypatch):
    from tests.test_lp_observed_pools import AsyncPoolRig

    class Rig(AsyncPoolRig):
        def __init__(self):
            self.low_hold = False
            self.low_held_once = False
            self.financial_unknown = False
            self.cleanup_entered = threading.Event()
            self.cleanup_release = threading.Event()
            self.cleanup_exited = threading.Event()
            super().__init__(tmp_path, monkeypatch)

        async def http(self, request):
            response = await super().http(request)
            source, order = request.headers['x-lp-source'], request.headers['x-lp-order']
            if source == 'open_orders' and self.financial_unknown:
                return httpx.Response(503)
            if source == 'book' and order.startswith('position:') and self.low_hold and not self.low_held_once:
                self.low_held_once = True
                rig = self
                class HeldBody(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        await asyncio.Event().wait()
                        yield b''
                    async def aclose(self):
                        rig.cleanup_entered.set()
                        await asyncio.get_running_loop().run_in_executor(None, rig.cleanup_release.wait)
                        rig.cleanup_exited.set()
                return httpx.Response(200, stream=HeldBody())
            return response

    rig = Rig()
    try:
        rig.market('D')
        rig.sdk.positions = [dict(condition_id=rig.cid('D'), token_id='tD', size='2.5', average_price='.4')]
        rig.low_hold = True
        rig.tick(5)
        rig.pump(lambda: rig.low_held_once and rig.live.read_runtime.status()['managed_tasks'] >= 2)
        started = next(s['started'] for s in rig.live.read_runtime.status()['scopes'] if s['key'][3] == 'holding')
        rig.financial_unknown = True
        rig.tick(started + 5 - rig.seconds)
        assert rig.cleanup_entered.wait(4)
        rig.pump(lambda: rig.live.state.snapshot().new_capacity.state == 'unknown')
        rig.tick(1)
        rig.pump(lambda: rig.live.read_runtime.status()['cleanup_faults'] == 1
            and rig.live.read_runtime.status()['ready_count'] == 0)
        assert not rig.live.read_runtime.status()['new_buy_allowed']
        rig.market('E')  # otherwise eligible new BUY candidate
        before_posts, before_cancels = list(rig.sdk.posts), list(rig.sdk.cancels)
        rig.tick(1)
        rig.cleanup_release.set()
        assert rig.cleanup_exited.wait(4)
        rig.pump(lambda: rig.live.read_runtime.status()['cleanup_faults'] == 0)
        assert rig.live.read_runtime.status()['new_buy_allowed']
        assert rig.live.state.snapshot().new_capacity.state == 'unknown'
        assert rig.sdk.posts == before_posts and rig.sdk.cancels == before_cancels
        for _ in range(3):
            rig.scheduler.request_check()
            rig.scheduler.run_due()
        assert rig.sdk.posts == before_posts and rig.sdk.cancels == before_cancels
        old_observation = rig.live.state.snapshot().observation.read_ticket
        at_send = []
        post = rig.sdk.post_order
        def observed_post(signed):
            at_send.append(rig.live.state.snapshot())
            return post(signed)
        rig.sdk.post_order = observed_post  # external SDK send boundary
        rig.financial_unknown = False
        rig.tick(5)
        # Sending invalidates authority immediately; capture it at the real
        # external send instead of requiring a transient post-send known state.
        rig.pump(lambda: len(rig.sdk.posts) > len(before_posts))
        assert len(at_send) == 1 and at_send[0].new_capacity.state == 'known'
        assert at_send[0].observation.read_ticket != old_observation
        assert all(at_send[0].observation.components[source].checked_at == rig.utc()
            for source in ('balance', 'open_orders', 'positions'))
        assert all(row['token_id'] == 'tE' for row in rig.sdk.posts[len(before_posts):])
        assert rig.sdk.cancels == before_cancels
    finally:
        rig.cleanup_release.set()
        rig.close()


def test_cleanup_fault_details_are_bounded_detached_and_readonly():
    from datetime import datetime
    import json
    from test_lp_financial_wait_diagnostics import plain
    class DiagnosticBoundary(LowBoundary):
        def factory(self, d):
            if d.kind != 'buy':
                return super().factory(d)
            boundary = self
            class HeldBuyBody(httpx.AsyncByteStream):
                async def __aiter__(self):
                    await asyncio.Event().wait()
                    yield b''
                async def aclose(self):
                    await asyncio.get_running_loop().run_in_executor(None, boundary.release.wait)
            async def http(request):
                boundary.record('entered', request.headers['x-lp-order'], 'book')
                return httpx.Response(200, stream=HeldBuyBody())
            return LPAsyncSourcePort({'clob': httpx.AsyncClient(transport=httpx.MockTransport(http),
                base_url='http://127.0.0.1', trust_env=False)}, clock=self.clock, utc_now=self.clock.utc)
    clock = Clock()
    boundary = DiagnosticBoundary(clock)
    boundary.holds = {(f'private-order-{i}', 'book') for i in range(18)}
    notices = []
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start(lambda: notices.append('wake'))
    try:
        runtime.reconcile((low(), *(demand(order, sources=('book',)) for order, _ in sorted(boundary.holds))))
        boundary.wait(lambda: len([e for e in boundary.events if e[0] == 'entered']) == 19)
        set_time(runtime, boundary, clock, 5)
        assert boundary.low_close_entered.wait(4)
        set_time(runtime, boundary, clock, 6)
        boundary.wait(lambda: runtime.status()['cleanup_faults'] == 19 and len(notices) >= 3)
        status = runtime.status()
        details = plain(status['cleanup_fault_details'])
        assert details['total'] == 19 and details['omitted'] == 3 and len(details['items']) == 16
        item = next(row for row in details['items'] if row['lane'] == 'low')
        assert item == dict(lane='low', kind='sell', since=clock.utc().isoformat(), active=True,
            blocks_new_buy=True, reason='delayed_read_cleanup')
        assert datetime.fromisoformat(item['since']).utcoffset() is not None
        assert all(row['blocks_new_buy'] is False for row in details['items'] if row['lane'] == 'buy')
        assert status['new_buy_block_reasons'] == ('cleanup_fault',)
        assert all(set(row) == {'lane', 'kind', 'since', 'active', 'blocks_new_buy', 'reason'}
            for row in details['items'])
        encoded = json.dumps(details)
        assert all(secret not in encoded for secret in ('private-order', 'account', 'epoch', 'generation',
            'token', 'http', 'Authorization', 'exception'))
        assert len(encoded.encode()) < 4096
        before = (list(boundary.events), list(boundary.low_requests), len(notices))
        for _ in range(10):
            assert plain(runtime.status()['cleanup_fault_details']) == details
        assert (list(boundary.events), list(boundary.low_requests), len(notices)) == before
        with pytest.raises(TypeError):
            status['cleanup_fault_details']['items'][0]['kind'] = 'mutated'
        details['items'][0]['since'] = 'mutated-copy'
        assert plain(runtime.status()['cleanup_fault_details']) != details
        held_snapshot = plain(status['cleanup_fault_details'])
        set_time(runtime, boundary, clock, 7)
        boundary.low_close_release.set()
        assert boundary.low_close_exited.wait(4)
        boundary.wait(lambda: not scope_for(runtime)['active'])
        current = plain(runtime.status()['cleanup_fault_details'])
        assert current['total'] == 18 and all(row['lane'] == 'buy' for row in current['items'])
        assert runtime.status()['new_buy_allowed'] and runtime.status()['new_buy_block_reasons'] == ()
        assert plain(status['cleanup_fault_details']) == held_snapshot
        capped = LPReadRuntime(boundary.factory, buy_cap=0)
        capped.reconcile((demand('deferred'),))
        assert capped.status()['new_buy_block_reasons'] == ('capacity', 'deferred')
        assert capped.status()['cleanup_fault_details']['total'] == 0
        assert not capped.status()['new_buy_allowed'] and capped.close()
    finally:
        boundary.low_close_release.set()
        boundary.release.set()
        runtime.reconcile(())
        assert runtime.close(4)


def test_auto_state_exposes_low_cleanup_fault_without_reads_or_wakeups(tmp_path):
    from decimal import Decimal
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from test_lp_async_live_contract import Trading
    from test_lp_financial_wait_diagnostics import plain
    from tests.test_lp_d2_execution_bridge import attach

    writes = []
    class SQLBoundary(PredictionArbitrageStore):
        def _connection(self):
            connection = super()._connection()
            connection.set_trace_callback(lambda sql: writes.append(sql)
                if sql.lstrip().split(' ', 1)[0].upper() in
                ('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'DROP', 'ALTER') else None)
            return connection

    clock = Clock()
    boundary = LowBoundary(clock)
    store, trading = SQLBoundary(tmp_path), Trading()
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    live = LPD2LiveExecution(store, trading, account_id='account', venue='polymarket', process_epoch='epoch',
        policy=CalculationPolicy(Decimal(100)), read_runtime=runtime, clock=clock, utc_now=clock.utc)
    live.initialize(ValidatedStartAdmission('epoch', 'offline-permit'))
    execution = attach(live, PolymarketLPService(store, trading, clock=clock.utc), tmp_path)
    notices = []
    execution.set_lp_auto_wakeup(lambda: notices.append(plain(runtime.status())))
    try:
        runtime.reconcile((low(),))
        boundary.wait(lambda: len(boundary.low_requests) == 1)
        set_time(runtime, boundary, clock, 5)
        assert boundary.low_close_entered.wait(4)
        set_time(runtime, boundary, clock, 6)
        boundary.wait(lambda: any(n['observed_monotonic'] == 6 and n['cleanup_faults'] == 1 for n in notices))
        details = plain(runtime.status()['cleanup_fault_details'])
        assert details['items'][0] == dict(lane='low', kind='sell', since=clock.utc().isoformat(),
            active=True, blocks_new_buy=True, reason='delayed_read_cleanup')
        state = live.state.snapshot()
        baseline = (tuple(boundary.low_requests), tuple(trading.cancels), tuple(writes), len(notices), tuple(store.lp_sessions()))
        for _ in range(5):
            projected = execution.lp_auto_state(include_intents=False)['diagnostics']['async_reads']
            assert projected['cleanup_fault_details'] == details
            assert projected['new_buy_block_reasons'] == ['cleanup_fault']
            assert 'scopes' not in projected
            projected['cleanup_fault_details']['items'][0]['kind'] = 'mutated-copy'
        assert live.state.snapshot() == state
        assert (tuple(boundary.low_requests), tuple(trading.cancels), tuple(writes), len(notices), tuple(store.lp_sessions())) == baseline
        assert not boundary.low_close_exited.is_set()
    finally:
        boundary.low_close_release.set()
        runtime.reconcile(())
        assert live.close(4)
