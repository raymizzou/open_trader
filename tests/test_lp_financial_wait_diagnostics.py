"""Timeout observations at real source/HTTPX and public state boundaries."""
import asyncio
from dataclasses import replace
import json
import threading

import httpx
import pytest

from test_lp_async_source_contract import independent_watchdog
from test_lp_async_read_runtime_contract import Boundary, Clock, collect, demand
from open_trader.polymarket_lp_async_source import LPAsyncSourcePort
from open_trader.polymarket_lp_read_runtime import LPReadRuntime

FINANCIAL = ('balance', 'open_orders', 'positions')


class WaitBoundary(Boundary):
    def __init__(self, clock, phase='headers'):
        super().__init__(clock)
        self.phase = phase
        self.entered = threading.Event()
        self.cancelled = threading.Event()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.close_exited = threading.Event()
        self.hold_next = True
        self.requests = []
        self.private_http_payload = None
        self.cancel_count = 0

    async def held_headers(self):
        self.entered.set()
        if self.phase == 'blocked':
            assert self.release.wait(4), 'external loop-block release watchdog'
        await asyncio.Event().wait()

    async def held_body(self):
        self.entered.set()
        await asyncio.Event().wait()

    def factory(self, d):
        boundary = self

        class ResponseBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                try:
                    await boundary.held_body()
                    yield b''
                except asyncio.CancelledError:
                    boundary.cancel_count += 1
                    boundary.cancelled.set()
                    raise

            async def aclose(self):
                boundary.close_entered.set()
                await asyncio.get_running_loop().run_in_executor(None, boundary.close_release.wait)
                boundary.close_exited.set()

        async def http(request):
            source = request.headers['x-lp-source']
            if boundary.private_http_payload is not None:
                request.headers['Authorization'] = boundary.private_http_payload
                request.url = request.url.copy_add_param('private', boundary.private_http_payload)
            boundary.requests.append((source, boundary.clock()))
            if source == 'open_orders' and boundary.hold_next:
                boundary.hold_next = False
                if boundary.phase in ('body', 'opaque'):
                    return httpx.Response(200, stream=ResponseBody())
                try:
                    await boundary.held_headers()
                except asyncio.CancelledError:
                    boundary.cancel_count += 1
                    boundary.cancelled.set()
                    raise
            value = {'balance': dict(balance='100000000', allowances={'spender': '100000000'}),
                     'open_orders': dict(data=[], next_cursor='LTE='), 'positions': []}[source]
            return httpx.Response(200, json=value)

        return LPAsyncSourcePort({name: httpx.AsyncClient(transport=httpx.MockTransport(http),
            base_url='http://127.0.0.1', trust_env=False) for name in ('clob', 'data')},
            clock=self.clock, utc_now=self.clock.utc)


def financial(observation='observation-1'):
    return demand('O', kind='financial', sources=FINANCIAL, observation_id=observation)


def plain(value):
    from open_trader.polymarket_lp_diagnostics import diagnostic_json_copy
    return diagnostic_json_copy(value)


def wait_capture(runtime, boundary):
    boundary.wait(lambda: runtime.status()['last_financial_timeout']['capture_state'] != 'pending')


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_financial_timeout_captures_pending_source_wait_before_cancel(phase):
    clock = Clock()
    boundary = WaitBoundary(clock, phase)
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((financial(),))
        assert boundary.entered.wait(4)
        boundary.wait(lambda: runtime.status()['managed_tasks'] == 2)
        original = runtime.status()['scopes'][0]['attempt']
        clock.value = 4.999
        runtime.wake()
        boundary.wait(lambda: runtime.status()['observed_monotonic'] == 4.999)
        assert runtime.drain_results() == ()
        clock.value = 5
        runtime.wake()
        assert boundary.cancelled.wait(4)
        wait_capture(runtime, boundary)
        snapshot = plain(runtime.status())['last_financial_timeout']
        assert snapshot['attempt'] == original
        assert snapshot['observation_id'] == 'observation-1'
        assert snapshot['deadline_monotonic'] == snapshot['timeout_monotonic'] == 5
        assert snapshot['captured_monotonic'] == 5 and snapshot['late'] is False
        assert snapshot['capture_state'] == 'captured'
        sources = {row['source']: row for row in snapshot['sources']}
        assert sources['balance']['state'] == sources['positions']['state'] == 'done'
        pending = sources['open_orders']
        assert pending['state'] == 'pending'
        functions = [f['function'] for f in pending['frames']]
        assert 'held_' + phase in functions
        assert 'aclose' not in functions
        assert ('held_body' if phase == 'headers' else 'held_headers') not in functions
        if phase == 'body':
            assert boundary.close_entered.wait(4)
            assert not boundary.close_exited.is_set()
            assert runtime.status()['scopes'][0]['active']
            assert len(boundary.requests) == 3
        runtime.reconcile(())
        result, = collect(runtime, boundary, lambda rows: bool(rows))
        assert not result.complete and result.reason == 'timeout' and not result.cleanup_fault
        assert result.attempt == original and result.demand.observation_id == 'observation-1'
        assert result.sources['balance'].complete and result.sources['positions'].complete
        assert not result.sources['open_orders'].complete
        assert runtime.drain_results() == () and boundary.cancel_count == 1
    finally:
        boundary.close_release.set()
        runtime.reconcile(())
        assert runtime.close(4)


def test_delayed_loop_capture_keeps_original_timeout_and_attempt_identity():
    clock = Clock()
    boundary = WaitBoundary(clock, 'blocked')
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((financial('old-observation'),))
        assert boundary.entered.wait(4)
        original = runtime.status()['scopes'][0]['attempt']
        clock.value = 5
        runtime.wake()
        # Collection is guarded by real time while the actual I/O loop is still
        # blocked externally. The supervisor must publish without sampling it.
        result, = collect(runtime, boundary, lambda rows: bool(rows))
        assert (result.attempt, result.deadline, result.completed, result.reason) == (original, 5, 5, 'timeout')
        assert not result.complete and not boundary.cancelled.is_set()
        pending = plain(runtime.status())['last_financial_timeout']
        assert pending['capture_state'] == 'pending' and pending['captured_monotonic'] is None
        assert pending['timeout_monotonic'] == pending['deadline_monotonic'] == 5
        for _ in range(3):
            assert plain(runtime.status())['last_financial_timeout'] == pending
        runtime.reconcile((financial('new-observation'),))
        clock.value = 7
        runtime.wake()
        boundary.wait(lambda: runtime.status()['cleanup_faults'] == 1)
        assert not boundary.cancelled.is_set()
        boundary.release.set()
        assert boundary.cancelled.wait(4)
        boundary.wait(lambda: not runtime.status()['scopes'][0]['active'])
        wait_capture(runtime, boundary)
        captured = plain(runtime.status())['last_financial_timeout']
        assert captured['attempt'] == original and captured['observation_id'] == 'old-observation'
        assert captured['deadline_monotonic'] == captured['timeout_monotonic'] == 5
        assert captured['capture_state'] == 'captured' and captured['captured_monotonic'] == 7
        assert captured['late'] is True
        assert runtime.status()['cleanup_faults'] == 0
        # A new attempt gets a new record. Retirement/collection of the old
        # timeout cannot relabel it with the replacement observation identity.
        boundary.phase = 'headers'
        boundary.entered.clear()
        boundary.cancelled.clear()
        boundary.hold_next = True
        cleanup, = runtime.drain_results()
        assert cleanup.attempt == original and cleanup.reason == 'cleanup_fault'
        assert boundary.entered.wait(4)
        boundary.wait(lambda: runtime.status()['managed_tasks'] == 2)
        fresh = runtime.status()['scopes'][0]['attempt']
        assert fresh > original
        clock.value = 12
        runtime.wake()
        assert boundary.cancelled.wait(4)
        wait_capture(runtime, boundary)
        latest = plain(runtime.status())['last_financial_timeout']
        assert latest['attempt'] == fresh and latest['observation_id'] == 'new-observation'
        assert latest['deadline_monotonic'] == latest['timeout_monotonic'] == latest['captured_monotonic'] == 12
        assert latest['late'] is False
        runtime.reconcile(())
        second, = collect(runtime, boundary, lambda rows: bool(rows))
        assert second.attempt == fresh and second.reason == 'timeout' and not second.complete
        assert runtime.drain_results() == ()
        assert runtime.close(4)
        assert plain(runtime.status())['last_financial_timeout'] == latest
    finally:
        boundary.release.set()
        boundary.close_release.set()
        runtime.reconcile(())
        assert runtime.close(4)


def test_financial_timeout_snapshot_is_bounded_detached_and_secret_free():
    import sys
    from types import AsyncGeneratorType

    secret = 'LP439-LOCAL-HEADER-URL-BODY-WALLET-SECRET'
    inspected = []
    auditing = [True]
    async def inert():
        yield None
    sentinel = inert()
    driver = sentinel.__anext__()
    native_driver_type = type(driver)
    driver.close()

    def audit(event, args):
        if auditing[0] and event == 'gc.get_referents':
            inspected.extend(type(obj) for obj in args[0])
    sys.addaudithook(audit)

    class LocalValue:
        def __init__(self):
            self.secret = secret
        def __repr__(self):
            raise AssertionError('must never represent local payload')

    class DeepBoundary(WaitBoundary):
        depth = 20
        def __init__(self, clock):
            super().__init__(clock)
            self.exited_layers = 0
            self.private_http_payload = secret

        async def held_headers(self):
            await self.layer(self.depth)

        async def layer(self, remaining):
            payload = LocalValue()
            try:
                if remaining:
                    await self.layer(remaining - 1)
                else:
                    await self.held_deepest()
            finally:
                self.exited_layers += 1

        async def held_deepest(self):
            self.entered.set()
            await asyncio.Event().wait()

    # External code metadata may have long names/paths; retain bounded basenames
    # and explicitly say when the diagnostic cannot preserve the whole string.
    DeepBoundary.layer.__code__ = DeepBoundary.layer.__code__.replace(
        co_name='deep_' + 'x' * 200, co_filename='/private/' + secret + '/' + 'm' * 200 + '.py')
    clock = Clock()
    boundary = DeepBoundary(clock)
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    previous = 0
    try:
        for number, depth in enumerate((20, 80), start=1):
            boundary.depth = depth
            boundary.entered.clear()
            boundary.cancelled.clear()
            boundary.hold_next = True
            observation = 'observation-' + str(number) + '-' + 'x' * 200
            runtime.reconcile((replace(financial(observation), account=secret),))
            assert boundary.entered.wait(4)
            boundary.wait(lambda: runtime.status()['managed_tasks'] == 2)
            original = runtime.status()['scopes'][0]['attempt']
            assert original > previous
            clock.value = number * 5
            runtime.wake()
            assert boundary.cancelled.wait(4)
            wait_capture(runtime, boundary)
            snapshot = plain(runtime.status())['last_financial_timeout']
            assert snapshot['attempt'] == original
            assert snapshot['observation_id'] == observation[:96]
            assert snapshot['observation_id_truncated'] is True
            assert len(snapshot['sources']) == 3
            row = next(r for r in snapshot['sources'] if r['source'] == 'open_orders')
            assert len(row['frames']) == 16 and row['walked'] <= 64
            assert row['truncated'] is True
            if depth == 20:
                # Deepest frames survive outer HTTPX wrappers and recursive
                # application layers while the terminal native Future is opaque.
                assert any(f['function'] == 'held_deepest' for f in row['frames'])
                assert row['walked'] < 64
            else:
                assert row['walked'] == 64 and row['end'] == 'unknown'
                assert not any(f['function'] == 'held_deepest' for f in row['frames'])
            assert any(f['text_truncated'] for f in row['frames'])
            for frame in row['frames']:
                assert len(frame['file']) <= 64 and len(frame['function']) <= 64
                assert '/' not in frame['file'] and '\\' not in frame['file']
                assert type(frame['line']) is int
            serialized = json.dumps(snapshot, ensure_ascii=False).encode('utf-8')
            assert len(serialized) <= 16 * 1024
            assert secret.encode() not in serialized
            assert b'/private/' not in serialized and b'127.0.0.1' not in serialized
            detached = plain(runtime.status())
            detached['last_financial_timeout']['sources'][0]['frames'].append({'secret': secret})
            detached['last_financial_timeout']['attempt'] = -1
            assert plain(runtime.status())['last_financial_timeout'] == snapshot
            runtime.reconcile(())
            result, = collect(runtime, boundary, lambda rows: bool(rows))
            assert result.attempt == original and result.reason == 'timeout' and not result.complete
            boundary.wait(lambda: not runtime.status()['scopes'])
            assert boundary.exited_layers == sum(d + 1 for d in (20, 80)[:number])
            # The diagnostic itself is fully represented by detached JSON
            # values. Do not equate task/traceback GC timing with source exit.
            assert plain(runtime.status())['last_financial_timeout'] == snapshot
            previous = original
        assert runtime.close(4)
        assert plain(runtime.status())['last_financial_timeout'] == snapshot
        assert inspected and all(t is native_driver_type for t in inspected)
        assert AsyncGeneratorType not in inspected
    finally:
        auditing[0] = False
        boundary.close_release.set()
        runtime.reconcile(())
        assert runtime.close(4)


def test_auto_state_exposes_financial_timeout_without_reads_or_wakeups(tmp_path):
    from decimal import Decimal
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from test_lp_async_live_contract import Trading
    from tests.test_lp_d2_execution_bridge import attach

    writes = []
    class SQLBoundary(PredictionArbitrageStore):
        def _connection(self):
            connection = super()._connection()
            # Observe actual SQLite statements at the external storage edge.
            connection.set_trace_callback(lambda sql: writes.append(sql)
                if sql.lstrip().split(' ', 1)[0].upper() in
                ('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'DROP', 'ALTER') else None)
            return connection

    clock = Clock()
    boundary = WaitBoundary(clock, 'body')
    trading = Trading()
    store = SQLBoundary(tmp_path)
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    live = LPD2LiveExecution(store, trading, account_id='account', venue='polymarket', process_epoch='epoch',
        policy=CalculationPolicy(Decimal(100)), read_runtime=runtime, clock=clock, utc_now=clock.utc)
    live.initialize(ValidatedStartAdmission('epoch', 'offline-permit'))
    producer = PolymarketLPService(store, trading, clock=clock.utc)
    execution = attach(live, producer, tmp_path)
    notices = []
    execution.set_lp_auto_wakeup(lambda: notices.append('wakeup'))
    try:
        runtime.reconcile((financial('pure-state-observation'),))
        assert boundary.entered.wait(4)
        boundary.wait(lambda: runtime.status()['managed_tasks'] == 2)
        clock.value = 5
        runtime.wake()
        assert boundary.cancelled.wait(4) and boundary.close_entered.wait(4)
        # Keep the response close held: cancellation, source exit and further
        # notices cannot race the read-only comparison below.
        boundary.wait(lambda: runtime.status()['observed_monotonic'] == 5 and len(notices) >= 2)
        snapshot = live.state.snapshot()
        wait_capture(runtime, boundary)
        record = plain(runtime.status())['last_financial_timeout']
        baseline = (tuple(boundary.requests), boundary.cancel_count, tuple(trading.cancels),
                    tuple(writes), tuple(notices), tuple(store.lp_sessions()))
        for _ in range(5):
            projected = execution.lp_auto_state(include_intents=False)
            assert projected['diagnostics']['async_reads']['last_financial_timeout'] == record
            assert plain(runtime.status())['last_financial_timeout'] == record
            projected['diagnostics']['async_reads']['last_financial_timeout']['attempt'] = -1
        assert live.state.snapshot() == snapshot
        assert (tuple(boundary.requests), boundary.cancel_count, tuple(trading.cancels),
                tuple(writes), tuple(notices), tuple(store.lp_sessions())) == baseline
        assert not boundary.close_exited.is_set() and boundary.cancel_count == 1
    finally:
        boundary.close_release.set()
        runtime.reconcile(())
        assert live.close(4)


def test_unknown_await_object_preserves_original_cancel_and_close():
    import sys

    inspected = []
    touched = []
    auditing = [True]
    deny = [False]
    async def inert():
        yield None
    sentinel = inert()
    driver = sentinel.__anext__()
    native_driver_type = type(driver)
    driver.close()

    def audit(event, args):
        if auditing[0] and event == 'gc.get_referents':
            inspected.extend(type(obj) for obj in args[0])
            if deny[0]:
                raise RuntimeError('private-diagnostic-audit-sentinel')
    sys.addaudithook(audit)

    class OpaqueDriver:
        # Deliberately spoof native names; type equality must still reject it.
        __module__ = 'builtins'
        def __init__(self):
            future = asyncio.get_running_loop().create_future()
            object.__setattr__(self, '_iterator', future.__await__())
        def __await__(self):
            return self
        def __iter__(self):
            return self
        def __next__(self):
            return next(object.__getattribute__(self, '_iterator'))
        def send(self, value):
            return object.__getattribute__(self, '_iterator').send(value)
        def throw(self, *args):
            return object.__getattribute__(self, '_iterator').throw(*args)
        def __getattribute__(self, name):
            if name not in ('__await__', '__iter__', '__next__', 'send', 'throw'):
                touched.append(name)
                raise AssertionError('opaque attributes are not diagnostic metadata')
            return object.__getattribute__(self, name)
        def __repr__(self):
            touched.append('repr')
            raise AssertionError('opaque repr must not be called')
    OpaqueDriver.__name__ = 'async_generator_asend'

    class OpaqueBoundary(WaitBoundary):
        async def held_body(self):
            self.entered.set()
            await OpaqueDriver()

    try:
        for observation_failure in (False, True):
            deny[0] = observation_failure
            clock = Clock()
            boundary = OpaqueBoundary(clock, 'opaque')
            runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
            runtime.start()
            try:
                runtime.reconcile((financial(),))
                assert boundary.entered.wait(4)
                boundary.wait(lambda: runtime.status()['managed_tasks'] == 2)
                original = runtime.status()['scopes'][0]['attempt']
                clock.value = 5
                runtime.wake()
                assert boundary.cancelled.wait(4) and boundary.close_entered.wait(4)
                wait_capture(runtime, boundary)
                snapshot = plain(runtime.status())['last_financial_timeout']
                if observation_failure:
                    assert snapshot['capture_state'] == 'unavailable'
                    assert snapshot['captured_monotonic'] == 5 and snapshot['late'] is False
                    assert all(not row['frames'] for row in snapshot['sources'])
                else:
                    assert snapshot['capture_state'] == 'captured'
                    row = next(r for r in snapshot['sources'] if r['source'] == 'open_orders')
                    assert row['end'] == 'unknown' and row['state'] == 'pending'
                    assert any(f['function'] == 'held_body' for f in row['frames'])
                assert touched == []
                assert b'private-diagnostic-audit-sentinel' not in json.dumps(snapshot).encode()
                result, = collect(runtime, boundary, lambda rows: bool(rows))
                assert result.attempt == original and result.reason == 'timeout' and not result.complete
                assert not boundary.close_exited.is_set() and len(boundary.requests) == 3
                clock.value = 6
                runtime.wake()
                boundary.wait(lambda: runtime.status()['cleanup_faults'] == 1)
                assert len(boundary.requests) == 3 and boundary.cancel_count == 1
                assert not runtime.status()['new_buy_allowed']
                boundary.close_release.set()
                assert boundary.close_exited.wait(4)
                boundary.wait(lambda: not runtime.status()['scopes'][0]['active'])
                assert runtime.status()['cleanup_faults'] == 0
                fault, = runtime.drain_results()
                assert fault.attempt == original and fault.reason == 'cleanup_fault'
                recovered, = collect(runtime, boundary, lambda rows: any(r.complete for r in rows))
                assert recovered.complete and recovered.attempt > original and recovered.started == 6
                assert all(s.complete for s in recovered.sources.values())
                runtime.reconcile(())
                assert len(boundary.requests) == 6 and boundary.cancel_count == 1
                assert runtime.close(4)
                assert plain(runtime.status())['last_financial_timeout'] == snapshot
                assert touched == [] and all(t is native_driver_type for t in inspected)
            finally:
                boundary.close_release.set()
                runtime.reconcile(())
                assert runtime.close(4)
        assert inspected
    finally:
        auditing[0] = False


def cleanup_record(runtime, source, *, ended=False):
    snapshot = plain(runtime.status())['financial_request_cleanup']
    rows = snapshot['history'] if ended else snapshot['current']
    return next(row for row in reversed(rows) if row['source'] == source)


def test_financial_request_cleanup_records_distinguish_started_and_completed():
    clock = Clock()
    boundary = WaitBoundary(clock, 'body')
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((financial('cleanup-observation'),))
        assert boundary.entered.wait(4)
        boundary.wait(lambda: plain(runtime.status()).get('financial_request_cleanup') is not None)
        running = cleanup_record(runtime, 'open_orders')
        attempt = runtime.status()['scopes'][0]['attempt']
        assert running['attempt'] == attempt and running['outcome'] == 'running'
        assert running['ended_monotonic'] is None
        assert running['cleanup_state'] == 'not_started'
        clock.value = 5
        runtime.wake()
        assert boundary.cancelled.wait(4) and boundary.close_entered.wait(4)
        boundary.wait(lambda: cleanup_record(runtime, 'open_orders')['cleanup_state'] == 'started')
        closing = cleanup_record(runtime, 'open_orders')
        assert closing['cancel_requested_monotonic'] == 5
        assert closing['ended_monotonic'] is None
        assert closing['cleanup_completed_monotonic'] is None
        assert runtime.status()['scopes'][0]['active']
        clock.value = 6
        runtime.wake()
        boundary.wait(lambda: runtime.status()['cleanup_faults'] == 1)
        assert not runtime.status()['new_buy_allowed'] and len(boundary.requests) == 3
        boundary.close_release.set()
        assert boundary.close_exited.wait(4)
        boundary.wait(lambda: not runtime.status()['scopes'][0]['active'])
        boundary.wait(lambda: any(row['source'] == 'open_orders'
            for row in plain(runtime.status())['financial_request_cleanup']['history']))
        finished = cleanup_record(runtime, 'open_orders', ended=True)
        assert finished['attempt'] == attempt and finished['sequence'] == closing['sequence']
        assert finished['cleanup_state'] == 'completed'
        assert finished['cleanup_completed_monotonic'] == finished['ended_monotonic'] == 6
        assert finished['outcome'] == 'cancelled'
        assert runtime.status()['cleanup_faults'] == 0
        notice, = runtime.drain_results()
        assert notice.reason == 'timeout' and notice.attempt == attempt and not notice.complete
        assert runtime.drain_results() == ()
    finally:
        boundary.close_release.set()
        runtime.reconcile(())
        assert runtime.close(4)


def test_financial_request_cleanup_records_are_bounded_detached_and_secret_free():
    from test_lp_async_source_contract import CID
    secret = 'LP455-PRIVATE-URL-ACCOUNT-HEADER-PAYLOAD-EXCEPTION'
    clock = Clock()
    boundary = Boundary(clock)
    entered = threading.Event()
    close_entered = threading.Event()
    close_release = threading.Event()
    closed = threading.Event()
    diagnostic_failures = []
    fail_next_clock = [False]
    requests = []
    positions_pages = []

    def source_clock():
        if fail_next_clock[0]:
            fail_next_clock[0] = False
            diagnostic_failures.append('clock-failed')
            raise RuntimeError(secret)
        return clock()

    class Payload:
        def __repr__(self):
            raise AssertionError('raw trace info is not diagnostic data')

    class HeldBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            try:
                await asyncio.Event().wait()
                yield b''
            finally:
                # External clock failure at the next cleanup observation only.
                fail_next_clock[0] = True
        async def aclose(self):
            close_entered.set()
            await asyncio.get_running_loop().run_in_executor(None, close_release.wait)
            closed.set()
            raise RuntimeError(secret)

    async def http(request):
        source = request.headers['x-lp-source']
        request.headers['authorization'] = secret
        request.url = request.url.copy_add_param('private', secret)
        requests.append(source)
        if source == 'positions':
            positions_pages.append(request.url.params['offset'])
            await request.extensions['trace']('http11.receive_response_headers.started',
                dict(request=Payload(), exception=Payload(), return_value=Payload()))
            if len(positions_pages) == 41:
                return httpx.Response(200, stream=HeldBody())
            return httpx.Response(200, json=[dict(conditionId=CID, asset='yes',
                size='1', avgPrice='0.4', private=secret)] * 100)
        return httpx.Response(200, json=({'balance': dict(balance='100000000',
            allowances={'spender': '100000000'}),
            'open_orders': dict(data=[], next_cursor='LTE=')}[source]))

    def factory(_demand):
        clients = {name: httpx.AsyncClient(transport=httpx.MockTransport(http),
            base_url='http://127.0.0.1', trust_env=False) for name in ('clob', 'data')}
        return LPAsyncSourcePort(clients, clock=source_clock, utc_now=clock.utc)

    runtime = LPReadRuntime(factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((financial(secret),))
        assert entered.wait(4)
        boundary.wait(lambda: plain(runtime.status()).get('financial_request_cleanup') is not None
            and len(plain(runtime.status())['financial_request_cleanup']['history']) == 32
            and any(r['source'] == 'positions' for r in plain(runtime.status())['financial_request_cleanup']['current']))
        snapshot = plain(runtime.status())['financial_request_cleanup']
        current, = snapshot['current']
        assert current['source'] == 'positions' and current['ended_monotonic'] is None
        assert len(snapshot['history']) == 32
        assert len(positions_pages) == 41
        assert positions_pages == [str(100 * i) for i in range(41)]
        assert len(json.dumps(snapshot).encode()) <= 16 * 1024
        assert secret not in json.dumps(snapshot)
        before = tuple(requests)
        for _ in range(5):
            copy = plain(runtime.status())['financial_request_cleanup']
            copy['current'][0]['sequence'] = -1
            copy['history'].append(dict(secret=secret))
            assert plain(runtime.status())['financial_request_cleanup'] == snapshot
        assert tuple(requests) == before
        clock.value = 5
        runtime.wake()
        assert close_entered.wait(4)
        assert diagnostic_failures == ['clock-failed']
        assert not closed.is_set() and runtime.status()['scopes'][0]['active']
        close_release.set()
        assert closed.wait(4)
        boundary.wait(lambda: not runtime.status()['scopes'][0]['active'])
        boundary.wait(lambda: not plain(runtime.status())['financial_request_cleanup']['current'])
        final = plain(runtime.status())['financial_request_cleanup']
        ended = next(row for row in reversed(final['history']) if row['sequence'] == current['sequence'])
        assert ended['outcome'] == 'cancelled' and ended['cleanup_state'] == 'failed'
        assert ended['ended_monotonic'] == 5
        assert len(final['history']) <= 32 and len(json.dumps(final).encode()) <= 16 * 1024
        assert secret not in json.dumps(final)
        notice, = runtime.drain_results()
        assert not notice.complete and notice.reason == 'timeout'
        assert runtime.drain_results() == () and tuple(requests) == before
    finally:
        close_release.set()
        runtime.reconcile(())
        assert runtime.close(4)


def test_financial_request_cleanup_records_preserve_attempt_identity():
    from dataclasses import replace
    clock = Clock()
    boundary = Boundary(clock)
    entered = [threading.Event(), threading.Event()]
    released = [threading.Event(), threading.Event()]
    traces = []
    requests = []

    class Body(httpx.AsyncByteStream):
        def __init__(self, index):
            self.index = index
        async def __aiter__(self):
            entered[self.index].set()
            await asyncio.Event().wait()
            yield b''
        async def aclose(self):
            await asyncio.get_running_loop().run_in_executor(None, released[self.index].wait)

    async def http(request):
        source = request.headers['x-lp-source']
        requests.append(source)
        if source == 'positions':
            index = len(traces)
            traces.append((asyncio.get_running_loop(), request.extensions['trace']))
            return httpx.Response(200, stream=Body(index))
        return httpx.Response(200, json={'balance': dict(balance='100000000',
            allowances={'spender': '100000000'}),
            'open_orders': dict(data=[], next_cursor='LTE=')}[source])

    def factory(_demand):
        clients = {name: httpx.AsyncClient(transport=httpx.MockTransport(http),
            base_url='http://127.0.0.1', trust_env=False) for name in ('data', 'clob')}
        return LPAsyncSourcePort(clients, clock=clock, utc_now=clock.utc)

    runtime = LPReadRuntime(factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((financial('old-observation'),))
        assert entered[0].wait(4)
        first = runtime.status()['scopes'][0]['attempt']
        clock.value = 5
        runtime.wake()
        old, = collect(runtime, boundary, bool)
        assert not old.complete and old.reason == 'timeout' and old.attempt == first
        wait_capture(runtime, boundary)
        timeout = plain(runtime.status())['last_financial_timeout']
        runtime.reconcile((financial('new-observation'),))
        assert len(traces) == 1
        released[0].set()
        assert entered[1].wait(4)
        fresh = runtime.status()['scopes'][0]['attempt']
        assert fresh > first and len(requests) == 6
        boundary.wait(lambda: plain(runtime.status())['financial_request_cleanup']['attempt'] == fresh
            and any(r['source'] == 'positions' for r in
                plain(runtime.status())['financial_request_cleanup']['current']))
        before = plain(runtime.status())['financial_request_cleanup']
        current, = before['current']
        assert current['attempt'] == fresh and current['outcome'] == 'running'
        old_history = [row for row in before['history'] if row['attempt'] == first]
        assert len(old_history) == 3
        async def late():
            for event in ('http11.response_closed.started', 'http11.response_closed.failed',
                          'http11.response_closed.complete'):
                await traces[0][1](event, {'exception': RuntimeError('old-private-payload')})
        asyncio.run_coroutine_threadsafe(late(), traces[0][0]).result(2)
        # Advance the supervisor's observation without advancing the deadline.
        clock.value = 5.5
        runtime.wake()
        boundary.wait(lambda: runtime.status()['observed_monotonic'] == 5.5)
        after = plain(runtime.status())['financial_request_cleanup']
        assert after == before
        assert plain(runtime.status())['last_financial_timeout'] == timeout
        assert runtime.drain_results() == ()
        assert old.demand.observation_id == 'old-observation' and not old.complete
        assert current['ended_monotonic'] is None and len(requests) == 6
    finally:
        for release in released:
            release.set()
        runtime.reconcile(())
        assert runtime.close(4)
