"""Native shadow quality regressions through the actual public read runtime."""
import asyncio
import json
import threading
from decimal import Decimal as D

import websockets.asyncio.client

from test_lp_async_read_runtime_contract import Boundary, Clock, demand
from test_lp_async_source_contract import CID, independent_watchdog
from test_lp_orderbook_ws_487 import wire_book
from open_trader.polymarket_lp_orderbook import book_after_buy_acceptance
from open_trader.polymarket_lp_read_runtime import LPReadRuntime


def test_initial_unsynced_shadow_read_is_explained_unknown(monkeypatch):
    class Socket:
        """External transport with explicitly released frames on its owning loop."""
        close_code = 1000

        def __init__(self):
            self.subscribed = threading.Event()
            self.received = threading.Event()
            self.closed = threading.Event()
            self.sent = []

        async def __aenter__(self):
            self.loop = asyncio.get_running_loop()
            self.frames = asyncio.Queue()
            return self

        async def __aexit__(self, *args):
            await self.close()

        async def send(self, message):
            self.sent.append(message)
            if message != 'PING':
                assert json.loads(message) == dict(type='market', assets_ids=['yes'])
                self.subscribed.set()

        def __aiter__(self):
            return self

        async def __anext__(self):
            frame = await self.frames.get()
            if frame is None:
                raise StopAsyncIteration
            self.received.set()
            return frame

        def publish(self, frame):
            self.loop.call_soon_threadsafe(self.frames.put_nowait, json.dumps(frame))

        async def close(self):
            if not self.closed.is_set():
                self.frames.put_nowait(None)
                self.closed.set()

    socket = Socket()
    connections = []

    def connect(url, **kwargs):
        connections.append(url)
        return socket

    monkeypatch.setattr(websockets.asyncio.client, 'connect', connect)
    clock = Clock()
    boundary = Boundary(clock)
    read = demand(sources=('book',))
    context = dict(key=(read.key, 'baseline-1'), demand_key=read.key,
        condition_id=CID, token_id='yes', price=D('.40'), own_remaining=D(20),
        bid_level=1, baseline_levels=((D('.40'), D(80)),), accepted_at=clock.utc())
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.configure_orderbook('shadow', {'yes': CID}, eligible_keys=(read.key,),
        buy_prices={'yes': (D('.40'),)}, comparison_contexts=(context,))
    runtime.start()
    try:
        assert socket.subscribed.wait(3)
        assert runtime.orderbook_snapshot('yes') is None
        runtime.reconcile((read,))
        boundary.wait(lambda: runtime.status()['ready_count'] == 1)
        first, = runtime.drain_results()
        assert first.complete and first.demand.key == read.key
        assert first.sources['book'].checked_at == clock.utc()
        assert first.sources['book'].value.get('transport') != 'ws'
        assert first.sources['book'].value['bids'] == ({'price': D('.40'), 'size': D(100)},)
        status = runtime.status()['orderbook']
        report = status['shadow']
        assert status['mode'] == 'shadow'
        # REST may seed the cache, but cannot provide post-acceptance WS proof.
        assert book_after_buy_acceptance(runtime.orderbook_snapshot('yes'),
            condition_id=CID, token_id='yes', accepted_at=context['accepted_at']) is not True
        assert report['samples'] == 1 and report['comparison_unknown_samples'] == 1
        assert report['unknown_sample_denominator'] == 1 and report['unknown_fraction'] == 1
        assert report['unexplained_ws_unknown'] == 0
        assert report['unknown_reason_counts']['book_unknown'] > 0
        assert report['book_matches'] == 0 and report['book_pending'] == 1
        assert report['last']['comparable'] is False and report['last']['ws_can_cancel'] is False
        assert report['rest_cancel_observations'] == 0 and report['automatic_enable'] is False

        # Genuine external synchronization precedes the first comparison's 2s deadline.
        clock.value = 1
        socket.publish(wire_book(timestamp=int(clock.utc().timestamp() * 1000)))
        assert socket.received.wait(3)
        boundary.wait(lambda: runtime.orderbook_snapshot('yes') is not None
            and runtime.status()['orderbook']['shadow']['book_matches'] == 1)
        assert runtime.orderbook_snapshot('yes')['bids'][0]['size'] == 100
        clock.value = 5
        runtime.wake()
        boundary.wait(lambda: runtime.status()['ready_count'] == 1)
        second, = runtime.drain_results()
        assert second.complete and second.attempt > first.attempt
        assert second.sources['book'].checked_at == clock.utc()
        assert second.sources['book'].value.get('transport') != 'ws'
        assert second.sources['book'].value['bids'] == first.sources['book'].value['bids']
        status = runtime.status()['orderbook']
        report = status['shadow']
        assert status['mode'] == 'shadow'
        assert report['samples'] == 2 and report['comparison_unknown_samples'] == 1
        assert report['unknown_sample_denominator'] == 2 and report['unknown_fraction'] == .5
        assert report['unexplained_ws_unknown'] == 0
        assert report['unknown_reason_counts']['book_unknown'] > 0
        assert report['max_unknown_segment_seconds'] == 1
        assert report['book_matches'] == 2 and report['book_pending'] == 0
        assert report['book_mismatches'] == 0 and report['rest_cancel_observations'] == 0
        assert report['last']['comparable'] is True and report['automatic_enable'] is False
        assert [event[:3] for event in boundary.events if event[0] == 'entered'] == [
            ('entered', 'B', 'book'), ('entered', 'B', 'book')]
        assert len(connections) == 1
        assert all(message == 'PING' for message in socket.sent[1:])
    finally:
        runtime.reconcile(())
        assert runtime.close(4)
        assert socket.closed.is_set()
        assert runtime.status()['managed_tasks'] == 0
        assert runtime.status()['pool_count'] == 0
