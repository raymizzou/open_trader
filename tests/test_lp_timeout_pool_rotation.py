"""Fixed-lane disposal at the external HTTP transport ownership boundary."""
import asyncio
import os
import threading

import httpx
import pytest

from open_trader.polymarket_lp_async_source import LPAsyncSourcePort, ProviderLimits
from open_trader.polymarket_lp_read_runtime import LPReadRuntime
from test_lp_async_read_runtime_contract import Boundary, Clock, collect, demand
from test_lp_async_source_contract import independent_watchdog, CID


class LeaseTransport(httpx.AsyncBaseTransport):
    """Two literal external leases, deliberately lost on native cancellation.

    A replacement is useful only if aclose really disposes the lost leases.
    Nothing here observes or alters HTTPX/httpcore private pool state.
    """
    def __init__(self, boundary, lane, size, poisoned):
        self.boundary, self.lane, self.size, self.poisoned = boundary, lane, size, poisoned
        self.leased = 0
        self.closed = threading.Event()
        self.changed = asyncio.Event()

    async def handle_async_request(self, request):
        source = request.headers['x-lp-source']
        order = request.headers['x-lp-order']
        b = self.boundary
        b.record('request', order, source)
        while self.leased == self.size:
            b.record('waiting_for_lease', order, source)
            await self.changed.wait()
            self.changed.clear()
        assert not self.closed.is_set(), 'closed generation reused'
        self.leased += 1
        b.record('leased', order, source)
        if self.poisoned:
            # Cancel exits the request but intentionally leaves its lease owned
            # by this transport. Only whole-client close can reclaim it.
            try:
                await asyncio.Event().wait()
            finally:
                b.record('cancelled_lease_retained', order, source)
                if not b.read_release.is_set():
                    await asyncio.get_running_loop().run_in_executor(None, b.read_release.wait)
        self.leased -= 1
        self.changed.set()
        if source == b.cool_source:
            b.cool_source = None
            return httpx.Response(429, headers={'Retry-After':'20'})
        return b.response(source)

    async def aclose(self):
        self.boundary.close_entered.set()
        if os.environ.get('LP491_CLOSE_SCHEDULE') == 'yield_first':
            await asyncio.sleep(0)  # controlled alternate close/read-unwind interleaving
        if not self.boundary.close_release.is_set():
            await asyncio.get_running_loop().run_in_executor(None, self.boundary.close_release.wait)
        self.leased = 0
        self.closed.set()
        self.changed.set()
        self.boundary.record('transport_closed', self.lane, '')


class PoolBoundary(Boundary):
    def __init__(self, clock, poison='financial'):
        super().__init__(clock)
        self.poison_all = False
        self.poison = poison
        self.close_release = threading.Event()
        self.close_release.set()
        self.read_release = threading.Event()
        self.read_release.set()
        self.close_entered = threading.Event()
        self.cool_source = None
        self.generations = {}
        self.clients = {}
        self.transports = {}
        self.limits = ProviderLimits()

    @staticmethod
    def response(source):
        if source == 'balance':
            value = dict(balance='100000000', allowances={'spender':'100000000'})
        elif source in ('open_orders', 'fills'):
            value = dict(data=[], next_cursor='LTE=')
        elif source == 'positions':
            value = []
        else:
            value = dict(id='exact', market=CID, asset_id='yes', side='BUY', original_size='20',
                size_matched='0', price='0.40', status='LIVE', owner='owner',
                maker_address='account', outcome='Yes', expiration='0', order_type='GTC', created_at=0)
        return httpx.Response(200, json=value)

    def factory(self, d):
        lane = d.kind
        generation = self.generations.get(lane, 0)
        self.generations[lane] = generation + 1
        sizes = {'clob':2, 'data':1} if lane == 'financial' else {'clob':2}
        transports = {name: LeaseTransport(self, lane, size,
            poisoned=lane == self.poison and (generation == 0 or self.poison_all) and name == 'clob')
            for name, size in sizes.items()}
        clients = {name: httpx.AsyncClient(transport=transports[name], base_url='http://127.0.0.1',
            limits=httpx.Limits(max_connections=size,max_keepalive_connections=size),
            timeout=None, trust_env=False) for name, size in sizes.items()}
        self.transports.setdefault(lane, []).append(transports)
        self.clients.setdefault(lane, []).append(clients)
        self.record('factory', lane, '')
        return LPAsyncSourcePort(clients, clock=self.clock, utc_now=self.clock.utc, provider_limits=self.limits)

    def not_before(self, provider, account):
        return self.limits.not_before(provider, account)


def advance(runtime, boundary, clock, stamp):
    clock.value = stamp
    runtime.wake()
    boundary.wait(lambda: runtime.status()['observed_monotonic'] == stamp)


@pytest.mark.parametrize('lane', ['financial', 'exact'])
def test_timeout_replaces_entire_fixed_client_before_next_attempt(lane):
    clock = Clock()
    boundary = PoolBoundary(clock, poison=lane)
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    financial = demand('O', kind='financial', sources=('balance','open_orders','positions'), observation_id='observation-1')
    exact = demand('exact', kind='exact', sources=('order',))
    fills = demand('fills', kind='exact', sources=('fills',))
    requests = (financial, exact, fills)
    try:
        runtime.reconcile(requests if lane == 'financial' else (financial, exact))
        if lane == 'exact':
            boundary.wait(lambda: any(e[:3] == ('leased','exact','order') for e in boundary.events))
            advance(runtime, boundary, clock, 2)
            runtime.reconcile(requests)
        boundary.wait(lambda: sum(e[0] == 'leased' and e[1] in
            (('O',) if lane == 'financial' else ('exact','fills')) for e in boundary.events)
            == (3 if lane == 'financial' else 2))
        old = boundary.transports[lane][0]
        assert old['clob'].leased == 2
        advance(runtime, boundary, clock, 4.999)
        assert not any(t.closed.is_set() for t in old.values())
        assert boundary.generations[lane] == 1
        healthy = collect(runtime, boundary, lambda rows: len([r for r in rows if r.demand.kind != lane]) == (2 if lane == 'financial' else 1))
        assert all(r.complete for r in healthy)
        advance(runtime, boundary, clock, 5)
        rows = collect(runtime, boundary, lambda rows: any(r.demand.kind == lane for r in rows))
        expired = next(r for r in rows if r.demand.order_id == ('O' if lane == 'financial' else 'exact'))
        assert not expired.complete and expired.reason == 'timeout'
        assert expired.demand == (financial if lane == 'financial' else exact)
        assert (expired.started, expired.deadline) == (0, 5)
        boundary.wait(lambda: all(t.closed.is_set() for t in old.values())
            or any(e[0] == 'waiting_for_lease' for e in boundary.events))
        # Without rotation the next attempt demonstrably waits behind both lost
        # leases. Whole-client disposal must precede the new successful read.
        assert old['clob'].closed.is_set(), boundary.events
        assert all(t.closed.is_set() for t in old.values())
        assert all(c.is_closed for c in boundary.clients[lane][0].values())
        fresh = collect(runtime, boundary, lambda rows: any(r.demand == expired.demand and r.complete for r in rows))
        new = next(r for r in fresh if r.demand == expired.demand and r.complete)
        assert new.attempt > expired.attempt and new.demand == (financial if lane == 'financial' else exact)
        if lane == 'exact':
            peer_rows = rows + fresh
            if not any(r.demand == fills for r in peer_rows):
                peer_rows += collect(runtime, boundary, lambda rows: any(r.demand == fills for r in rows))
            peer = next(r for r in peer_rows if r.demand == fills)
            assert not peer.complete and peer.reason == 'pool_rotated'
            assert peer.started == 2 and peer.deadline == 7
            assert peer.demand.target_order_id == fills.target_order_id
            # The shared fills owner is sealed with its own identity; it cannot
            # publish facts obtained after its generation was closed.
            assert not any(r.complete and r.attempt == peer.attempt for r in runtime.drain_results())
        assert all(s.complete and not s.error for s in new.sources.values())
        assert not expired.complete
        assert boundary.generations == ({'financial':2,'exact':1} if lane == 'financial' else {'financial':1,'exact':2})
        assert {k:t.size for k,t in boundary.transports[lane][1].items()} == ({'clob':2,'data':1} if lane == 'financial' else {'clob':2})
        assert runtime.status()['pool_count'] == 3
    finally:
        runtime.reconcile(())
        assert runtime.close(4)
        assert all(c.is_closed for generations in boundary.clients.values() for clients in generations for c in clients.values())


def test_timed_out_pool_close_retains_fault_and_blocks_replacement():
    clock = Clock()
    boundary = PoolBoundary(clock)
    boundary.close_release.clear()
    boundary.read_release.clear()
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    request = demand('O', kind='financial', sources=('balance','open_orders','positions'), observation_id='original')
    try:
        runtime.reconcile((request,))
        boundary.wait(lambda: sum(e[0] == 'leased' for e in boundary.events) == 3)
        advance(runtime, boundary, clock, 5)
        expired = collect(runtime, boundary, bool)[0]
        assert expired.reason == 'timeout' and not expired.complete
        assert boundary.close_entered.wait(4)
        advance(runtime, boundary, clock, 6)
        boundary.wait(lambda: runtime.status()['cleanup_faults'] > 0)
        faults = collect(runtime, boundary, bool)
        assert faults[0].attempt == expired.attempt and faults[0].cleanup_fault
        status = runtime.status()
        assert not status['new_buy_allowed']
        assert any(d['reason'] == 'port_close_failure' for d in status['cleanup_fault_details']['items'])
        for stamp in (10, 50, 125):
            advance(runtime, boundary, clock, stamp)
            runtime.reconcile((request,))
            assert boundary.generations == {'financial':1}
            assert not boundary.transports['financial'][0]['clob'].closed.is_set()
            assert runtime.status()['scopes'][0]['active']
        assert not runtime.close(0)
        boundary.close_release.set()
        boundary.wait(lambda: all(t.closed.is_set() for t in boundary.transports['financial'][0].values()))
        assert runtime.status()['scopes'][0]['active']
        assert not runtime.close(0)  # client close does not release original source owners
        assert boundary.generations == {'financial':1}
    finally:
        boundary.close_release.set()
        boundary.read_release.set()
        assert runtime.close(4)
        assert all(c.is_closed for c in boundary.clients['financial'][0].values())


def test_successful_fixed_reads_reuse_clients_and_preserve_cooldown():
    clock = Clock()
    boundary = PoolBoundary(clock, poison=None)
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    requests = (demand('O',kind='financial',sources=('balance','open_orders','positions')),
                demand('exact',kind='exact',sources=('order',)))
    try:
        runtime.reconcile(requests)
        for stamp in (0, 5):
            advance(runtime, boundary, clock, stamp)
            rows = collect(runtime, boundary, lambda rows: len(rows) == 2)
            assert all(r.complete for r in rows)
            assert boundary.generations == {'financial':1,'exact':1}
            assert not any(t.closed.is_set() for generations in boundary.transports.values()
                for transports in generations for t in transports.values())
        old = boundary.transports['financial'][0]
        old['clob'].poisoned = True
        boundary.cool_source = 'positions'
        advance(runtime, boundary, clock, 10)
        boundary.wait(lambda: old['clob'].leased == 2 and boundary.limits.not_before('data','account') == 30)
        advance(runtime, boundary, clock, 15)
        rows = collect(runtime, boundary, lambda rows: any(r.demand.kind == 'financial' for r in rows))
        expired = next(r for r in rows if r.demand.kind == 'financial')
        assert expired.reason == 'timeout' and expired.sources['positions'].retry_at == 30
        assert expired.sources['positions'].error_facts['status'] == 429
        boundary.wait(lambda: all(t.closed.is_set() for t in old.values()))
        boundary.wait(lambda: all(not s['active'] for s in runtime.status()['scopes'] if s['kind'] == 'financial'))
        advance(runtime, boundary, clock, 29.999)
        assert boundary.generations['financial'] == 1
        advance(runtime, boundary, clock, 30)
        fresh = collect(runtime, boundary, lambda rows: any(r.demand.kind == 'financial' and r.complete for r in rows))
        assert next(r for r in fresh if r.demand.kind == 'financial' and r.complete).started == 30
        assert boundary.generations == {'financial':2,'exact':1}
        runtime.reconcile(())
        boundary.wait(lambda: not runtime.status()['scopes'])
        before = list(boundary.events)
        advance(runtime, boundary, clock, 60)
        assert boundary.events == before
        assert runtime.drain_results() == ()
    finally:
        assert runtime.close(4)


def test_financial_timeout_streak_resets_only_on_complete_current_success():
    from dataclasses import replace
    clock = Clock()
    boundary = PoolBoundary(clock)
    boundary.poison_all = True
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    request = demand('O',kind='financial',sources=('balance','open_orders','positions'))
    try:
        runtime.reconcile((request,))
        boundary.wait(lambda: bool(boundary.transports.get('financial'))
            and boundary.transports['financial'][-1]['clob'].leased == 2)
        advance(runtime, boundary, clock, 5)
        expired = collect(runtime, boundary, bool)[0]
        assert expired.reason == 'timeout' and not expired.complete
        streak = runtime.status()['financial_timeout_streak']
        assert streak['state'] == 'timed_out' and streak['elapsed_seconds'] == 0
        assert streak['timeouts'] == 1
        owner = streak['identity']
        boundary.wait(lambda: len(boundary.transports['financial']) == 2
            and boundary.transports['financial'][-1]['clob'].leased == 2)
        advance(runtime, boundary, clock, 70)
        expired_again = collect(runtime, boundary, bool)
        assert all(not r.complete for r in expired_again)
        streak = runtime.status()['financial_timeout_streak']
        assert streak['state'] == 'timed_out' and streak['elapsed_seconds'] == 65
        assert streak['timeouts'] == 2 and streak['identity'] == owner
        boundary.wait(lambda: len(boundary.transports['financial']) == 3
            and boundary.transports['financial'][-1]['clob'].leased == 2)
        # A different current generation cannot inherit the old outage or be
        # healed by its late result. The retained timeout remains historical.
        current = replace(request, generation='new-generation')
        runtime.reconcile((current,))
        streak = runtime.status()['financial_timeout_streak']
        assert streak['state'] == 'unknown' and streak['elapsed_seconds'] is None
        assert streak['identity'] != owner
        boundary.poison_all = False
        boundary.transports['financial'][-1]['clob'].poisoned = False
        # Retiring the old scope cancels its retained leases. Time out the new
        # scope sharing that generation so it must close before fresh success.
        boundary.wait(lambda: any(s['key'] == current.key and s['active'] for s in runtime.status()['scopes']))
        boundary.wait(lambda: any(e[0] == 'waiting_for_lease' and e[3] == 70 for e in boundary.events))
        advance(runtime, boundary, clock, 75)
        collect(runtime, boundary, lambda rows: any(r.demand == current and r.complete for r in rows))
        streak = runtime.status()['financial_timeout_streak']
        assert streak['state'] == 'healthy' and streak['elapsed_seconds'] is None and streak['timeouts'] == 0
        assert runtime.status()['last_financial_timeout'] is not None
        before = list(boundary.events)
        for _ in range(5):
            assert runtime.status()['financial_timeout_streak']['state'] == 'healthy'
        assert before == boundary.events
    finally:
        runtime.reconcile(())
        assert runtime.close(4)
