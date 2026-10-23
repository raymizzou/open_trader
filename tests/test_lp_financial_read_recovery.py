"""Financial recovery through real HTTPX response ownership and public runtime seams."""
import asyncio
import json
import threading

import httpx
import pytest

from test_lp_async_source_contract import independent_watchdog
from test_lp_async_read_runtime_contract import Boundary, Clock, collect, demand
from open_trader.polymarket_lp_async_source import LPAsyncSourcePort
from open_trader.polymarket_lp_read_runtime import LPReadRuntime

FINANCIAL = ('balance', 'open_orders', 'positions')


class FinancialBoundary(Boundary):
    """Hold the external response body and its actual close independently."""
    def __init__(self, clock):
        super().__init__(clock)
        self.hold_source = None
        self.hold_next = True
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.close_exited = threading.Event()
        self.clients = []
        self.ports = []
        self.requests = []
        self.cool_source = None
        self.publication_source = None
        self.publication_entered = threading.Event()
        self.publication_release = threading.Event()
        self.publication_release.set()

    def factory(self, d):
        if d.kind != 'financial':
            return super().factory(d)
        boundary = self

        class HeldResponse(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.Event().wait()
                yield b''

            async def aclose(self):
                boundary.close_entered.set()
                await asyncio.get_running_loop().run_in_executor(None, boundary.close_release.wait)
                boundary.record('response_closed', d.order_id, boundary.hold_source)
                boundary.close_exited.set()

        async def http(request):
            source = request.headers['x-lp-source']
            with self.changed:
                self.requests.append((source, self.clock()))
            self.record('entered', d.order_id, source)
            if source == self.hold_source and self.hold_next:
                self.hold_next = False
                return httpx.Response(200, stream=HeldResponse())
            if source == self.publication_source and not self.publication_release.is_set():
                self.publication_entered.set()
                await asyncio.get_running_loop().run_in_executor(None, self.publication_release.wait)
            if source == self.cool_source:
                self.cool_source = None
                return httpx.Response(429, headers={'Retry-After': '20'})
            value = {'balance': dict(balance='100000000', allowances={'spender': '100000000'}),
                     'open_orders': dict(data=self.orders, next_cursor='LTE='), 'positions': []}[source]
            self.record('finally_exited', d.order_id, source)
            return httpx.Response(200, json=value)

        clients = {name: httpx.AsyncClient(transport=httpx.MockTransport(http),
                   base_url='http://127.0.0.1', trust_env=False) for name in ('clob', 'data')}
        self.clients.extend(clients.values())
        port = LPAsyncSourcePort(clients, clock=self.clock, utc_now=self.clock.utc)
        self.ports.append(port)
        return port


def financial():
    return demand('O', kind='financial', sources=FINANCIAL)


def financial_scope(runtime):
    return next(s for s in runtime.status()['scopes'] if s['key'][3] == 'financial')


def set_time(runtime, boundary, clock, stamp):
    clock.value = stamp
    runtime.wake()
    boundary.wait(lambda: runtime.status()['observed_monotonic'] == stamp)


@pytest.mark.parametrize('source', FINANCIAL)
def test_financial_cleanup_completion_restores_existing_reader(source):
    clock = Clock()
    boundary = FinancialBoundary(clock)
    boundary.hold_source = source
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((financial(),))
        boundary.wait(lambda: len(boundary.requests) == 3)
        first = financial_scope(runtime)['attempt']
        set_time(runtime, boundary, clock, 5)
        assert boundary.close_entered.wait(4)
        expired = collect(runtime, boundary, bool)
        assert len(expired) == 1 and expired[0].reason == 'timeout' and not expired[0].complete
        old_ids = {name: result.source_id for name, result in expired[0].sources.items()}
        set_time(runtime, boundary, clock, 6)
        faults = collect(runtime, boundary, bool)
        assert len(faults) == 1 and faults[0].cleanup_fault and faults[0].attempt == first
        assert runtime.drain_results() == ()
        assert not runtime.status()['new_buy_allowed']
        assert financial_scope(runtime)['active'] and runtime.status()['cleanup_faults'] == 1
        before = tuple(boundary.requests)
        set_time(runtime, boundary, clock, 7)
        assert tuple(boundary.requests) == before and len(boundary.clients) == 2
        assert not boundary.close_exited.is_set()
        boundary.close_release.set()
        assert boundary.close_exited.wait(4)
        # A fresh complete attempt must emerge from the same owned port/client.
        recovered = collect(runtime, boundary, lambda rows: any(r.complete for r in rows))
        assert len(recovered) == 1 and recovered[0].complete
        fresh = recovered[0]
        assert fresh.attempt > first and fresh.started == 7
        assert set(fresh.sources) == set(FINANCIAL)
        assert all(r.complete and not r.error and r.checked_at == clock.utc() for r in fresh.sources.values())
        assert all(fresh.sources[name].source_id != old_ids[name] for name in FINANCIAL)
        assert len(boundary.ports) == 1 and len(boundary.clients) == 2
        assert len(boundary.requests) == 6 and runtime.drain_results() == ()
        status = runtime.status()
        assert status['cleanup_faults'] == 0 and status['new_buy_allowed']
        assert status['financial_cleanup_faults'] == 1
        history = status['last_financial_cleanup_fault']
        assert dict(history) == dict(attempt=first, sources=(source,), fault_monotonic=6, ended_monotonic=7)
        assert 'account' not in json.dumps(dict(history)) and 'O' not in json.dumps(dict(history))
        before_status = tuple(boundary.requests)
        for _ in range(20):
            assert runtime.status()['last_financial_cleanup_fault'] == history
        assert tuple(boundary.requests) == before_status
    finally:
        boundary.close_release.set()
        runtime.reconcile(())
        assert runtime.close(4)
        assert all(client.is_closed for client in boundary.clients)


@pytest.mark.parametrize('mode', ('ordinary', 'observed_pools'))
def test_scheduled_financial_recovery_restores_current_facts(tmp_path, mode):
    from decimal import Decimal
    from datetime import datetime
    import time
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from test_lp_async_live_contract import Trading, wire_row
    from tests.test_lp_d2_integration import settle_control
    import hashlib
    from tests.test_lp_d2_execution_bridge import attach

    clock = Clock()
    boundary = FinancialBoundary(clock)
    boundary.orders = [wire_row('A')]
    trading = Trading()
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    store.lp_save_current_settings(hashlib.sha256(b'account').hexdigest(), expected_config_version=0,
        changes=dict(budget_usd='100', allocation_mode='observed_pools' if mode == 'observed_pools' else 'minimum'))
    live = LPD2LiveExecution(store, trading,
        account_id='account', venue='polymarket', process_epoch='epoch',
        policy=CalculationPolicy(Decimal(100), target_count=1, allocation_mode='observed_pools' if mode == 'observed_pools' else 'minimum',
            trial_budget_usd='100', income_budget_usd='100', allocation_fields=('allocation_mode',)),
        read_runtime=runtime, clock=clock, utc_now=clock.utc)
    live.initialize(ValidatedStartAdmission('epoch', 'offline-permit'))
    execution = attach(live, PolymarketLPService(live.store, trading, clock=clock.utc), tmp_path)
    scheduler = LPAutoScheduler(execution, clock=clock.utc)
    execution.set_lp_auto_scheduler(scheduler)
    woken = threading.Event()
    def wake():
        woken.set()
        scheduler.request_check()
    execution.set_lp_auto_wakeup(wake)

    def pump(predicate):
        end = time.monotonic() + 4
        while not predicate():
            scheduler.run_due()
            left = end - time.monotonic()
            assert left > 0, (live.status(), live.state.snapshot(), boundary.events)
            with boundary.changed:
                boundary.changed.wait(min(.01, left))

    def cancel_audit():
        return [a for row in live.diagnostics()['local_outcomes']['recent'] if row['kind'] == 'CANCEL'
            for a in store.lp_actions('d2:' + row['request_id'])]

    def fresh():
        observation = live.state.snapshot().observation
        return (observation is not None and observation.orders_complete
            and observation.components['open_orders'].checked_at == clock.utc()
            and any(y.evaluated_at == clock.utc() for y in live.state.snapshot().yields.values()))

    try:
        pump(fresh)
        initial = live.state.snapshot().observation
        assert initial.orders_complete and live.state.snapshot().active_orders[0].key[3] == 'A'
        assert live.rounds.snapshot().active_batch_id is None  # no unfinished fixed BUY plan
        # A real earlier UNKNOWN startup cancel remains audit/identity, not a read gate.
        live.request_running(True)
        settle_control(live)
        clock.value = 5
        runtime.wake()
        scheduler.request_check()
        pump(lambda: trading.cancels == [('A',)] and live.status()['native_inflight'] == 0
            and live.rounds.snapshot().round_number >= 1 and fresh()
            and len(cancel_audit()) == 1 and cancel_audit()[0].get('receipt', {}).get('outcome') == 'unknown')
        assert live.rounds.snapshot().active_batch_id is None
        unknown_request = cancel_audit()[0]['request_id']
        assert cancel_audit()[0]['order_id'] == 'A'
        assert trading.cancels == [('A',)]
        financial_before = len(boundary.requests)
        for _ in range(3):
            scheduler.request_check()
            scheduler.run_due()
        assert len(boundary.requests) == financial_before and trading.cancels == [('A',)]
        boundary.hold_source = 'open_orders'
        boundary.hold_next = True
        clock.value = 60
        runtime.wake()
        scheduler.request_check()
        pump(lambda: ('open_orders', 60) in boundary.requests
            and any(y.evaluated_at == clock.utc() for y in live.state.snapshot().yields.values())
            and all(not s['active'] for s in runtime.status()['scopes'] if s['key'][3] == 'buy'))
        old = financial_scope(runtime)['attempt']
        clock.value = 65
        runtime.wake()
        pump(lambda: live.state.snapshot().observation is not None
            and not live.state.snapshot().observation.orders_complete)
        assert boundary.close_entered.wait(4)
        pump(lambda: any(y.evaluated_at == clock.utc() for y in live.state.snapshot().yields.values())
            and all(not s['active'] for s in runtime.status()['scopes'] if s['key'][3] == 'buy'))
        clock.value = 66
        runtime.wake()
        pump(lambda: runtime.status()['cleanup_faults'] == 1 and runtime.status()['ready_count'] == 0)
        assert not runtime.status()['new_buy_allowed']
        assert live.state.snapshot().new_capacity.state == 'unknown'
        round_before = live.rounds.snapshot().round_number
        assert live.rounds.snapshot().active_batch_id is None
        before = tuple(boundary.requests)
        for _ in range(30):
            scheduler.run_due()
            execution.lp_auto_state(include_intents=False)
        assert tuple(boundary.requests) == before and not boundary.close_exited.is_set()
        assert len(boundary.clients) == 2 and len(boundary.ports) == 1
        clock.value = 67
        runtime.wake()
        woken.clear()
        boundary.publication_source = 'open_orders'
        boundary.publication_release.clear()
        boundary.close_release.set()
        assert boundary.close_exited.wait(4) and woken.wait(4)
        assert boundary.publication_entered.wait(4)
        pump(lambda: runtime.status()['cleanup_faults'] == 0 and financial_scope(runtime)['attempt'] > old)
        assert live.state.snapshot().new_capacity.state == 'unknown'
        assert live.rounds.snapshot().active_batch_id is None
        assert live.diagnostics()['round_start']['fixed_buy_plan'] == 'absent'
        boundary.publication_release.set()
        pump(fresh)
        recovered = live.state.snapshot()
        assert recovered.observation.read_ticket != initial.read_ticket
        assert recovered.observation.orders_complete and recovered.new_capacity.state == 'known'
        assert recovered.observation.components['open_orders'].source_id != initial.components['open_orders'].source_id
        assert recovered.observation.components['open_orders'].checked_at == clock.utc()
        assert datetime.fromisoformat(scheduler.snapshot()['last_check_at']) == clock.utc()
        assert financial_scope(runtime)['attempt'] > old
        assert runtime.status()['cleanup_faults'] == 0
        assert runtime.status()['financial_cleanup_faults'] == 1
        assert runtime.status()['last_financial_cleanup_fault']['ended_monotonic'] == 67
        assert len(boundary.clients) == 2 and len(boundary.ports) == 1
        assert all(sum(s == source and stamp == 67 for s, stamp in boundary.requests) == 1 for source in FINANCIAL)
        assert live.rounds.snapshot().active_batch_id is None
        assert trading.cancels == [('A',)]
        assert [a['request_id'] for a in cancel_audit()] == [unknown_request]
        assert cancel_audit()[0]['receipt']['outcome'] == 'unknown'
        if mode == 'observed_pools':
            assert execution.lp_auto_state(include_intents=False)['allocation_mode'] == 'observed_pools'
        # A new normal financial cadence continues despite historical UNKNOWN.
        clock.value = 72
        runtime.wake()
        pump(fresh)
        assert live.state.snapshot().observation.orders_complete and trading.cancels == [('A',)]
        assert live.rounds.snapshot().round_number > round_before
        assert all(sum(s == source and stamp == 72 for s, stamp in boundary.requests) == 1 for source in FINANCIAL)
    finally:
        boundary.close_release.set()
        boundary.publication_release.set()
        boundary.release.set()
        scheduler.stop()
        assert live.close(4)
        assert all(client.is_closed for client in boundary.clients)


def test_financial_recovery_preserves_mailbox_and_provider_cooldown():
    from dataclasses import replace
    clock = Clock()
    boundary = FinancialBoundary(clock)
    boundary.hold_source = 'open_orders'
    boundary.cool_source = 'balance'
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    original = replace(financial(), observation_id='old-ticket')
    try:
        runtime.reconcile((original,))
        boundary.wait(lambda: len(boundary.requests) == 3)
        old = financial_scope(runtime)['attempt']
        set_time(runtime, boundary, clock, 5)
        assert boundary.close_entered.wait(4)
        set_time(runtime, boundary, clock, 6)
        boundary.wait(lambda: runtime.status()['cleanup_faults'] == 1)
        assert not runtime.status()['new_buy_allowed']
        set_time(runtime, boundary, clock, 7)
        boundary.close_release.set()
        assert boundary.close_exited.wait(4)
        boundary.wait(lambda: not financial_scope(runtime)['active'])
        assert runtime.status()['cleanup_faults'] == 0 and runtime.status()['ready_count'] == 1
        runtime.reconcile((replace(original, observation_id='new-ticket'),))
        set_time(runtime, boundary, clock, 19)
        assert len(boundary.requests) == 3 and financial_scope(runtime)['attempt'] == old
        notices = runtime.drain_results()
        assert len(notices) == 1
        assert notices[0].attempt == old and notices[0].demand.observation_id == 'old-ticket'
        assert notices[0].reason == 'timeout' and notices[0].cleanup_fault and not notices[0].complete
        assert notices[0].sources['balance'].retry_at == 20
        assert runtime.drain_results() == ()
        boundary.wait(lambda: runtime.status()['next_dependency_monotonic'] == 20)
        assert len(boundary.requests) == 3
        boundary.close_release.clear()
        boundary.close_entered.clear()
        boundary.close_exited.clear()
        boundary.hold_next = True
        set_time(runtime, boundary, clock, 20)
        boundary.wait(lambda: len(boundary.requests) == 6)
        assert financial_scope(runtime)['attempt'] > old and financial_scope(runtime)['started'] == 20
        assert len(boundary.clients) == 2 and runtime.drain_results() == ()
        assert runtime.close(.02) is False
        assert boundary.close_entered.wait(4) and not boundary.close_exited.is_set()
        assert any(not client.is_closed for client in boundary.clients)
        before = tuple(boundary.requests)
        set_time(runtime, boundary, clock, 30)
        assert tuple(boundary.requests) == before
        boundary.close_release.set()
        assert boundary.close_exited.wait(4)
        assert runtime.close(4)
        assert tuple(boundary.requests) == before and runtime.drain_results() == ()
        assert all(client.is_closed for client in boundary.clients)
        # History stays fixed-size after scopes retire and no demand remains.
        status = runtime.status()
        assert status['financial_cleanup_faults'] == 2 and status['cleanup_faults'] == 0
        assert set(status['last_financial_cleanup_fault']) == {'attempt', 'sources', 'fault_monotonic', 'ended_monotonic'}
        assert status['last_financial_cleanup_fault']['attempt'] > old
    finally:
        boundary.close_release.set()
        runtime.reconcile(())
        assert runtime.close(4)
