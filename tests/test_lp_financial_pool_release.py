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
