"""Actual same-pool progress and public LP response-release ownership."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import httpx
import pytest

from test_lp_async_source_contract import independent_watchdog
from test_lp_async_read_runtime_contract import Boundary, Clock, collect, demand
from open_trader.polymarket_lp_async_source import LPAsyncSourcePort, SourceRequest
from open_trader.polymarket_lp_read_runtime import LPReadRuntime


class LocalPositions:
    def __init__(self, before_positions_body=None):
        self.requests = []
        self.completed = []
        self.handlers = []
        self.events = []
        self.changed = threading.Condition()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def setup(self):
                super().setup()
                owner.handlers.append(threading.current_thread())

            def do_GET(self):
                path = self.path.split('?')[0]
                payload = {'/positions': [], '/balance-allowance':
                    dict(balance='100000000', allowances={'spender': '100000000'}),
                    '/data/orders': dict(data=[], next_cursor='LTE=')}[path]
                with owner.changed:
                    owner.requests.append(self.headers['x-lp-source'])
                    number = len(owner.requests)
                    owner.events.append(('request', self.headers['x-lp-source']))
                    owner.changed.notify_all()
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'keep-alive')
                self.end_headers()
                if path == '/positions' and before_positions_body is not None:
                    # Let both sibling financial responses finish before the
                    # controlled positions close phase, independent of arrival.
                    assert before_positions_body.wait(4)
                self.wfile.write(body)
                self.wfile.flush()
                with owner.changed:
                    owner.completed.append(number)
                    owner.changed.notify_all()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def wait_completed(self, count):
        end = time.monotonic() + 2
        with self.changed:
            while len(self.completed) < count:
                remaining = end - time.monotonic()
                assert remaining > 0, self.events
                self.changed.wait(remaining)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        for thread in self.handlers:
            thread.join(2)
        assert not self.thread.is_alive() and all(not t.is_alive() for t in self.handlers)


async def settled(task):
    done, _ = await asyncio.wait((task,), timeout=2)
    assert done, 'legal same-pool request did not complete before independent watchdog'
    return await task


@pytest.mark.parametrize('mode', ('baseline', 'cancel_at_normal_close'))
def test_source_close_cancel_preserves_same_pool_progress(mode):
    async def scenario():
        server = LocalPositions()
        tasks = []
        trace_events = []
        cancels = []
        original = None

        def cancel_original():
            cancels.append(original.cancel('normal-close-checkpoint'))

        async def on_request(request):
            first_request = not tasks or len(tasks) == 1
            async def trace(event, info):
                trace_events.append(event)
                if (first_request and mode == 'cancel_at_normal_close'
                        and event == 'http11.response_closed.started'):
                    # No await: delivery occurs at the real library checkpoint.
                    # Keep the original business owner, even if close has a child task.
                    asyncio.get_running_loop().call_soon(cancel_original)
            request.extensions['trace'] = trace

        client = httpx.AsyncClient(base_url=server.url, timeout=None, trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            event_hooks={'request': [on_request]})
        port = LPAsyncSourcePort({'data': client})
        try:
            for identity in ('first', 'second'):
                task = asyncio.create_task(port.read(SourceRequest('positions', 'offline-account',
                    request_id=identity), time.monotonic() + 5))
                tasks.append(task)
                if identity == 'first':
                    original = task
                if identity == 'first' and mode == 'cancel_at_normal_close':
                    with pytest.raises(asyncio.CancelledError):
                        await settled(task)
                    assert cancels == [True]
                    continue
                result = await settled(task)
                assert result.complete and not result.error and result.value == ()
                assert result.source_id == identity
            await asyncio.to_thread(server.wait_completed, 2)
            assert server.requests == ['positions', 'positions']
            assert server.completed == [1, 2]
            assert trace_events.count('http11.response_closed.complete') == 2
            assert cancels == ([True] if mode == 'cancel_at_normal_close' else [])
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
            await asyncio.to_thread(server.close)
        assert client.is_closed and all(task.done() for task in tasks)

    asyncio.run(scenario())


def test_financial_deadline_close_cancel_preserves_actual_cleanup_and_retry():
    clock = Clock()
    observer = Boundary(clock)
    siblings_closed = threading.Event()
    server = LocalPositions(before_positions_body=siblings_closed)
    close_entered = threading.Event()
    allow_checkpoint = threading.Event()
    close_checkpoint_completed = threading.Event()
    original_ended = threading.Event()
    clients = []
    ports = []
    original_sources = []
    sibling_closes = []

    async def on_request(request):
        first_positions = (request.headers['x-lp-source'] == 'positions'
            and not close_entered.is_set())
        if first_positions:
            original_sources.append(asyncio.current_task())
            def source_ended(_task):
                with server.changed:
                    server.events.append(('source_ended', 'positions'))
                original_ended.set()
            original_sources[0].add_done_callback(source_ended)

        async def trace(event, info):
            if (request.headers['x-lp-source'] in ('balance', 'open_orders')
                    and event == 'http11.response_closed.complete'):
                sibling_closes.append(request.headers['x-lp-source'])
                if {'balance', 'open_orders'} <= set(sibling_closes):
                    siblings_closed.set()
            if not first_positions:
                return
            if event == 'http11.response_closed.started':
                close_entered.set()
                # Coordinate the supervisor's original five-second deadline.
                # This is a synchronous public trace, not a replacement close:
                # after release cancellation lands at the real AnyIO checkpoint.
                assert allow_checkpoint.wait(4)
            elif event == 'http11.response_closed.complete':
                with server.changed:
                    server.events.append(('normal_close_checkpoint_complete', 'positions'))
                close_checkpoint_completed.set()
        request.extensions['trace'] = trace

    def factory(_demand):
        owned = {service: httpx.AsyncClient(base_url=server.url, timeout=None, trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            event_hooks={'request': [on_request]}) for service in ('data', 'clob')}
        clients.extend(owned.values())
        port = LPAsyncSourcePort(owned, clock=clock, utc_now=clock.utc)
        ports.append(port)
        return port

    runtime = LPReadRuntime(factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((demand('O', kind='financial',
            sources=('balance', 'open_orders', 'positions')),))
        assert close_entered.wait(4)
        first = next(s for s in runtime.status()['scopes'] if s['key'][3] == 'financial')['attempt']
        clock.value = 5
        runtime.wake()
        expired = collect(runtime, observer, bool)
        assert len(expired) == 1
        old = expired[0]
        assert (old.attempt, old.started, old.deadline, old.reason) == (first, 0, 5, 'timeout')
        assert not old.complete and not old.sources['positions'].complete
        assert not close_checkpoint_completed.is_set() and not original_ended.is_set()
        assert server.requests.count('positions') == 1
        assert any(s['active'] for s in runtime.status()['scopes'])
        assert runtime.drain_results() == ()
        # The loop cannot run its queued cancellation while trace is paused.
        # The diagnostic may therefore still be pending; it is not a release ACK.
        assert runtime.status()['last_financial_timeout']['attempt'] == first
        allow_checkpoint.set()
        assert close_checkpoint_completed.wait(4)
        assert original_ended.wait(4)
        recovered = collect(runtime, observer, lambda rows: any(r.complete for r in rows))
        assert len(recovered) == 1 and recovered[0].complete
        fresh = recovered[0]
        assert fresh.attempt > first and fresh.started == 5
        assert set(fresh.sources) == {'balance', 'open_orders', 'positions'}
        assert all(r.complete and not r.error for r in fresh.sources.values())
        assert fresh.sources['positions'].source_id != old.sources['positions'].source_id
        assert fresh.sources['positions'].value == ()
        assert len(original_sources) == 1
        assert original_sources[0].cancelled() and original_sources[0].cancelling() == 1
        assert len(ports) == 1 and len(clients) == 2
        server.wait_completed(6)
        assert server.requests.count('positions') == 2
        positions = [i for i, event in enumerate(server.events) if event == ('request', 'positions')]
        checkpoint = server.events.index(('normal_close_checkpoint_complete', 'positions'))
        source_exit = server.events.index(('source_ended', 'positions'))
        assert positions[0] < checkpoint < source_exit < positions[1]
        assert runtime.drain_results() == ()
        assert not any(s['active'] for s in runtime.status()['scopes'])
    finally:
        siblings_closed.set()
        allow_checkpoint.set()
        runtime.reconcile(())
        assert runtime.close(4)
        server.close()
    assert all(client.is_closed for client in clients)


def test_cancelled_read_waits_for_real_close_and_propagates_cancellation():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        exited = asyncio.Event()
        calls = []
        before = asyncio.all_tasks()

        class ResponseStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'[]'

            async def aclose(self):
                calls.append('close')
                entered.set()
                await release.wait()
                exited.set()

        async def http(_request):
            return httpx.Response(200, stream=ResponseStream())

        async with httpx.AsyncClient(transport=httpx.MockTransport(http),
                base_url='http://127.0.0.1', trust_env=False) as client:
            port = LPAsyncSourcePort({'data': client})
            original = asyncio.create_task(port.read(SourceRequest('positions', 'offline'),
                time.monotonic() + 5))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for _ in range(2):
                    original.cancel('owner-cancel')
                    # An explicit loop barrier delivers the pending cancellation.
                    barrier = asyncio.get_running_loop().create_future()
                    asyncio.get_running_loop().call_soon(barrier.set_result, None)
                    await barrier
                    assert not original.done() and not exited.is_set()
                    assert calls == ['close']
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await settled(original)
                assert exited.is_set() and calls == ['close']
                assert asyncio.all_tasks() == before
            finally:
                release.set()
                await asyncio.gather(original, return_exceptions=True)

    asyncio.run(scenario())


def test_response_close_error_never_returns_complete_facts():
    class CloseFailure(Exception):
        pass

    async def scenario(cancel):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        unobserved = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: unobserved.append(context))
        before = asyncio.all_tasks()
        failure = CloseFailure('known-external-close-failure')

        class ResponseStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'[]'

            async def aclose(self):
                calls.append('close')
                entered.set()
                await release.wait()
                raise failure

        async def http(_request):
            return httpx.Response(200, stream=ResponseStream())

        async with httpx.AsyncClient(transport=httpx.MockTransport(http),
                base_url='http://127.0.0.1', trust_env=False) as client:
            port = LPAsyncSourcePort({'data': client})
            original = asyncio.create_task(port.read(SourceRequest('positions', 'offline'),
                time.monotonic() + 5))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if cancel:
                    original.cancel('owner-cancel')
                release.set()
                if cancel:
                    with pytest.raises(asyncio.CancelledError) as raised:
                        await settled(original)
                    assert raised.value.__cause__ is failure
                else:
                    result = await settled(original)
                    assert not result.complete and result.error == 'CloseFailure'
                assert calls == ['close']
                assert asyncio.all_tasks() == before
                assert unobserved == []
            finally:
                release.set()
                await asyncio.gather(original, return_exceptions=True)
                loop.set_exception_handler(None)

    for cancel in (False, True):
        asyncio.run(scenario(cancel))


class ErrorPositions:
    """Real HTTP/1.1 errors; accept and full request arrival are separate facts."""
    def __init__(self, phase, before_error=None):
        self.before_error = before_error
        self.phase = phase
        self.events = []
        self.writers = set()
        self.handlers = set()
        self.positions = 0

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.url = 'http://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
        return self

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.writers.add(writer)
        self.events.append(('tcp_accept', time.monotonic()))
        try:
            while True:
                request = await reader.readuntil(b'\r\n\r\n')
                source = next(line.split(b':', 1)[1].strip().decode()
                    for line in request.split(b'\r\n') if line.lower().startswith(b'x-lp-source:'))
                self.events.append(('full_get', source, time.monotonic()))
                if source == 'positions':
                    self.positions += 1
                    if self.positions == 1 and self.phase in ('headers', 'body'):
                        if self.before_error is not None:
                            assert await asyncio.to_thread(self.before_error.wait, 4)
                        if self.phase == 'headers':
                            writer.write(b'INVALID HTTP RESPONSE\r\n\r\n')
                        else:
                            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n[')
                        await writer.drain()
                        return
                body = json.dumps({'positions': [], 'balance': dict(balance='100000000',
                    allowances={'spender': '100000000'}),
                    'open_orders': dict(data=[], next_cursor='LTE=')}[source]).encode()
                if self.phase == 'limit' and self.positions == 1:
                    body = b'[' + b' ' * 70000 + b']'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode()
                    + b'\r\n\r\n' + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)
            self.handlers.discard(task)

    async def close(self):
        self.server.close()
        for writer in tuple(self.writers):
            writer.close()
        await self.server.wait_closed()
        await asyncio.gather(*tuple(self.handlers), return_exceptions=True)


def wait_functions(task):
    """Native await metadata for failure evidence; no transport or pool state."""
    from open_trader.polymarket_lp_read_runtime import _await_locations
    return [frame['function'] for frame in _await_locations(task)['frames']]


async def error_cleanup_scenario(mode, artifact_dir, repeat=1):
    phase, action = mode.split('_')
    server = await ErrorPositions(phase).start()
    events = []
    tasks = []
    original = None
    first = True

    def cancel_original():
        events.append(('parent_cancel_target', original.get_name()))
        events.append(('parent_cancel_delivery', original.cancel('error-cleanup')))
        if sum(e[0] == 'parent_cancel_delivery' for e in events) < repeat:
            asyncio.get_running_loop().call_soon(cancel_original)

    async def on_request(request):
        nonlocal first
        is_first = first
        first = False
        async def trace(event, _info):
            events.append(('trace', event))
            if is_first and action == 'cancel' and event == 'http11.response_closed.started':
                asyncio.get_running_loop().call_soon(cancel_original)
        request.extensions['trace'] = trace

    client = httpx.AsyncClient(base_url=server.url, timeout=None, trust_env=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        event_hooks={'request': [on_request]})
    port = LPAsyncSourcePort({'data': client})
    try:
        original = asyncio.create_task(port.read(SourceRequest('positions', 'offline-account',
            request_id='error-first'), time.monotonic() + 5), name='original-parent')
        tasks.append(original)
        original.add_done_callback(lambda task: events.append(('parent_exit', task.cancelled())))
        if action == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await settled(original)
        else:
            result = await settled(original)
            assert not result.complete and result.error == 'RemoteProtocolError'
        assert original.done()
        followup = asyncio.create_task(port.read(SourceRequest('positions', 'offline-account',
            request_id='same-pool-next'), time.monotonic() + 5))
        tasks.append(followup)
        done, _ = await asyncio.wait((followup,), timeout=2)
        if not done:
            events.append(('next_request_wait', wait_functions(followup)))
        assert done, events
        result = await followup
        assert result.complete and not result.error and result.value == ()
        assert result.source_id == 'same-pool-next'
        assert server.positions == 2
        assert sum(e[0] == 'full_get' for e in server.events) == 2
        assert ('trace', 'http11.response_closed.complete') in events
        if action == 'cancel':
            assert sum(e == ('parent_cancel_delivery', True) for e in events) == repeat
            assert ('parent_exit', True) in events
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()
        await server.close()
        (artifact_dir / ('real-error-' + mode + '.json')).write_text(
            json.dumps(dict(server=server.events, client=events), indent=2))
    assert all(task.done() for task in tasks)


@pytest.mark.parametrize('mode', ('headers_baseline', 'headers_cancel', 'body_baseline', 'body_cancel'))
def test_source_error_cleanup_preserves_same_pool_progress(mode, tmp_path):
    asyncio.run(error_cleanup_scenario(mode, tmp_path))


def test_request_cleanup_owner_survives_repeated_outer_cancel(tmp_path):
    asyncio.run(error_cleanup_scenario('body_cancel', tmp_path, repeat=2))


class ErrorServerThread:
    def __init__(self, phase, before_error=None):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.server = asyncio.run_coroutine_threadsafe(
            ErrorPositions(phase, before_error).start(), self.loop).result(2)
        self.url = self.server.url

    def close(self):
        asyncio.run_coroutine_threadsafe(self.server.close(), self.loop).result(2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2)
        self.loop.close()
        assert not self.thread.is_alive()


@pytest.mark.parametrize('phase', ('headers', 'body'))
def test_financial_error_cleanup_cancel_preserves_supervisor_recovery(phase):
    clock = Clock()
    observer = Boundary(clock)
    siblings_done = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()
    exited = threading.Event()
    server = ErrorServerThread(phase, siblings_done)
    original = []
    siblings = set()
    ports = []
    clients = []

    async def on_request(request):
        source = request.headers['x-lp-source']
        is_first = source == 'positions' and not original
        if is_first:
            original.append(asyncio.current_task())
            original[0].add_done_callback(lambda _task: exited.set())
        async def trace(event, _info):
            if source in ('balance', 'open_orders') and event == 'http11.response_closed.complete':
                siblings.add(source)
                if len(siblings) == 2:
                    siblings_done.set()
            if is_first and event == 'http11.response_closed.started':
                entered.set()
                assert release.wait(4), 'independent cleanup release watchdog'
            if is_first and event == 'http11.response_closed.complete':
                cleaned.set()
        request.extensions['trace'] = trace

    def factory(_demand):
        owned = {name: httpx.AsyncClient(base_url=server.url, timeout=None, trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            event_hooks={'request': [on_request]}) for name in ('data', 'clob')}
        clients.extend(owned.values())
        port = LPAsyncSourcePort(owned, clock=clock, utc_now=clock.utc)
        ports.append(port)
        return port

    runtime = LPReadRuntime(factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((demand('O', kind='financial',
            sources=('balance', 'open_orders', 'positions')),))
        assert entered.wait(4)
        first = runtime.status()['scopes'][0]['attempt']
        clock.value = 5
        runtime.wake()
        timeout, = collect(runtime, observer, bool)
        assert timeout.attempt == first and timeout.reason == 'timeout' and not timeout.complete
        assert timeout.sources['balance'].complete and timeout.sources['open_orders'].complete
        assert not timeout.sources['positions'].complete
        assert not cleaned.is_set() and not exited.is_set()
        clock.value = 6
        runtime.wake()
        observer.wait(lambda: runtime.status()['cleanup_faults'] == 1)
        fault, = runtime.drain_results()
        assert fault.attempt == first and fault.cleanup_fault
        assert runtime.drain_results() == ()
        assert not runtime.status()['new_buy_allowed']
        assert server.server.positions == 1
        assert len(ports) == 1 and not exited.is_set()
        release.set()
        assert cleaned.wait(4) and exited.wait(4)
        recovered, = collect(runtime, observer, lambda rows: any(row.complete for row in rows))
        assert recovered.complete and recovered.attempt > first and recovered.started == 6
        assert all(row.complete and not row.error for row in recovered.sources.values())
        assert recovered.sources['positions'].value == ()
        assert recovered.sources['positions'].source_id != timeout.sources['positions'].source_id
        assert original[0].cancelled() and original[0].cancelling() == 1
        assert runtime.drain_results() == () and runtime.status()['cleanup_faults'] == 0
        assert server.server.positions == 2 and len(ports) == 1
        observer.wait(lambda: runtime.status()['financial_request_cleanup']['attempt'] == recovered.attempt
            and not runtime.status()['financial_request_cleanup']['current'])
        normal = [row for row in runtime.status()['financial_request_cleanup']['history']
            if row['attempt'] == recovered.attempt]
        assert len(normal) == 3 and all(row['outcome'] == 'complete' for row in normal)
        assert all(row['cancel_requested_monotonic'] is None for row in normal)
    finally:
        siblings_done.set()
        release.set()
        runtime.reconcile(())
        assert runtime.close(4)
        server.close()
    assert all(client.is_closed for client in clients)


def test_business_body_limit_closes_generator_and_preserves_same_pool_progress():
    async def scenario():
        server = await ErrorPositions('limit').start()
        iterations = []
        closes = []
        before = asyncio.all_tasks()

        class ObservedStream(httpx.AsyncByteStream):
            def __init__(self, stream):
                self.stream = stream
            async def __aiter__(self):
                try:
                    async for chunk in self.stream:
                        yield chunk
                finally:
                    iterations.append('iterator_exited')
            async def aclose(self):
                await self.stream.aclose()
                closes.append('response_closed')

        async def on_response(response):
            response.stream = ObservedStream(response.stream)

        async with httpx.AsyncClient(base_url=server.url, timeout=None, trust_env=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                event_hooks={'response': [on_response]}) as client:
            port = LPAsyncSourcePort({'data': client}, response_bytes=1024)
            try:
                failed = await port.read(SourceRequest('positions', 'offline'), time.monotonic() + 5)
                assert not failed.complete and failed.error == 'ReadLimitError'
                assert iterations == ['iterator_exited']
                assert closes == ['response_closed']
                good = await port.read(SourceRequest('positions', 'offline'), time.monotonic() + 5)
                assert good.complete and good.value == ()
                assert server.positions == 2
                assert iterations == ['iterator_exited'] * 2
                assert closes == ['response_closed'] * 2
                assert asyncio.all_tasks() - server.handlers == before
            finally:
                await server.close()
    asyncio.run(scenario())


def test_owned_request_tasks_are_counted_and_drained():
    clock = Clock()
    observer = Boundary(clock)
    lock = threading.Lock()
    entered = set()
    closing = set()
    loops = set()
    releases = {}
    clients = []
    cap = 64
    expected_sources = 4 * cap + 3 + 2 + 4

    async def http(request):
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        with lock:
            loops.add(loop)
            release = releases.setdefault(loop, asyncio.Event())
            entered.add(task)
        try:
            await asyncio.Event().wait()
        finally:
            with lock:
                closing.add(task)
            await release.wait()

    def factory(d):
        services = (('clob',) if d.kind == 'exact' else
            ('clob', 'data') if d.kind == 'financial' else ('clob', 'gamma'))
        owned = {name: httpx.AsyncClient(transport=httpx.MockTransport(http),
            base_url='http://127.0.0.1', trust_env=False,
            limits=httpx.Limits(max_connections=3 if name == 'clob' else 1,
                               max_keepalive_connections=3 if name == 'clob' else 1))
            for name in services}
        clients.extend(owned.values())
        return LPAsyncSourcePort(owned, clock=clock, utc_now=clock.utc)

    async def actual_tasks():
        return asyncio.all_tasks() - {asyncio.current_task()}

    runtime = LPReadRuntime(factory, clock=clock, utc_now=clock.utc, buy_cap=cap)
    assert runtime.status()['managed_tasks'] == 0
    runtime.start()
    try:
        demands = [demand('B' + str(i)) for i in range(cap)]
        demands += [demand('O', kind='financial', sources=('balance', 'open_orders', 'positions')),
            demand('C', kind='exact', sources=('order', 'fills')),
            demand('L', kind='position')]
        runtime.reconcile(demands)
        observer.wait(lambda: len(entered) == expected_sources)
        status = runtime.status()
        observed = set().union(*(asyncio.run_coroutine_threadsafe(actual_tasks(), loop).result(2)
            for loop in tuple(loops)))
        assert entered <= observed
        assert status['managed_tasks'] == len(observed) == expected_sources + cap + 3
        assert status['managed_tasks'] <= 8 * cap + 32
        assert status['pool_count'] == len(clients) == 2 * cap + 5
        assert status['pool_limit'] == 2 * cap + 5
        assert status['connection_limit'] == 4 * cap + 9
        runtime.reconcile(())
        observer.wait(lambda: len(closing) == expected_sources)
        assert not runtime.close(0.01)
        status = runtime.status()
        observed = set().union(*(asyncio.run_coroutine_threadsafe(actual_tasks(), loop).result(2)
            for loop in tuple(loops)))
        assert status['managed_tasks'] == len(observed) == expected_sources + cap + 3
        assert all(not task.done() for task in entered)
    finally:
        runtime.reconcile(())
        for loop, release in tuple(releases.items()):
            loop.call_soon_threadsafe(release.set)
        assert runtime.close(4)
    assert runtime.status()['managed_tasks'] == 0
    assert all(task.done() for task in entered)
    assert all(client.is_closed for client in clients)


class InternalScopeNetwork:
    """Real AnyIO scope at an external HTTPX boundary, with public observations."""
    def __init__(self):
        self.internal_seen = threading.Event()
        self.scope_exited = threading.Event()
        self.network_entered = threading.Event()
        self.network_cancelled = threading.Event()
        self.cleanup_entered = threading.Event()
        self.cleanup_exited = threading.Event()
        self.requests = []
        self.observations = []
        self.loop = None
        self.scope_release = None
        self.response_release = None
        self.cleanup_release = None

    async def http(self, request):
        import anyio
        source = request.headers['x-lp-source']
        self.requests.append(source)
        if source != 'positions' or self.requests.count('positions') > 1:
            return httpx.Response(200, json={'positions': [],
                'balance': dict(balance='100000000', allowances={'spender': '100000000'}),
                'open_orders': dict(data=[], next_cursor='LTE=')}[source])
        self.loop = asyncio.get_running_loop()
        self.scope_release = asyncio.Event()
        self.response_release = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        task = asyncio.current_task()
        try:
            with anyio.CancelScope() as scope:
                scope.cancel()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.observations.append(('internal_delivered', task.cancelling()))
                    self.internal_seen.set()
                    await self.scope_release.wait()
                    self.observations.append(('before_balance', task.cancelling()))
                    raise
            self.observations.append(('after_balance', task.cancelling()))
            self.scope_exited.set()
            self.network_entered.set()
            try:
                await self.response_release.wait()
            except asyncio.CancelledError:
                self.observations.append(('external_delivered', task.cancelling()))
                self.network_cancelled.set()
                raise
            return httpx.Response(200, json=[])
        finally:
            self.cleanup_entered.set()
            await self.cleanup_release.wait()
            self.cleanup_exited.set()

    def release(self, name):
        self.loop.call_soon_threadsafe(getattr(self, name).set)


async def internal_scope_direct_case(overlap):
    boundary = InternalScopeNetwork()
    before = asyncio.all_tasks()
    async with httpx.AsyncClient(transport=httpx.MockTransport(boundary.http),
            base_url='http://127.0.0.1', timeout=None, trust_env=False) as client:
        port = LPAsyncSourcePort({'data': client})
        original = asyncio.create_task(port.read(SourceRequest('positions', 'offline-account'),
            time.monotonic() + 5))
        try:
            assert await asyncio.to_thread(boundary.internal_seen.wait, 2)
            assert boundary.observations[0] == ('internal_delivered', 1)
            if overlap:
                assert original.cancel('independent-outer-cancel')
            # Explicit loop barriers let the public read forward cancellation
            # while the actual internal CancelScope is still balancing.
            for _ in range(3):
                barrier = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(barrier.set_result, None)
                await barrier
            boundary.release('scope_release')
            assert await asyncio.to_thread(boundary.scope_exited.wait, 2)
            after = next(count for event, count in boundary.observations if event == 'after_balance')
            assert after == (1 if overlap else 0)
            assert next(count for event, count in boundary.observations
                if event == 'before_balance') > after + 1
            assert boundary.network_entered.is_set()
            if overlap:
                assert await asyncio.to_thread(boundary.network_cancelled.wait, 2), boundary.observations
                assert boundary.cleanup_entered.is_set() and not boundary.cleanup_exited.is_set()
                assert not original.done()
                boundary.release('cleanup_release')
                with pytest.raises(asyncio.CancelledError):
                    await settled(original)
                assert boundary.cleanup_exited.is_set()
            else:
                assert not boundary.network_cancelled.is_set() and not original.done()
                boundary.release('response_release')
                boundary.release('cleanup_release')
                result = await settled(original)
                assert result.complete and result.value == () and not result.error
                assert not boundary.network_cancelled.is_set()
            assert boundary.requests == ['positions']
        finally:
            for name in ('scope_release', 'response_release', 'cleanup_release'):
                if getattr(boundary, name) is not None:
                    boundary.release(name)
            await asyncio.gather(original, return_exceptions=True)
    assert asyncio.all_tasks() == before


def internal_scope_runtime_case(overlap):
    clock = Clock()
    observer = Boundary(clock)
    boundary = InternalScopeNetwork()
    clients = []

    def factory(_demand):
        owned = {name: httpx.AsyncClient(transport=httpx.MockTransport(boundary.http),
            base_url='http://127.0.0.1', timeout=None, trust_env=False)
            for name in ('data', 'clob')}
        clients.extend(owned.values())
        return LPAsyncSourcePort(owned, clock=clock, utc_now=clock.utc)

    runtime = LPReadRuntime(factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((demand('O', kind='financial',
            sources=('balance', 'open_orders', 'positions')),))
        assert boundary.internal_seen.wait(2)
        assert boundary.observations[0] == ('internal_delivered', 1)
        first = runtime.status()['scopes'][0]['attempt']
        if overlap:
            clock.value = 5
            runtime.wake()
            notice, = collect(runtime, observer, bool)
            assert notice.attempt == first and notice.reason == 'timeout' and not notice.complete
            assert notice.sources['balance'].complete and notice.sources['open_orders'].complete
            assert not notice.sources['positions'].complete
            observer.wait(lambda: runtime.status()['last_financial_timeout']['capture_state'] != 'pending')
            assert runtime.drain_results() == ()
            runtime.reconcile(())
        boundary.release('scope_release')
        assert boundary.scope_exited.wait(2)
        after = next(count for event, count in boundary.observations if event == 'after_balance')
        assert after == (1 if overlap else 0)
        assert next(count for event, count in boundary.observations
            if event == 'before_balance') > after + 1
        assert boundary.network_entered.is_set()
        if overlap:
            assert boundary.network_cancelled.wait(2), boundary.observations
            assert boundary.cleanup_entered.is_set() and not boundary.cleanup_exited.is_set()
            assert runtime.status()['scopes'][0]['active']
            assert runtime.status()['managed_tasks'] == 2
            assert boundary.requests.count('positions') == 1
            assert runtime.drain_results() == ()
            boundary.release('cleanup_release')
            assert boundary.cleanup_exited.wait(2)
            observer.wait(lambda: not runtime.status()['scopes']
                or not runtime.status()['scopes'][0]['active'])
            assert runtime.drain_results() == ()
        else:
            assert not boundary.network_cancelled.is_set()
            boundary.release('response_release')
            boundary.release('cleanup_release')
            normal, = collect(runtime, observer, lambda rows: any(row.complete for row in rows))
            assert normal.attempt == first and normal.complete and not normal.reason
            assert normal.sources['positions'].value == ()
            assert all(row.complete and not row.error for row in normal.sources.values())
            assert not boundary.network_cancelled.is_set()
        assert boundary.requests.count('positions') == 1
    finally:
        for name in ('scope_release', 'response_release', 'cleanup_release'):
            if getattr(boundary, name) is not None:
                boundary.release(name)
        runtime.reconcile(())
        assert runtime.close(4)
    assert all(client.is_closed for client in clients)
    assert runtime.status()['managed_tasks'] == 0


@pytest.mark.parametrize('mode', ('direct', 'runtime'))
def test_internal_cancel_scope_preserves_outer_read_cancellation(mode):
    if mode == 'direct':
        asyncio.run(internal_scope_direct_case(False))
        asyncio.run(internal_scope_direct_case(True))
    else:
        internal_scope_runtime_case(False)
        internal_scope_runtime_case(True)
