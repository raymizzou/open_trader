"""#487 shadow-FAIL contracts at public value, runtime and execution seams."""
from datetime import timedelta
from decimal import Decimal as D

import pytest

from test_lp_async_read_runtime_contract import Clock
from test_lp_async_source_contract import CID
from test_lp_orderbook_ws_487 import wire_book
from open_trader.polymarket_lp_orderbook import OrderBooks, book_after_buy_acceptance
from open_trader.polymarket_lp_risk import evaluate_lp_bid_levels


@pytest.mark.parametrize('confirmation', ['pong', 'other-token-message'])
def test_quiet_book_confirmation_after_local_acceptance(confirmation):
    clock = Clock()
    books = OrderBooks(clock=clock, utc_now=clock.utc)
    books.subscribe({'yes': CID, 'no': CID})
    books.connected()
    books.handle(wire_book(timestamp='1000', bids=(('.40', '120'),)))
    original = books.market_book('yes')
    clock.value = 1
    accepted_at = clock.utc()
    identity = dict(condition_id=CID, token_id='yes', accepted_at=accepted_at)
    assert book_after_buy_acceptance(books.market_book('yes'), **identity) is not True
    clock.value = 1.001
    if confirmation == 'pong':
        books.proof()  # The public received-PONG observation, not a sent PING.
    else:
        books.handle(wire_book(token='no', timestamp='1001'))
    confirmed = books.market_book('yes')
    assert book_after_buy_acceptance(confirmed, **identity) is True
    assert confirmed['timestamp'] == original['timestamp']
    assert confirmed['source_timestamp'] == D('1000')
    assert evaluate_lp_bid_levels(confirmed, price=D('.40'), own_remaining=D(20),
        bid_level=1, baseline_levels=((D('.40'), D(100)),),
        condition_id=CID, token_id='yes')['state'] == 'monitoring'
    # REST retains its inclusive source-time gate, independent of WS proof.
    rest = dict(confirmed, transport='rest', timestamp=accepted_at)
    assert book_after_buy_acceptance(rest, **identity) is True
    rest['timestamp'] = accepted_at - timedelta(microseconds=1)
    assert book_after_buy_acceptance(rest, **identity) is False


@pytest.mark.parametrize('mode', ['shadow', 'use'])
def test_change_between_baseline_and_acceptance_is_seen(tmp_path, monkeypatch, mode):
    import asyncio
    import json
    from websockets.asyncio.server import serve
    from tests.test_lp_observed_pools import AsyncPoolRig
    from test_lp_orderbook_ws_acceptance_487 import local_rig, publish_book, wait_for

    # Change the external market during the actual offline native submission.
    # No local association, receipt or baseline is installed by the test.
    initialize = AsyncPoolRig.__init__
    def init(rig, *args, **kwargs):
        initialize(rig, *args, **kwargs)
        post = rig.sdk.post_order
        def changed_market(signed):
            rig.upper = D(101)
            rig.changed_at = rig.utc() - timedelta(microseconds=500)
            return post(signed)
        rig.sdk.post_order = changed_market
    monkeypatch.setattr(AsyncPoolRig, '__init__', init)

    async def scenario():
        outbound = asyncio.Queue()
        async def server(ws):
            await ws.recv()
            while (message := await outbound.get()) is not None:
                await ws.send(json.dumps(message))
        async with serve(server, '127.0.0.1', 0) as listener:
            rig = None
            try:
                rig = await local_rig(tmp_path, monkeypatch, mode,
                    f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
                assert rig.original_baseline.bid_levels == ((D('.41'), D(100)), (D('.40'), D(100)))
                assert rig.changed_at < rig.accepted_at
                when = rig.changed_at if mode == 'use' else rig.accepted_at + timedelta(milliseconds=2)
                await publish_book(rig, outbound, when, 101, mode, ws_when=rig.changed_at)
                ws = rig.live.read_runtime.orderbook_snapshot('tA')
                assert ws['timestamp'] == rig.changed_at
                assert book_after_buy_acceptance(ws, condition_id=rig.cid('A'), token_id='tA',
                    accepted_at=rig.accepted_at) is True
                assert evaluate_lp_bid_levels(ws, price=D('.40'), own_remaining=D(20), bid_level=2,
                    baseline_levels=((D('.41'), D(100)), (D('.40'), D(100))),
                    condition_id=rig.cid('A'), token_id='tA')['state'] == 'triggered'
                handles = rig.live.run_protection_once()
                for handle in handles:
                    await asyncio.to_thread(handle.result, 3)
                    await asyncio.to_thread(handle._thread.join, 3)
                    assert handle.done()
                await wait_for(lambda: bool(rig.sdk.cancels))
                assert rig.sdk.cancels == [(rig.oid,)] and len(rig.sdk.posts) == 1
                assert rig.live.state.snapshot().baselines[rig.original_baseline.key] is rig.original_baseline
                rig.live.run_protection_once()
                assert rig.sdk.cancels == [(rig.oid,)]
            finally:
                await outbound.put(None)
                if rig:
                    await asyncio.to_thread(rig.close)
    asyncio.run(scenario())


def test_cached_three_second_old_rest_is_not_recompared(tmp_path):
    import asyncio
    import json
    from websockets.asyncio.server import serve
    from test_lp_orderbook_ws_487 import ws_live, change
    from test_lp_orderbook_ws_acceptance_487 import wait_for

    async def scenario():
        outbound = asyncio.Queue()
        async def server(ws):
            await ws.recv(); await ws.send(json.dumps(wire_book(bids=(('.40', '120'),))))
            while (event := await outbound.get()) is not None:
                await ws.send(json.dumps(event))
        async with serve(server, '127.0.0.1', 0) as listener:
            clock, boundary, trading, live, store, account = ws_live(tmp_path,
                f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}', mode='shadow')
            boundary.book_depth = 120
            store.lp_save_observed_pool_roles(account, expected_revision=0,
                roles={CID: dict(pool='trial', episode='A')})
            try:
                await wait_for(lambda: ('account', CID, 'yes', 'A') in live.state.snapshot().yields
                    and live.read_runtime.orderbook_snapshot('yes') is not None
                    and f'market_book:{CID}:yes' in live.state.snapshot().observation.source_frame.sources,
                    live.run_once)
                live.run_protection_once()
                baseline, = live.state.snapshot().baselines.values()
                before = live.read_runtime.status()['orderbook']['shadow']
                original = live.state.snapshot().observation.source_frame.sources[f'market_book:{CID}:yes']
                clock.value = 3
                await outbound.put(change(size='119', timestamp=1001))
                await wait_for(lambda: live.read_runtime.orderbook_snapshot('yes')['bids'][0]['size'] == 119)
                for _ in range(30):
                    assert live.run_protection_once() == ()
                after = live.read_runtime.status()['orderbook']['shadow']
                assert after['samples'] == before['samples']
                assert after['mismatches'] == before['mismatches']
                assert after['book_mismatches'] == 0 and trading.cancels == []
                cached = live.state.snapshot().observation.source_frame.sources[f'market_book:{CID}:yes']
                assert cached.source_id == original.source_id and cached.checked_at == original.checked_at
                assert cached.value['book']['timestamp'] == original.value['book']['timestamp']
                boundary.book_depth = 119; clock.value = 5; live.read_runtime.wake()
                await wait_for(lambda: (s := live.state.snapshot().observation.source_frame.sources[f'market_book:{CID}:yes'])
                    .checked_at == clock.utc() and s.value['book']['bids'][0]['size'] == 119, live.run_once)
                handles = live.run_protection_once()
                for handle in handles:
                    await asyncio.to_thread(handle.result, 3); await asyncio.to_thread(handle._thread.join, 3)
                await wait_for(lambda: bool(trading.cancels))
                assert trading.cancels == [('A',)]
                assert live.state.snapshot().baselines[baseline.key] is baseline
                assert live.read_runtime.status()['orderbook']['shadow']['samples'] == before['samples'] + 1
            finally:
                await outbound.put(None); assert await asyncio.to_thread(live.close, 4)
    asyncio.run(scenario())


@pytest.mark.parametrize('bids,expected', [
    ((('.41', '100'),), 'unknown'),
    ((('.41', '100'), ('.40', '10')), 'unknown'),
    ((('.41', '100'), ('.40', '100')), 'triggered'),
])
def test_own_order_update_missing_never_monitors(bids, expected):
    clock = Clock()
    books = OrderBooks(clock=clock, utc_now=clock.utc)
    books.subscribe({'yes': CID}); books.connected()
    books.handle(wire_book(bids=bids))
    result = evaluate_lp_bid_levels(books.market_book('yes'), price=D('.40'), own_remaining=D(20),
        bid_level=2, baseline_levels=((D('.41'), D(100)), (D('.40'), D(100))),
        condition_id=CID, token_id='yes')
    assert result['state'] == expected
    assert result['state'] != 'monitoring'


@pytest.mark.parametrize('gap', ['connection', 'membership', 'token-sync', 'runtime-invalidation'])
def test_confirmation_does_not_cross_connection_or_token_gap(gap):
    if gap == 'runtime-invalidation':
        return runtime_invalidation_does_not_replay()
    clock = Clock()
    books = OrderBooks(clock=clock, utc_now=clock.utc)
    books.subscribe({'yes': CID}); books.connected()
    books.handle(wire_book(timestamp='1000'))
    cutoff = clock.utc() + timedelta(seconds=1)
    identity = dict(condition_id=CID, token_id='yes', accepted_at=cutoff)
    retained = books.market_book('yes')
    clock.value = 2
    if gap == 'connection':
        books.disconnected('server_close'); books.connected()
    elif gap == 'membership':
        books.subscribe({}); books.subscribe({'yes': CID})
    else:
        books.handle(dict(event_type='tick_size_change', market=CID, asset_id='yes', timestamp='1001'))
    books.proof()  # PONG while this token is unsynchronized cannot confirm a future seed.
    ticket = books.seed_ticket('yes')
    assert books.seed('yes', wire_book(timestamp='1002'), ticket)
    assert book_after_buy_acceptance(books.market_book('yes'), **identity) is not True
    clock.value = 3; books.proof()
    current = books.market_book('yes')
    assert book_after_buy_acceptance(current, **identity) is True
    assert book_after_buy_acceptance(retained, **identity) is not True
    assert current['source_timestamp'] == D('1002')
    assert current['timestamp'].tzinfo is not None
    for invalid in (None, cutoff, cutoff.replace(tzinfo=None)):
        assert book_after_buy_acceptance(dict(current, confirmation_at=invalid), **identity) is not True
    assert book_after_buy_acceptance(dict(current, confirmation_identity=retained['sync_identity']), **identity) is not True
    clock.value = 13
    assert books.market_book('yes') is not None  # freshness inclusive at 10s
    clock.value = 13.001
    assert books.market_book('yes') is None


def runtime_invalidation_does_not_replay():
    import asyncio
    import json
    from websockets.asyncio.server import serve
    from test_lp_async_read_runtime_contract import Boundary, demand
    from test_lp_orderbook_ws_acceptance_487 import wait_for
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime

    async def scenario():
        clock = Clock(); boundary = Boundary(clock); d = demand(sources=('book',))
        boundary.holds = {('B', 'book')}
        outbound = asyncio.Queue()
        async def server(ws):
            await ws.recv(); await ws.send(json.dumps(wire_book()))
            while (event := await outbound.get()) is not None:
                await ws.send(event if isinstance(event, str) else json.dumps(event))
        async with serve(server, '127.0.0.1', 0) as listener:
            runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            try:
                runtime.configure_orderbook('use', {'yes': CID}, eligible_keys=(d.key,),
                    buy_prices={'yes': (D('.40'),)})
                runtime.start()
                await wait_for(lambda: runtime.orderbook_snapshot('yes') is not None)
                runtime.reconcile((d,))
                await wait_for(lambda: runtime.status()['ready_count'] == 1)
                first, = runtime.drain_results()
                assert first.complete and first.sources['book'].value['transport'] == 'ws'
                clock.value = 1
                await outbound.put(dict(event_type='tick_size_change', market=CID, asset_id='yes', timestamp='1001'))
                await wait_for(lambda: runtime.orderbook_snapshot('yes') is None)
                invalidated, = runtime.drain_orderbook_results()
                assert invalidated.ws_invalidation and not invalidated.complete
                assert runtime.drain_orderbook_results() == ()
                clock.value = 5; runtime.wake()
                await wait_for(lambda: any(e[:3] == ('entered', 'B', 'book') for e in boundary.events))
                await outbound.put('PONG')
                await wait_for(lambda: runtime.status()['orderbook']['last_proof_monotonic'] == 5)
                boundary.release.set()
                await wait_for(lambda: runtime.status()['ready_count'] == 1)
                fresh, = runtime.drain_results()
                assert fresh.complete and not fresh.ws_invalidation
                assert fresh.attempt != first.attempt
                assert fresh.sources['book'].value.get('transport') != 'ws'
                assert fresh.sources['book'].checked_at == clock.utc()
                cutoff = clock.utc() - timedelta(seconds=1)
                identity = dict(condition_id=CID, token_id='yes', accepted_at=cutoff)
                assert book_after_buy_acceptance(runtime.orderbook_snapshot('yes'), **identity) is not True
                clock.value = 5.001; await outbound.put('PONG')
                await wait_for(lambda: book_after_buy_acceptance(runtime.orderbook_snapshot('yes'), **identity) is True)
            finally:
                boundary.release.set(); await outbound.put(None); runtime.reconcile(())
                assert await asyncio.to_thread(runtime.close, 4)
    asyncio.run(scenario())


def comparison_context(demand, *, basis='baseline-1'):
    return dict(key=(demand.key, basis), demand_key=demand.key, condition_id=CID, token_id='yes',
        price=D('.40'), own_remaining=D(20), bid_level=1,
        baseline_levels=((D('.40'), D(80)),), accepted_at=None)


@pytest.mark.parametrize('matching', ['past', 'future', 'quiet-anchor'])
def test_rest_return_matches_recent_ws_versions_in_read_loop(matching):
    import asyncio
    import json
    from websockets.asyncio.server import serve
    from test_lp_async_read_runtime_contract import Boundary, demand
    from test_lp_orderbook_ws_acceptance_487 import wait_for
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime

    async def scenario():
        clock = Clock(); boundary = Boundary(clock); d = demand(sources=('book',))
        outbound = asyncio.Queue()
        async def server(ws):
            await ws.recv()
            await ws.send(json.dumps(wire_book(bids=(('.40', '101' if matching == 'future' else '100'),))))
            while (event := await outbound.get()) is not None:
                await ws.send(event if isinstance(event, str) else json.dumps(event))
        async with serve(server, '127.0.0.1', 0) as listener:
            runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            try:
                runtime.configure_orderbook('shadow', {'yes': CID}, eligible_keys=(d.key,),
                    buy_prices={'yes': (D('.40'),)}, comparison_contexts=(comparison_context(d),))
                runtime.start()
                await wait_for(lambda: runtime.orderbook_snapshot('yes') is not None)
                if matching == 'past':
                    clock.value = 1
                    await outbound.put(wire_book(timestamp='1001', bids=(('.40', '101'),)))
                    await wait_for(lambda: runtime.orderbook_snapshot('yes')['bids'][0]['size'] == 101)
                elif matching == 'quiet-anchor':
                    clock.value = 3
                    await outbound.put('PONG')
                    await wait_for(lambda: runtime.orderbook_snapshot('yes')['confirmation_at'] == clock.utc())
                runtime.reconcile((d,))
                await wait_for(lambda: runtime.status()['ready_count'] == 1)
                report = runtime.status()['orderbook']['shadow']
                if matching == 'future':
                    assert report['book_pending'] == 1 and report['book_mismatches'] == 0
                else:
                    assert report['book_matches'] == 1  # already compared before owner drain
                result, = runtime.drain_results()
                assert result.complete and result.sources['book'].checked_at == clock.utc()
                assert result.sources['book'].value.get('transport') != 'ws'
                assert result.sources['book'].value['bids'][0]['size'] == 100
                if matching == 'future':
                    clock.value = 1
                    await outbound.put(wire_book(timestamp='1001', bids=(('.40', '100'),)))
                    await wait_for(lambda: runtime.status()['orderbook']['shadow']['book_matches'] == 1)
                assert runtime.status()['orderbook']['shadow']['book_mismatches'] == 0
            finally:
                await outbound.put(None); runtime.reconcile(())
                assert await asyncio.to_thread(runtime.close, 4)
    asyncio.run(scenario())


from contextlib import asynccontextmanager


@asynccontextmanager
async def shadow_runtime(*, public_size=100, baseline=80, with_context=True):
    import asyncio
    import json
    from types import SimpleNamespace
    from websockets.asyncio.server import serve
    from test_lp_async_read_runtime_contract import Boundary, demand
    from test_lp_orderbook_ws_acceptance_487 import wait_for
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    clock = Clock(); boundary = Boundary(clock); d = demand(sources=('book',))
    outbound = asyncio.Queue()
    async def server(ws):
        await ws.recv(); await ws.send(json.dumps(wire_book(bids=(('.40', str(public_size)),))))
        while (event := await outbound.get()) is not None:
            await ws.send(event if isinstance(event, str) else json.dumps(event))
    async with serve(server, '127.0.0.1', 0) as listener:
        runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc,
            orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
        context = dict(comparison_context(d), baseline_levels=((D('.40'), D(baseline)),))
        try:
            runtime.configure_orderbook('shadow', {'yes': CID}, eligible_keys=(d.key,),
                buy_prices={'yes': (D('.40'),)}, comparison_contexts=(context,) if with_context else ())
            runtime.start()
            await wait_for(lambda: runtime.orderbook_snapshot('yes') is not None)
            yield SimpleNamespace(clock=clock, boundary=boundary, demand=d, runtime=runtime,
                outbound=outbound, context=context, wait=wait_for)
        finally:
            boundary.release.set(); await outbound.put(None); runtime.reconcile(())
            assert await asyncio.to_thread(runtime.close, 4)


def test_real_book_mismatch_waits_two_seconds_then_fails():
    import asyncio
    async def scenario():
        async with shadow_runtime(public_size=101) as rig:
            runtime = rig.runtime
            runtime.reconcile((rig.demand,))
            await rig.wait(lambda: runtime.status()['ready_count'] == 1)
            result, = runtime.drain_results()
            assert result.sources['book'].value['bids'][0]['size'] == 100
            report = runtime.status()['orderbook']['shadow']
            assert report['book_pending'] == 1 and report['book_mismatches'] == 0
            assert report['assessment'] != 'FAIL'
            rig.clock.value = 1.999; runtime.wake()
            assert runtime.status()['orderbook']['shadow']['book_mismatches'] == 0
            rig.clock.value = 2; runtime.wake()
            await rig.wait(lambda: runtime.status()['orderbook']['shadow']['book_mismatches'] == 1)
            assert runtime.status()['orderbook']['shadow']['assessment'] == 'FAIL'
            runtime.wake()
            assert runtime.status()['orderbook']['shadow']['book_pending'] == 0
            assert runtime.status()['orderbook']['shadow']['book_mismatches'] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('timing', ['early-four', 'late-one', 'boundary-two', 'missed-three', 'queued-boundary-two', 'queued-after-two'])
def test_rest_cancel_matches_ws_early_or_within_two_seconds(timing, monkeypatch):
    import asyncio
    import threading
    received=threading.Event();release=threading.Event();actual_wait=asyncio.wait;receipts=[]
    async def delayed_application(tasks, **options):
        result=await actual_wait(tasks,**options)
        for task in result[0]:
            if task.cancelled() or task.exception() is not None:continue
            item=task.result()
            if (isinstance(item,tuple) and len(item)==5 and isinstance(item[0],dict)
                    and item[0].get('timestamp')=='1002'):
                receipts.append(dict(event=item[0],monotonic=item[1],received_at=item[3].isoformat()))
                received.set()
                while not release.is_set():await asyncio.sleep(.001)
        return result
    async def scenario():
        if timing.startswith('queued-'):monkeypatch.setattr(asyncio,'wait',delayed_application)
        try:
            async with shadow_runtime() as rig:
                runtime = rig.runtime
                runtime.reconcile((rig.demand,))
                await rig.wait(lambda: runtime.status()['ready_count'] == 1)
                runtime.drain_results()
                if timing == 'early-four':
                    rig.clock.value = 1
                    await rig.outbound.put(wire_book(timestamp='1001', bids=(('.40', '99'),)))
                    await rig.wait(lambda: runtime.orderbook_snapshot('yes')['bids'][0]['size'] == 99)
                rig.boundary.book_depth = 99; rig.clock.value = 5; runtime.wake()
                await rig.wait(lambda: runtime.status()['ready_count'] == 1)
                sample, = runtime.drain_results()
                assert sample.sources['book'].value['bids'][0]['size'] == 99
                report = runtime.status()['orderbook']['shadow']
                assert report.get('rest_cancel_observations') == 1
                if timing != 'early-four':
                    assert report['cancel_pending'] == 1 and report['ws_missed_cancels'] == 0
                    if timing == 'missed-three':
                        rig.clock.value = 7; runtime.wake()
                        await rig.wait(lambda: runtime.status()['orderbook']['shadow']['ws_missed_cancels'] == 1)
                        assert runtime.status()['orderbook']['shadow']['assessment'] == 'FAIL'
                    if timing.startswith('queued-'):
                        try:
                            rig.clock.value=7 if timing=='queued-boundary-two' else 7.001
                            await rig.outbound.put(wire_book(timestamp='1002',bids=(('.40','99'),)))
                            await rig.wait(received.is_set)  # real socket receive and real queue, application still held
                            print('queued receive trace:', receipts, 'REST read:',sample.sources['book'].checked_at.isoformat(),
                                'held WS:',dict(runtime.orderbook_snapshot('yes')))
                            assert receipts[0]['monotonic']==(7 if timing=='queued-boundary-two' else 7.001)
                            assert receipts[0]['event']['market']==CID and receipts[0]['event']['asset_id']=='yes'
                            rig.clock.value=7.5;runtime.wake()
                            await rig.wait(lambda:runtime.status()['orderbook']['shadow']['coverage_seconds']>=7.5)
                            held=runtime.status()['orderbook']['shadow']
                            if timing=='queued-boundary-two':
                                assert held['cancel_pending']==1 and held['ws_missed_cancels']==0
                            else:assert held['ws_missed_cancels']==1
                        finally:release.set()
                        await rig.wait(lambda:runtime.orderbook_snapshot('yes')['bids'][0]['size']==99)
                        result=runtime.status()['orderbook']['shadow']
                        if timing=='queued-boundary-two':
                            assert result['cancel_matches']==1 and result['ws_missed_cancels']==0
                            assert result['cancel_timing']['last_seconds']==2
                            assert result['book_mismatches']==0
                        else:assert result['cancel_matches']==0 and result['ws_missed_cancels']==1
                        return
                    # A frozen business clock must not close the deadline before
                    # the real socket has received its frame. Exercise the socket
                    # just inside the window; exact equality is checked below at
                    # the public diagnostic event-arithmetic seam.
                    rig.clock.value = {'late-one': 6, 'boundary-two': 6.999, 'missed-three': 8}[timing]
                    await rig.outbound.put(wire_book(timestamp='1002', bids=(('.40', '99'),)))
                    await rig.wait(lambda: runtime.orderbook_snapshot('yes')['bids'][0]['size'] == 99)
                report = runtime.status()['orderbook']['shadow']
                if timing == 'missed-three':
                    assert report['ws_missed_cancels'] == 1 and report['cancel_matches'] == 0
                else:
                    assert report['cancel_matches'] == 1 and report['ws_missed_cancels'] == 0
                    assert report['cancel_timing']['last_seconds'] == pytest.approx({'early-four': -4, 'late-one': 1, 'boundary-two': 1.999}[timing])
                    if timing == 'boundary-two':
                        from open_trader.polymarket_lp_orderbook import ShadowDiagnostics
                        arithmetic = ShadowDiagnostics(clock=rig.clock)
                        arithmetic.cancel_started()
                        assert arithmetic.cancel_result(2) is True
                        assert arithmetic.snapshot()['cancel_timing']['last_seconds'] == 2
                        assert arithmetic.snapshot()['ws_missed_cancels'] == 0
                        arithmetic.cancel_started()
                        assert arithmetic.cancel_result(2.001) is False
                        assert arithmetic.snapshot()['ws_missed_cancels'] == 1
                for _ in range(30):
                    runtime.compare_shadow('yes', dict(state='triggered'), price=D('.40'), own_remaining=D(20),
                        bid_level=1, baseline_levels=((D('.40'), D(80)),), condition_id=CID,
                        source_id=sample.sources['book'].source_id, context_key=rig.context['key'])
                assert runtime.status()['orderbook']['shadow']['rest_cancel_observations'] == 1
        finally:release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize('arrival', ['one-second', 'absent'])
def test_pending_cancel_observation_survives_order_removal(tmp_path, monkeypatch, arrival):
    import asyncio
    import json
    from websockets.asyncio.server import serve
    from test_lp_orderbook_ws_acceptance_487 import local_rig, publish_book, packet, wait_for
    async def scenario():
        outbound = asyncio.Queue()
        async def server(ws):
            await ws.recv()
            while (event := await outbound.get()) is not None:
                await ws.send(json.dumps(event))
        async with serve(server, '127.0.0.1', 0) as listener:
            rig = None
            try:
                rig = await local_rig(tmp_path, monkeypatch, 'shadow',
                    f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}', manual=True)
                await publish_book(rig, outbound, rig.accepted_at + timedelta(milliseconds=1), 100, 'shadow')
                def initial_proof():
                    snapshot=rig.live.state.snapshot();observation=snapshot.observation
                    source=observation.source_frame.sources.get(f'market_book:{rig.cid("A")}:tA') if observation else None
                    value=source.value.get('book') if source and source.value else None
                    return dict(seconds=rig.seconds,baselines=[b.bid_levels for b in snapshot.baselines.values()],
                        reasons=[t.reason for p in snapshot.progress.values() for t in p.cancel_targets],
                        progress=[p.reason_codes for p in snapshot.progress.values()],
                        openings=[dict(phase=o.phase,review_at=o.review_at.value) for o in snapshot.openings.values()],
                        source_at=source.checked_at if source else None,
                        source_exchange=value.get('timestamp') if value else None,
                        bids=[(r['price'],r['size']) for r in value['bids']] if value else None)
                print('initial protection before',json.dumps(initial_proof(),default=str))
                try:
                    assert rig.live.run_protection_once() == ()
                finally:print('initial protection after',json.dumps(initial_proof(),default=str))
                baseline, = rig.live.state.snapshot().baselines.values()
                assert baseline.bid_levels == ((D('.41'), D(100)), (D('.40'), D(100)))
                rig.live.run_once()  # register this new baseline before waking the next actual REST read
                # Bounded t636u diagnosis: actual external REST entry/return and
                # the public runtime/accepted-source progression, no internals.
                from collections import deque
                from hashlib import sha256
                transport_events = deque(maxlen=32); publication_events = deque(maxlen=32)
                http = rig.http
                async def observe_http(request):
                    if request.headers['x-lp-source'] != 'book':
                        return await http(request)
                    transport_events.append(dict(event='REST_enter', at=rig.seconds))
                    response = await http(request)
                    public = response.json()
                    transport_events.append(dict(event='REST_return', at=rig.seconds,
                        status=response.status_code, timestamp=public.get('timestamp'),
                        bids=public.get('bids', ())[:2]))
                    return response
                rig.http = observe_http
                def observe_publication():
                    status = rig.live.read_runtime.status()
                    observation = rig.live.state.snapshot().observation
                    source = observation.source_frame.sources.get(f'market_book:{rig.cid("A")}:tA') if observation else None
                    book = source.value.get('book') if source and source.value else None
                    row = dict(at=rig.seconds, utc=rig.utc().isoformat(),
                        next_dependency=status['next_dependency_monotonic'], observed=status['observed_monotonic'],
                        ready=status['ready_count'], scopes=[{k:s.get(k) for k in
                            ('kind','active','retiring','attempt','started','deadline','next_start','reason')}
                            for s in status['scopes']], source=None if source is None else dict(
                            identity=sha256(source.source_id.encode()).hexdigest()[:16],
                            checked_at=source.checked_at, complete=source.complete, error=source.error,
                            exchange=book.get('timestamp') if book else None,
                            bids=tuple((v['price'],v['size']) for v in book['bids'][:2]) if book else ()))
                    encoded = json.dumps(row, default=str, sort_keys=True)
                    if not publication_events or publication_events[-1] != encoded:
                        publication_events.append(encoded)
                def drive_setup():
                    rig.live.run_once(); observe_publication()
                before_read = rig.live.read_runtime.status()['orderbook']['shadow']
                rig.upper = D(101); rig.tick(6); rig.book_time = rig.utc()
                try:
                    await wait_for(lambda: (s := rig.live.state.snapshot().observation.source_frame.sources.get(
                        f'market_book:{rig.cid("A")}:tA')) is not None and s.checked_at == rig.utc()
                        and next(v['size'] for v in s.value['book']['bids'] if v['price'] == D('.41')) == 101,
                        drive_setup)
                except TimeoutError:
                    observe_publication()
                    print('case09 bounded setup diagnosis', json.dumps(dict(REST=list(transport_events),
                        publication=[json.loads(v) for v in publication_events],
                        actual_book_requests=[time for source, _, time in rig.requests if source=='book'][-16:],
                        actual_book_returns=[stamp for _, _, stamp in rig.published_books][-16:]), default=str))
                    raise
                at = rig.seconds
                observed = rig.live.read_runtime.status()['orderbook']['shadow']
                assert observed['samples'] == before_read['samples'] + 1
                assert observed['rest_cancel_observations'] == before_read['rest_cancel_observations'] + 1
                assert observed['cancel_pending'] == 1
                handles = rig.live.run_protection_once()
                for handle in handles:
                    await asyncio.to_thread(handle.result, 3); await asyncio.to_thread(handle._thread.join, 3)
                assert rig.sdk.cancels == [(rig.oid,)] and len(rig.sdk.posts) == 1
                assert all(o.key[3] != rig.oid for o in rig.live.state.snapshot().active_orders)
                rig.live.run_once()  # retire actual demand after the native cancellation receipt
                if arrival == 'one-second':
                    rig.tick(1)
                    await outbound.put(packet(rig, rig.utc(), 101))
                    await wait_for(lambda: rig.live.read_runtime.status()['orderbook']['shadow']['cancel_matches'] == 1)
                    assert rig.live.read_runtime.status()['orderbook']['shadow']['cancel_timing']['last_seconds'] == 1
                else:
                    rig.tick(2.001); rig.live.read_runtime.wake()
                    await wait_for(lambda: rig.live.read_runtime.status()['orderbook']['shadow']['ws_missed_cancels'] == 1)
                assert rig.sdk.cancels == [(rig.oid,)] and len(rig.sdk.posts) == 1
                assert rig.live.state.snapshot().baselines[baseline.key] is baseline
                if rig.seconds < at + 2.001:
                    rig.tick(at + 2.001 - rig.seconds)
                rig.live.read_runtime.wake()
                await wait_for(lambda: rig.live.read_runtime.status()['orderbook']['shadow'].get('retired_contexts') == 0)
                if arrival == 'one-second':
                    assert rig.live.read_runtime.status()['orderbook']['shadow']['book_mismatches'] == 0
            finally:
                await outbound.put(None)
                if rig: await asyncio.to_thread(rig.close)
    asyncio.run(scenario())


@pytest.mark.parametrize('third_size', ['120', '999'])
def test_public_bid_comparison_sorts_before_top_n(third_size):
    import asyncio, json, httpx
    from websockets.asyncio.server import serve
    from test_lp_async_source_contract import book
    from test_lp_async_read_runtime_contract import demand
    from test_lp_orderbook_ws_acceptance_487 import wait_for
    from open_trader.polymarket_lp_async_source import LPAsyncSourcePort
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    async def scenario():
        clock = Clock(); d = demand(sources=('book',)); stop = asyncio.Event()
        levels = (('.42', '80'), ('.41', '100'), ('.40', '120'))
        async def server(ws):
            await ws.recv(); await ws.send(json.dumps(wire_book(bids=levels)))
            await stop.wait()
        def factory(_):
            async def http(request):
                return httpx.Response(200, json=dict(book(), timestamp=str(int(clock.utc().timestamp()*1000)),
                    bids=[dict(price='.40',size=third_size), dict(price='.41',size='100'), dict(price='.42',size='80')],
                    asks=[dict(price='.60',size='100')]))
            return LPAsyncSourcePort({key:httpx.AsyncClient(transport=httpx.MockTransport(http),
                base_url='http://127.0.0.1') for key in ('clob','gamma')}, clock=clock, utc_now=clock.utc)
        async with serve(server, '127.0.0.1', 0) as listener:
            runtime = LPReadRuntime(factory, clock=clock, utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            context = dict(comparison_context(d), price=D('.41'), bid_level=2,
                baseline_levels=((D('.42'),D(80)),(D('.41'),D(80))))
            try:
                runtime.configure_orderbook('shadow', {'yes':CID}, eligible_keys=(d.key,),
                    comparison_contexts=(context,))
                runtime.start()
                await wait_for(lambda:runtime.orderbook_snapshot('yes') is not None)
                runtime.reconcile((d,))
                await wait_for(lambda:runtime.status()['ready_count']==1)
                report=runtime.status()['orderbook']['shadow']
                assert report['book_matches']==1 and report['book_mismatches']==0
                result,=runtime.drain_results()
                assert result.complete and result.sources['book'].value['bids'][0]['price']==D('.40')
                assert result.sources['book'].value['bids'][0]['size']==D(third_size)
            finally:
                stop.set();runtime.reconcile(())
                assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(scenario())



def test_rest_read_without_baseline_context_is_visible():
    import asyncio
    async def scenario():
        async with shadow_runtime(with_context=False) as rig:
            runtime=rig.runtime;rig.boundary.holds={('B','book')}
            runtime.reconcile((rig.demand,))
            await rig.wait(lambda:any(event[:3]==('entered','B','book') for event in rig.boundary.events))
            assert runtime.status()['ready_count']==0
            rig.boundary.release.set()
            await rig.wait(lambda:runtime.status()['ready_count']==1)
            report=runtime.status()['orderbook']['shadow']
            assert report.get('context_missing_reads')==1
            assert report['reasons']['missing_comparison_context']==1
            assert report['not_comparable']==1
            assert report['samples']==0 and report['book_matches']==0 and report['rest_cancel_observations']==0
            assert report['assessment']!='PASS'
            result,=runtime.drain_results()
            assert result.complete and result.sources['book'].checked_at==rig.clock.utc()
            assert result.sources['book'].value['timestamp']==rig.clock.utc()
            for _ in range(20):
                runtime.status();assert runtime.drain_results()==()
            assert runtime.status()['orderbook']['shadow']['context_missing_reads']==1
            runtime.configure_orderbook('shadow',{'yes':CID},eligible_keys=(rig.demand.key,),
                buy_prices={'yes':(D('.40'),)},comparison_contexts=(rig.context,))
            rig.clock.value=5;runtime.wake()
            await rig.wait(lambda:runtime.status()['ready_count']==1)
            second,=runtime.drain_results()
            assert second.complete and second.sources['book'].checked_at==rig.clock.utc()
            assert second.sources['book'].source_id!=result.sources['book'].source_id
            report=runtime.status()['orderbook']['shadow']
            assert report['context_missing_reads']==1 and report['samples']==1
            assert report['eligible_rest_reads']==2 and report['book_matches']==1
    asyncio.run(scenario())



@pytest.mark.parametrize('followthrough',['REST-later','short-transient'])
def test_ws_early_and_transient_changes_do_not_fail(followthrough):
    import asyncio
    async def scenario():
        async with shadow_runtime() as rig:
            runtime=rig.runtime
            runtime.reconcile((rig.demand,))
            await rig.wait(lambda:runtime.status()['ready_count']==1)
            runtime.drain_results()
            rig.clock.value=1
            await rig.outbound.put(wire_book(timestamp='1001',bids=(('.40','99'),)))
            await rig.wait(lambda:runtime.orderbook_snapshot('yes')['bids'][0]['size']==99)
            report=runtime.status()['orderbook']['shadow']
            assert report.get('ws_early_events')==1
            assert report['assessment']!='FAIL'
            if followthrough=='short-transient':
                rig.clock.value=2
                await rig.outbound.put(wire_book(timestamp='1002',bids=(('.40','100'),)))
                await rig.wait(lambda:runtime.orderbook_snapshot('yes')['bids'][0]['size']==100)
            else:rig.boundary.book_depth=99
            rig.clock.value=5;runtime.wake()
            await rig.wait(lambda:runtime.status()['ready_count']==1)
            runtime.drain_results()
            report=runtime.status()['orderbook']['shadow']
            assert report['ws_early_events']==1
            assert report['ws_transient_events']==(1 if followthrough=='short-transient' else 0)
            assert report['ws_missed_cancels']==0 and report['book_mismatches']==0
            assert report['assessment']!='FAIL'
            if followthrough=='REST-later':
                assert report['cancel_timing']['last_seconds']==-4
            else:assert report['rest_cancel_observations']==0
    asyncio.run(scenario())



@pytest.mark.parametrize('boundary',['history','capacity','identity','mode-close'])
def test_shadow_history_and_pending_work_are_bounded_and_isolated(boundary):
    import asyncio
    from test_lp_async_read_runtime_contract import demand
    if boundary=='history':
        clock=Clock();books=OrderBooks(clock=clock,utc_now=clock.utc)
        books.subscribe({'yes':CID});books.connected();books.handle(wire_book())
        original=books.market_book('yes')
        for i in range(1,71):
            clock.value=i/1000
            books.handle(wire_book(timestamp=str(1000+i),bids=(('.40',str(100+i)),)))
        report=books.status()
        assert report.get('history_versions')==64
        assert report['history_overflows']>0
        window=books.comparison_versions('yes',since=0,until=clock.value)
        assert window['reason']=='history_capacity' and len(window['versions'])==64
        assert original['bids'][0]['size']==100
        clock.value=3;books.proof()
        assert books.status()['history_versions']==1
        window=books.comparison_versions('yes',since=1,until=3)
        assert window['reason'] is None and len(window['versions'])==1
        books.subscribe({})
        assert books.status()['history_versions']==0
        return
    async def scenario():
        async with shadow_runtime(public_size=99 if boundary=='capacity' else 100) as rig:
            runtime=rig.runtime
            if boundary=='capacity':
                demands=tuple(demand(str(i),sources=('book',)) for i in range(64))
                keys=tuple(d.key for d in demands)
                contexts=tuple(dict(comparison_context(demands[0],basis=str(i)),demand_keys=keys) for i in range(300))
                runtime.configure_orderbook('shadow',{'yes':CID},eligible_keys=keys,comparison_contexts=contexts)
                report=runtime.status()['orderbook']['shadow']
                assert report['active_contexts']<=64 and report['retired_contexts']<=64
                assert report['reasons']['context_capacity']>0
                runtime.reconcile(demands)
                await rig.wait(lambda:runtime.status()['ready_count']==64)
                report=runtime.status()['orderbook']['shadow']
                assert report['pending_observations']<=256
                assert report['reasons']['pending_capacity']>0
                assert report['incomplete_observations']>0 and report['assessment']!='PASS'
            elif boundary=='identity':
                runtime.reconcile((rig.demand,))
                await rig.wait(lambda:runtime.status()['ready_count']==1);runtime.drain_results()
                rig.clock.value=1
                await rig.outbound.put(wire_book(timestamp='1001',bids=(('.40','99'),)))
                await rig.wait(lambda:runtime.orderbook_snapshot('yes')['bids'][0]['size']==99)
                second=demand('C',sources=('book',));rig.boundary.book_depth=99;rig.clock.value=2
                context=dict(comparison_context(second,basis='second-order'),accepted_at=rig.clock.utc())
                runtime.configure_orderbook('shadow',{'yes':CID},eligible_keys=(second.key,),
                    comparison_contexts=(context,))
                runtime.reconcile((second,))
                await rig.wait(lambda:runtime.status()['ready_count']==1);runtime.drain_results()
                report=runtime.status()['orderbook']['shadow']
                assert report['rest_cancel_observations']==1 and report['cancel_matches']==0
                assert report['cancel_pending']==1  # old order's t1 trigger cannot transfer
                rig.clock.value=3;await rig.outbound.put('PONG')
                await rig.wait(lambda:runtime.status()['orderbook']['shadow']['cancel_matches']==1)
                assert runtime.status()['orderbook']['shadow']['cancel_timing']['last_seconds']==1
            else:
                runtime.reconcile((rig.demand,))
                await rig.wait(lambda:runtime.status()['ready_count']==1);runtime.drain_results()
                rig.boundary.book_depth=99;rig.clock.value=5;runtime.wake()
                await rig.wait(lambda:runtime.status()['ready_count']==1);runtime.drain_results()
                assert runtime.status()['orderbook']['shadow']['cancel_pending']==1
                runtime.configure_orderbook('off',{})
                report=runtime.status()['orderbook']['shadow']
                assert report['pending_observations']==0 and report['cancel_pending']==0
                assert report['incomplete_observations']==1 and report['reasons']['mode_changed']==1
                assert report['assessment']!='PASS' and runtime.orderbook_snapshot('yes') is None
    asyncio.run(scenario())



@pytest.mark.parametrize('reason',['server_close','invalid_json','silence','queue_overflow','send_failure','other_exception'])
def test_connection_reasons_are_counted_and_logged_once(monkeypatch,caplog,reason):
    import asyncio,json,logging
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import ClientConnection
    from test_lp_orderbook_ws_acceptance_487 import wait_for
    from open_trader.polymarket_lp_orderbook import OrderBookWebSocket
    caplog.set_level(logging.INFO,logger='open_trader.polymarket_lp_orderbook')
    if reason=='send_failure':
        async def failed_send(self,message,*args,**kwargs):raise OSError('credential-account-order-SECRET')
        monkeypatch.setattr(ClientConnection,'send',failed_send)
    elif reason=='other_exception':
        async def failed_recv(self,*args,**kwargs):raise ArithmeticError('credential-account-order-SECRET')
        monkeypatch.setattr(ClientConnection,'recv',failed_recv)
    async def scenario():
        clock=Clock();books=OrderBooks(clock=clock,utc_now=clock.utc);books.subscribe({'yes':CID})
        close_next=asyncio.Queue();entered=asyncio.Event()
        async def server(ws):
            entered.set()
            if reason=='send_failure':await ws.wait_closed();return
            await ws.recv()
            if reason=='invalid_json':await ws.send('credential-account-order-SECRET')
            elif reason=='queue_overflow':await ws.send(json.dumps([wire_book(),wire_book(timestamp=1001),wire_book(timestamp=1002)]))
            else:
                await ws.send(json.dumps(wire_book()))
                if reason=='server_close':await close_next.get();await ws.close(code=1001)
            await ws.wait_closed()
        async with serve(server,'127.0.0.1',0) as listener:
            client=OrderBookWebSocket(books,url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}',queue_limit=1)
            task=asyncio.create_task(client.run())
            try:
                await asyncio.wait_for(entered.wait(),4)
                if reason in ('server_close','silence'):
                    await wait_for(lambda:books.market_book('yes') is not None)
                    if reason=='server_close':await close_next.put(True)
                    else:clock.value=15
                await wait_for(lambda:books.status()['reason'] is not None)
                report=books.status()
                assert report.get('connection_reason_counts',{}).get(reason)==1
                assert report['disconnects']==1 and report['connections']==1
                if reason=='server_close':
                    for index in range(1,20):
                        await wait_for(lambda:books.status()['next_retry_monotonic'] is not None)
                        clock.value=books.status()['next_retry_monotonic']+.001
                        await wait_for(lambda:books.market_book('yes') is not None)
                        await close_next.put(True)
                        await wait_for(lambda:books.status()['connection_reason_counts']['server_close']==index+1)
                    report=books.status()
                    assert report['disconnects']==20 and report['connections']==20
                    assert len(report['connection_events'])<=16
                    assert report['connection_events'][-1]['code']==1001
                def logs():
                    return [json.loads(r.getMessage().split(' ',1)[1]) for r in caplog.records
                        if r.name=='open_trader.polymarket_lp_orderbook'
                        and r.getMessage().startswith('prediction_lp_orderbook_connection ')]
                await wait_for(lambda:sum(row.get('reason')==reason for row in logs())==report['disconnects'])
                rows=logs()
                assert sum(row.get('reason')==reason for row in rows)==report['disconnects']
                assert 'SECRET' not in json.dumps(rows)
                assert set(report['connection_reason_counts'])<=set(('server_close','invalid_json','silence','queue_overflow','send_failure','other_exception','closed'))
            finally:
                await client.aclose();await asyncio.wait_for(task,4)
    asyncio.run(scenario())



@pytest.mark.parametrize('outcome',['equal','different','rest-older'])
def test_audit_counts_compared_equal_different_and_older_skip(caplog,outcome):
    import asyncio,json,logging
    from websockets.asyncio.server import serve
    from test_lp_async_read_runtime_contract import Boundary,demand
    from test_lp_orderbook_ws_acceptance_487 import wait_for,milliseconds
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    caplog.set_level(logging.INFO,logger='open_trader.polymarket_lp_orderbook')
    async def scenario():
        clock=Clock();boundary=Boundary(clock);outbound=asyncio.Queue()
        demands=(demand('A',sources=('book',)),demand('B',sources=('book',)))
        async def server(ws):
            await ws.recv();await ws.send(json.dumps(wire_book(asks=(('.42','100'),))))
            while (event:=await outbound.get()) is not None:
                await ws.send(event if isinstance(event,str) else json.dumps(event))
        async with serve(server,'127.0.0.1',0) as listener:
            runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            try:
                runtime.configure_orderbook('use',{'yes':CID},eligible_keys=tuple(d.key for d in demands))
                runtime.start();await wait_for(lambda:runtime.orderbook_snapshot('yes') is not None)
                runtime.reconcile(demands)
                await wait_for(lambda:runtime.status()['ready_count']==2);runtime.drain_results()
                for stamp in range(5,60,5):
                    clock.value=stamp;await outbound.put('PONG')
                    await wait_for(lambda:runtime.orderbook_snapshot('yes')['confirmation_at']==clock.utc())
                    runtime.wake();await wait_for(lambda:runtime.status()['ready_count']==2);runtime.drain_results()
                assert not [e for e in boundary.events if e[0]=='entered' and e[2]=='book']
                if outcome=='rest-older':
                    await outbound.put(wire_book(timestamp=milliseconds(clock.utc()+timedelta(seconds=10)),asks=(('.42','100'),)))
                    await wait_for(lambda:runtime.orderbook_snapshot('yes')['timestamp']==clock.utc()+timedelta(seconds=10))
                if outcome=='different':boundary.book_depth=101
                clock.value=60;await outbound.put('PONG')
                await wait_for(lambda:runtime.orderbook_snapshot('yes')['confirmation_at']==clock.utc())
                runtime.wake();await wait_for(lambda:runtime.status()['ready_count']==2);runtime.drain_results()
                report=runtime.status()['orderbook']
                assert report.get('audit_attempts')==1
                assert report['audit_compared']==(0 if outcome=='rest-older' else 1)
                assert report['audit_equal']==(1 if outcome=='equal' else 0)
                assert report['audit_different']==(1 if outcome=='different' else 0)
                assert report['audit_rest_older_skipped']==(1 if outcome=='rest-older' else 0)
                assert len([e for e in boundary.events if e[0]=='entered' and e[2]=='book'])==1
                if outcome=='rest-older':assert runtime.orderbook_snapshot('yes')['timestamp']==clock.utc()+timedelta(seconds=5)
                def audit_logs():
                    return [json.loads(r.getMessage().split(' ',1)[1]) for r in caplog.records
                        if r.name=='open_trader.polymarket_lp_orderbook' and r.getMessage().startswith('prediction_lp_orderbook_audit ')]
                await wait_for(lambda:len(audit_logs())>=2)
                assert {row['reason'] for row in audit_logs()}=={'attempt',{'equal':'equal','different':'different','rest-older':'rest_older_skipped'}[outcome]}
            finally:
                await outbound.put(None);runtime.reconcile(())
                assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(scenario())



def test_five_second_ping_keeps_quiet_books_available():
    import asyncio,json
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed
    from test_lp_orderbook_ws_acceptance_487 import wait_for
    from open_trader.polymarket_lp_orderbook import OrderBookWebSocket
    async def scenario():
        clock=Clock();books=OrderBooks(clock=clock,utc_now=clock.utc);books.subscribe({'yes':CID})
        pong_gate=asyncio.Event();pings=[]
        async def server(ws):
            await ws.recv();await ws.send(json.dumps(wire_book()))
            try:
                async for message in ws:
                    if message=='PING':
                        pings.append(clock.value);await pong_gate.wait();await ws.send('PONG')
            except ConnectionClosed:pass
        async with serve(server,'127.0.0.1',0) as listener:
            client=OrderBookWebSocket(books,url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            task=asyncio.create_task(client.run())
            try:
                await wait_for(lambda:books.market_book('yes') is not None)
                previous=books.market_book('yes')['confirmation_at']
                for stamp in range(5,31,5):
                    pong_gate.clear();clock.value=stamp
                    await wait_for(lambda:pings and pings[-1]==stamp)
                    current=books.market_book('yes')
                    assert current is not None and current['confirmation_at']==previous
                    assert current['source_timestamp']==D(1000)
                    pong_gate.set()
                    await wait_for(lambda:books.market_book('yes')['confirmation_at']==clock.utc())
                    previous=clock.utc()
                assert pings==[5,10,15,20,25,30]
                pong_gate.clear();clock.value=40
                assert books.market_book('yes') is not None
                clock.value=40.001
                assert books.market_book('yes') is None
                clock.value=45
                await wait_for(lambda:books.status()['reason']=='silence')
                assert books.status()['connection_reason_counts']['silence']==1
            finally:
                pong_gate.set();await client.aclose();await asyncio.wait_for(task,4)
    asyncio.run(scenario())



@pytest.mark.parametrize('sink',['normal','raises','blocked'])
def test_shadow_logs_only_public_values_and_sink_failure_isolated(tmp_path,monkeypatch,caplog,sink):
    import asyncio,json,logging,threading
    from websockets.asyncio.server import serve
    from test_lp_orderbook_ws_acceptance_487 import local_rig,publish_book,packet,wait_for
    caplog.set_level(logging.INFO,logger='open_trader.polymarket_lp_read_runtime')
    async def scenario():
        outbound=asyncio.Queue();entered=threading.Event();release=threading.Event()
        async def server(ws):
            await ws.recv()
            while (event:=await outbound.get()) is not None:await ws.send(json.dumps(event))
        async with serve(server,'127.0.0.1',0) as listener:
            rig=None;handler=None;logger=logging.getLogger('open_trader.polymarket_lp_read_runtime')
            try:
                rig=await local_rig(tmp_path,monkeypatch,'shadow',f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}',manual=True)
                await publish_book(rig,outbound,rig.accepted_at+timedelta(milliseconds=1),100,'shadow')
                assert rig.live.run_protection_once()==()
                rig.live.run_once()
                rig.tick(6);rig.book_time=rig.utc()
                await wait_for(lambda:(s:=rig.live.state.snapshot().observation.source_frame.sources.get(
                    f'market_book:{rig.cid("A")}:tA')) is not None and s.checked_at==rig.utc(),rig.live.run_once)
                def rows():
                    return [json.loads(r.getMessage().split(' ',1)[1]) for r in caplog.records
                        if r.name=='open_trader.polymarket_lp_read_runtime'
                        and r.getMessage().startswith('prediction_lp_orderbook_shadow ')]
                await wait_for(lambda:any(row.get('reason')=='book_match' for row in rows()))
                row=next(row for row in reversed(rows()) if row.get('reason')=='book_match')
                assert row['token_prefix']=='tA'
                assert tuple((D(p),D(q)) for p,q in row['rest_bids'])==((D('.41'),D(100)),(D('.40'),D(120)))
                assert tuple((D(p),D(q)) for p,q in row['ws_bids'])==((D('.41'),D(100)),(D('.40'),D(120)))
                assert row['rest_exchange_at'] and row['rest_read_at'] and row['ws_exchange_at']
                assert row['ws_confirmation_at'] and row['ws_read_at'] and row['ws_version']
                assert not any(secret in json.dumps(rows()) for secret in (rig.oid,rig.live.account_id,'credential-SECRET','request-SECRET'))
                old=rig.live.read_runtime.status()['orderbook']['shadow']['last']
                frozen=json.dumps(dict(old),default=str)
                class Handler(logging.Handler):
                    def emit(self,record):
                        if not record.getMessage().startswith('prediction_lp_orderbook_shadow '):return
                        public=json.loads(record.getMessage().split(' ',1)[1])
                        if public.get('reason')!='book_match':return
                        entered.set()
                        if sink=='raises':raise RuntimeError('credential-SECRET account-order-request-SECRET')
                        if sink=='blocked':assert release.wait(4)
                handler=Handler();logger.addHandler(handler)
                rig.upper=D(101);rig.tick(6);rig.book_time=rig.utc()
                await wait_for(lambda:(s:=rig.live.state.snapshot().observation.source_frame.sources.get(
                    f'market_book:{rig.cid("A")}:tA')) is not None and s.checked_at==rig.utc(),rig.live.run_once)
                await outbound.put(packet(rig,rig.utc(),101))
                await wait_for(lambda:(b:=rig.live.read_runtime.orderbook_snapshot('tA')) is not None
                    and next(v['size'] for v in b['bids'] if v['price']==D('.41'))==101)
                assert await asyncio.to_thread(entered.wait,1)
                handles=await asyncio.wait_for(asyncio.to_thread(rig.live.run_protection_once),1)
                for handle in handles:
                    await asyncio.to_thread(handle.result,3);await asyncio.to_thread(handle._thread.join,3)
                assert rig.sdk.cancels==[(rig.oid,)] and len(rig.sdk.posts)==1
                assert json.dumps(dict(old),default=str)==frozen
                assert await asyncio.to_thread(rig.live.close,timeout=3)
            finally:
                release.set()
                if handler:logger.removeHandler(handler)
                await outbound.put(None)
                if rig:await asyncio.to_thread(rig.close)
    asyncio.run(scenario())



@pytest.mark.parametrize('coverage_case',['7199s','29-cancels','qualifies','mode-gap','new-process','early-transient'])
def test_shadow_acceptance_requires_two_hours_and_thirty_rest_cancels(coverage_case):
    from open_trader.polymarket_lp_orderbook import ShadowDiagnostics
    clock=Clock();summary=ShadowDiagnostics(clock=clock);summary.set_mode('shadow')
    count=29 if coverage_case=='29-cancels' else 30
    for number in range(count):
        summary.eligible_read(has_context=True)
        summary.sample(dict(state='triggered'),dict(state='triggered'))
        assert summary.cancel_started(observation_id=str(number)) is True
        summary.cancel_result(0)
    for _ in range(30):
        assert summary.cancel_started(observation_id='0') is False
    for stamp in range(0,7200,10):
        clock.value=stamp;summary.availability(True)
    clock.value=7199 if coverage_case=='7199s' else 7200
    summary.availability(True)
    if coverage_case=='mode-gap':
        summary.set_mode('off');summary.set_mode('shadow');summary.availability(True)
    elif coverage_case=='new-process':
        summary=ShadowDiagnostics(clock=clock);summary.set_mode('shadow');summary.availability(True)
    elif coverage_case=='early-transient':
        summary.ws_excursion();summary.ws_excursion(transient=True)
    report=summary.snapshot()
    assert report['assessment']==('PASS' if coverage_case in ('qualifies','early-transient') else 'PENDING')
    assert report['automatic_enable'] is False
    if coverage_case not in ('mode-gap','new-process'):
        assert report['coverage_seconds']==(7199 if coverage_case=='7199s' else 7200)
        assert report['rest_cancel_observations']==count
    else:assert report['coverage_seconds']==0



@pytest.mark.parametrize('limit',['unknown-2-percent','unknown-over-2-percent','segment15','segment-over15',
    'unexplained','six-disconnects','seven-disconnects','book-mismatch','missed-cancel','audit-mismatch',
    'gap-2-percent','gap-over-2-percent','mode-reset-fault'])
def test_shadow_acceptance_enforces_unknown_and_disconnect_limits(limit):
    from open_trader.polymarket_lp_orderbook import ShadowDiagnostics
    clock=Clock();summary=ShadowDiagnostics(clock=clock);summary.set_mode('shadow')
    missing=2 if limit=='gap-2-percent' else 3 if limit=='gap-over-2-percent' else 0
    unknown=2 if limit=='unknown-2-percent' else 3 if limit=='unknown-over-2-percent' else 0
    for _ in range(missing):summary.eligible_read(has_context=False)
    for index in range(100-missing):
        summary.eligible_read(has_context=True)
        ws=dict(state='unknown',reason_codes=['book_unknown']) if 30<=index<30+unknown else dict(state='triggered' if index<30 else 'monitoring')
        summary.sample(dict(state='triggered' if index<30 else 'monitoring'),ws)
        if index<30:
            summary.cancel_started(observation_id=str(index));summary.cancel_result(0)
    for stamp in range(0,7201,10):
        clock.value=stamp
        if stamp==100 and limit in ('six-disconnects','seven-disconnects'):
            for _ in range(6 if limit=='six-disconnects' else 7):summary.disconnect('server_close')
        bad=stamp in (100,110) and limit in ('segment15','segment-over15','unexplained')
        summary.availability(not bad,reason=None if limit=='unexplained' else 'book_unknown')
        if stamp==110 and limit in ('segment15','segment-over15','unexplained'):
            clock.value=115.001 if limit=='segment-over15' else 115
            summary.availability(True)
    if limit in ('book-mismatch','mode-reset-fault'):
        summary.book_started();summary.book_result(False)
    elif limit=='missed-cancel':
        summary.cancel_started(observation_id='missed');summary.cancel_result(None)
    elif limit=='audit-mismatch':summary.audit_result('different')
    if limit=='mode-reset-fault':summary.set_mode('off');summary.set_mode('shadow');summary.availability(True)
    allowed=limit in ('unknown-2-percent','segment15','six-disconnects','gap-2-percent')
    report=summary.snapshot()
    assert report['assessment']==('PASS' if allowed else 'FAIL')
    if limit in ('unknown-2-percent','gap-2-percent'):
        assert report['unknown_fraction']==pytest.approx(.02)
    if limit in ('six-disconnects','seven-disconnects'):
        assert report['disconnect_hour_exceeded'] is (limit=='seven-disconnects')
    if limit=='unexplained':assert report['unexplained_ws_unknown']>0
    if limit=='segment15':assert report['max_unknown_segment_seconds']==15
    assert report['automatic_enable'] is False



@pytest.mark.parametrize('mode',['off','use'])
def test_off_mode_and_rest_only_acceptance_are_unchanged(mode):
    import asyncio,json
    from websockets.asyncio.server import serve
    from test_lp_async_read_runtime_contract import Boundary,demand
    from test_lp_orderbook_ws_acceptance_487 import wait_for
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    async def scenario():
        clock=Clock();boundary=Boundary(clock);d=demand(sources=('book',));outbound=asyncio.Queue();connections=[]
        async def server(ws):
            connections.append(ws);await ws.recv();await ws.send(json.dumps(wire_book()))
            while (event:=await outbound.get()) is not None:
                await ws.send(event if isinstance(event,str) else json.dumps(event))
        async with serve(server,'127.0.0.1',0) as listener:
            runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            try:
                runtime.configure_orderbook(mode,{'yes':CID},eligible_keys=(d.key,));runtime.start()
                if mode=='use':await wait_for(lambda:runtime.orderbook_snapshot('yes') is not None)
                clock.value=1;cutoff=clock.utc()
                if mode=='use':runtime.configure_orderbook(mode,{'yes':CID},eligible_keys=(d.key,),accepted_cutoffs={d.key:cutoff})
                runtime.reconcile((d,));await wait_for(lambda:runtime.status()['ready_count']==1)
                first,=runtime.drain_results()
                assert first.complete and first.sources['book'].value.get('transport')!='ws'
                assert first.sources['book'].checked_at==cutoff and not first.ws_invalidation
                identity=dict(condition_id=CID,token_id='yes',accepted_at=cutoff)
                assert book_after_buy_acceptance(first.sources['book'].value,**identity) is True
                assert book_after_buy_acceptance(dict(first.sources['book'].value,timestamp=cutoff-timedelta(microseconds=1)),**identity) is False
                if mode=='use':
                    clock.value=2;await outbound.put('PONG')
                    await wait_for(lambda:runtime.status()['ready_count']==1)
                    confirmed,=runtime.drain_results()
                    assert confirmed.complete and confirmed.sources['book'].value['transport']=='ws'
                    assert confirmed.sources['book'].checked_at==clock.utc()
                    assert confirmed.sources['book'].value['source_timestamp']==D(1000)
                clock.value=6;runtime.wake();await wait_for(lambda:runtime.status()['ready_count']==1)
                second,=runtime.drain_results()
                requests=[e for e in boundary.events if e[0]=='entered' and e[2]=='book']
                if mode=='off':
                    assert len(requests)==2 and not connections
                    assert runtime.status()['orderbook']['history_versions']==0
                    assert runtime.status()['orderbook']['shadow']['eligible_rest_reads']==0
                    assert second.sources['book'].value.get('transport')!='ws'
                else:
                    assert len(requests)==1  # healthy confirmed use has no REST dual insurance
                    assert second.sources['book'].value['transport']=='ws'
                    assert second.sources['book'].checked_at==cutoff+timedelta(seconds=1)
                    runtime.configure_orderbook(mode,{'yes':CID},eligible_keys=(),accepted_cutoffs={})
                    clock.value=11;runtime.wake();await wait_for(lambda:runtime.status()['ready_count']==1)
                    ineligible,=runtime.drain_results()
                    assert ineligible.complete and ineligible.sources['book'].value.get('transport')!='ws'
                assert runtime.status()['orderbook']['owned_tasks']<=3
            finally:
                await outbound.put(None);runtime.reconcile(())
                assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(scenario())


@pytest.mark.parametrize('ending',['disconnect','member-retirement','invalid-frame','close','overflow'])
def test_queued_receipt_identity_and_cleanup(monkeypatch,ending):
    import asyncio,threading
    from test_lp_async_read_runtime_contract import demand
    received=threading.Event();release=threading.Event();actual_wait=asyncio.wait
    async def hold_application(tasks,**options):
        result=await actual_wait(tasks,**options)
        for task in result[0]:
            if task.cancelled() or task.exception() is not None:continue
            item=task.result()
            if (isinstance(item,tuple) and len(item)==5 and isinstance(item[0],dict)
                    and item[0].get('timestamp')=='1002'):
                received.set()
                while not release.is_set():await asyncio.sleep(.001)
        return result
    async def scenario():
        monkeypatch.setattr(asyncio,'wait',hold_application)
        try:
            async with shadow_runtime() as rig:
                runtime=rig.runtime;closing=None
                try:
                    runtime.reconcile((rig.demand,))
                    await rig.wait(lambda:runtime.status()['ready_count']==1);runtime.drain_results()
                    rig.boundary.book_depth=99;rig.clock.value=5;runtime.wake()
                    await rig.wait(lambda:runtime.status()['ready_count']==1);runtime.drain_results()
                    before=runtime.status()['orderbook']['shadow']
                    assert before['cancel_pending']==1 and before['cancel_matches']==0
                    rig.clock.value=6
                    bids=(('invalid-price','99'),) if ending=='invalid-frame' else (('.40','99'),)
                    await rig.outbound.put(wire_book(timestamp='1002',bids=bids))
                    await rig.wait(received.is_set)
                    held=runtime.status()['orderbook']
                    assert held['pending_receipts']==1 and held['receipt_capacity']==257
                    assert held['shadow']['book_matches']==before['book_matches']
                    assert held['shadow']['cancel_matches']==0  # receipt alone is not validation
                    assert runtime.orderbook_snapshot('yes')['bids'][0]['size']==100
                    runtime.configure_orderbook('shadow',{},eligible_keys=(),comparison_contexts=())
                    assert runtime.status()['orderbook']['shadow']['retired_contexts']==1
                    if ending=='member-retirement':
                        # Public membership retirement/re-add while the old frame is still held.
                        runtime.configure_orderbook('off',{})
                        assert runtime.status()['orderbook']['pending_receipts']==0
                        second=demand('new-order',sources=('book',));rig.clock.value=6.5
                        context=dict(comparison_context(second,basis='new-baseline'),accepted_at=rig.clock.utc())
                        runtime.configure_orderbook('shadow',{'yes':CID},eligible_keys=(second.key,),comparison_contexts=(context,))
                        runtime.reconcile((second,))
                        await rig.wait(lambda:runtime.status()['ready_count']==1)
                        result,=runtime.drain_results()
                        assert result.complete and result.demand==second
                        assert result.sources['book'].checked_at==rig.clock.utc()
                        release.set()
                        await rig.wait(lambda:runtime.status()['orderbook']['owned_tasks']>=2)
                        assert runtime.status()['orderbook']['shadow']['cancel_matches']==0
                        runtime.configure_orderbook('shadow',{},eligible_keys=(),comparison_contexts=())
                        rig.clock.value=9;runtime.wake()
                    elif ending=='invalid-frame':
                        rig.clock.value=8.5;runtime.wake()
                        await rig.wait(lambda:runtime.status()['orderbook']['shadow']['coverage_seconds']>=8.5)
                        assert runtime.status()['orderbook']['shadow']['retired_contexts']==1
                        assert runtime.status()['orderbook']['shadow']['cancel_pending']==1
                        release.set()
                        await rig.wait(lambda:runtime.orderbook_snapshot('yes') is None)
                    elif ending=='close':
                        closing=asyncio.create_task(asyncio.to_thread(runtime.close,4))
                        await rig.wait(lambda:runtime.status()['orderbook']['pending_receipts']==0)
                        assert runtime.status()['orderbook']['shadow']['retired_contexts']==0
                        assert runtime.status()['orderbook']['shadow']['pending_observations']==0
                        release.set();assert await closing;closing=None
                    else:
                        if ending=='disconnect':await rig.outbound.put(None)
                        else:
                            await rig.outbound.put([wire_book(timestamp=str(1003+i)) for i in range(260)])
                        await rig.wait(lambda:not runtime.status()['orderbook']['connected'])
                        report=runtime.status()['orderbook']
                        assert report['pending_receipts']==0
                        if ending=='overflow':assert report['queue_overflows']==1
                        # No old-generation receipt can retain the frozen work after close.
                        runtime.configure_orderbook('off',{})
                        release.set()
                    await rig.wait(lambda:runtime.status()['orderbook']['shadow']['retired_contexts']==0)
                    final=runtime.status()['orderbook']
                    assert final['pending_receipts']==0 and final['shadow']['pending_observations']==0
                    assert final['shadow']['cancel_matches']==0
                    assert final['shadow']['assessment']!='PASS'
                finally:
                    release.set()
                    if closing is not None:assert await closing
        finally:release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize('timing',['on-time','late-receipt','late-first-proof','earliest-proof','pre-acceptance-proof'])
def test_later_pong_preserves_queued_trigger_evidence_time(monkeypatch,timing):
    import asyncio,threading
    received=threading.Event();release=threading.Event();actual_wait=asyncio.wait
    async def held_apply(tasks,**options):
        result=await actual_wait(tasks,**options)
        for task in result[0]:
            if task.cancelled() or task.exception() is not None:continue
            item=task.result()
            if isinstance(item,tuple) and len(item)==5 and isinstance(item[0],dict) and item[0].get('timestamp')=='1002':
                received.set()
                while not release.is_set():await asyncio.sleep(.001)
        return result
    async def scenario():
        monkeypatch.setattr(asyncio,'wait',held_apply)
        try:
            async with shadow_runtime() as rig:
                runtime=rig.runtime;early=timing in ('late-first-proof','earliest-proof','pre-acceptance-proof')
                runtime.reconcile((rig.demand,))
                await rig.wait(lambda:runtime.status()['ready_count']==1);runtime.drain_results()
                context=rig.context
                if early:
                    rig.clock.value=3
                    context=dict(comparison_context(rig.demand,basis='accepted-later'),accepted_at=rig.clock.utc()+timedelta(seconds=1.5))
                    runtime.configure_orderbook('shadow',{'yes':CID},eligible_keys=(rig.demand.key,),comparison_contexts=(context,))
                    rig.clock.value=4
                    await rig.outbound.put(wire_book(timestamp='1002',bids=(('.40','99'),)))
                    await rig.wait(received.is_set)
                    if timing=='pre-acceptance-proof':
                        rig.clock.value=4.25;await rig.outbound.put('PONG')
                        await rig.wait(lambda:runtime.status()['orderbook']['last_proof_monotonic']==4.25)
                rig.boundary.book_depth=99;rig.clock.value=5;runtime.wake()
                await rig.wait(lambda:runtime.status()['ready_count']==1);runtime.drain_results()
                assert runtime.status()['orderbook']['shadow']['rest_cancel_observations']==1
                if not early:
                    rig.clock.value=7 if timing=='on-time' else 7.001
                    await rig.outbound.put(wire_book(timestamp='1002',bids=(('.40','99'),)))
                    await rig.wait(received.is_set)
                if timing=='earliest-proof':
                    rig.clock.value=6;await rig.outbound.put('PONG')
                    await rig.wait(lambda:runtime.status()['orderbook']['last_proof_monotonic']==6)
                if timing=='pre-acceptance-proof':
                    rig.clock.value=6;release.set()
                    await rig.wait(lambda:runtime.orderbook_snapshot('yes')['source_timestamp']==1002)
                    assert book_after_buy_acceptance(runtime.orderbook_snapshot('yes'),condition_id=CID,
                        token_id='yes',accepted_at=context['accepted_at']) is False
                rig.clock.value=7.5;await rig.outbound.put('PONG')
                await rig.wait(lambda:runtime.status()['orderbook']['last_proof_monotonic']==7.5)
                release.set()
                await rig.wait(lambda:runtime.orderbook_snapshot('yes')['source_timestamp']==1002)
                await rig.wait(lambda:runtime.status()['orderbook']['shadow']['cancel_pending']==0)
                report=runtime.status()['orderbook']['shadow']
                timely=timing in ('on-time','earliest-proof')
                assert report['cancel_matches']==(1 if timely else 0)
                assert report['ws_missed_cancels']==(0 if timely else 1)
                if timely:
                    assert report['cancel_timing']['last_seconds']==(2 if timing=='on-time' else 1)
                    assert report['book_mismatches']==0
                assert runtime.orderbook_snapshot('yes')['source_timestamp']==1002
        finally:release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize('phase',['initial','invalidated','member','connection','runtime'])
def test_initial_sync_preserves_same_batch_pending_receipts(monkeypatch,phase):
    import asyncio,json,threading
    from websockets.asyncio.server import serve
    from test_lp_orderbook_ws_acceptance_487 import wait_for
    from test_lp_async_read_runtime_contract import Boundary,demand
    from open_trader.polymarket_lp_orderbook import OrderBookWebSocket
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    received=threading.Event();release=threading.Event();actual_wait=asyncio.wait
    async def held_second(tasks,**options):
        result=await actual_wait(tasks,**options)
        for task in result[0]:
            if task.cancelled() or task.exception() is not None:continue
            item=task.result()
            if isinstance(item,tuple) and len(item)==5 and isinstance(item[0],dict) and item[0].get('timestamp')=='1001':
                received.set()
                while not release.is_set():await asyncio.sleep(.001)
        return result
    async def scenario():
        monkeypatch.setattr(asyncio,'wait',held_second)
        clock=Clock();accepted=clock.utc();clock.value=4 if phase=='runtime' else 7
        subscribed=asyncio.Event();batch=asyncio.Event();drop=asyncio.Event()
        async def server(ws):
            await ws.recv();subscribed.set();await batch.wait()
            first=wire_book(bids=(('invalid-price','100'),)) if phase=='invalidated' else wire_book()
            await ws.send(json.dumps([first,wire_book(timestamp='1001',bids=(('.40','99'),))]))
            await drop.wait();await ws.close()
        async with serve(server,'127.0.0.1',0) as listener:
            url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}'
            runtime=None;client=None;task=None;updates=[]
            try:
                if phase=='runtime':
                    boundary=Boundary(clock);boundary.book_depth=99;d=demand(sources=('book',))
                    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,orderbook_url=url)
                    context=dict(comparison_context(d),accepted_at=accepted)
                    runtime.configure_orderbook('shadow',{'yes':CID},eligible_keys=(d.key,),comparison_contexts=(context,))
                    runtime.start()
                else:
                    books=OrderBooks(clock=clock,utc_now=clock.utc);books.subscribe({'yes':CID})
                    client=OrderBookWebSocket(books,url=url,on_update=lambda tokens:updates.append(books.status()["pending_receipts"]));task=asyncio.create_task(client.run())
                await asyncio.wait_for(subscribed.wait(),4)
                continuity=books.continuity_ticket('yes') if runtime is None else None
                batch.set();await wait_for(received.is_set)
                if phase=='runtime':
                    assert runtime.orderbook_snapshot('yes')['bids'][0]['size']==100
                    clock.value=5;runtime.reconcile((d,))
                    await wait_for(lambda:runtime.status()['ready_count']==1)
                    row,=runtime.drain_results();assert row.sources['book'].value['bids'][0]['size']==99
                    clock.value=7.5;runtime.wake()
                    await wait_for(lambda:runtime.status()['orderbook']['shadow']['coverage_seconds']>=3.5)
                    report=runtime.status()['orderbook']['shadow']
                    assert report['cancel_pending']==1 and report['ws_missed_cancels']==0
                    release.set()
                    await wait_for(lambda:runtime.status()['orderbook']['shadow']['cancel_matches']==1)
                    report=runtime.status()['orderbook']['shadow']
                    assert report['cancel_timing']['last_seconds']==-1
                    assert report['ws_missed_cancels']==0 and report['book_mismatches']==0
                else:
                    if phase=='initial':
                        assert books.market_book('yes')['bids'][0]['size']==100
                        assert books.status()['pending_receipts']==1
                        assert books.pending_receipt('yes',until=7,continuity=continuity)
                    elif phase=='member':books.subscribe({});books.subscribe({'yes':CID})
                    elif phase=='connection':drop.set();await wait_for(lambda:not books.status()['connected'])
                    if phase!='initial':
                        assert books.status()['pending_receipts']==0
                        assert not books.pending_receipt('yes',until=7,continuity=continuity)
                    release.set()
                    if phase in ('initial','invalidated'):
                        await wait_for(lambda:(b:=books.market_book('yes')) is not None and b['source_timestamp']==1001)
                        if phase=='invalidated':
                            assert book_after_buy_acceptance(books.market_book('yes'),condition_id=CID,
                                token_id='yes',accepted_at=clock.utc()+timedelta(seconds=.1)) is False
                            assert not books.pending_receipt('yes',until=7,continuity=continuity)
                    else:
                        if phase=='member':await wait_for(lambda:len(updates)==2)
                        else:await wait_for(lambda:books.status()['next_retry_monotonic'] is not None)
                        assert books.market_book('yes') is None
                    await wait_for(lambda:books.status()['pending_receipts']==0)
            finally:
                release.set();batch.set();drop.set()
                if runtime is not None:assert await asyncio.to_thread(runtime.close,4)
                if client is not None:
                    await client.aclose();await asyncio.wait_for(task,4)
                    assert client.managed_tasks()==0 and books.status()['pending_receipts']==0
    asyncio.run(scenario())


@pytest.mark.parametrize('phase',['expired','no-expiry','late-first-proof','connection-loss'])
def test_temporary_book_expiry_preserves_qualified_shadow_evidence(monkeypatch,phase):
    import asyncio,threading
    received=threading.Event();release=threading.Event();actual_wait=asyncio.wait
    async def held_apply(tasks,**options):
        result=await actual_wait(tasks,**options)
        for task in result[0]:
            if task.cancelled() or task.exception() is not None:continue
            item=task.result()
            if isinstance(item,tuple) and len(item)==5 and isinstance(item[0],dict) and item[0].get('timestamp')=='1002':
                received.set()
                while not release.is_set():await asyncio.sleep(.001)
        return result
    async def scenario():
        monkeypatch.setattr(asyncio,'wait',held_apply)
        try:
            async with shadow_runtime() as rig:
                try:
                    runtime=rig.runtime
                    runtime.reconcile((rig.demand,))
                    await rig.wait(lambda:runtime.status()['ready_count']==1);runtime.drain_results()
                    rig.clock.value=3
                    context=dict(comparison_context(rig.demand,basis='freshness-proof'),
                        accepted_at=rig.clock.utc()+timedelta(seconds=1.5))
                    runtime.configure_orderbook('shadow',{'yes':CID},eligible_keys=(rig.demand.key,),comparison_contexts=(context,))
                    original=runtime.orderbook_snapshot('yes')
                    rig.clock.value=4
                    await rig.outbound.put(wire_book(timestamp='1002',bids=(('.40','99'),)))
                    await rig.wait(received.is_set)
                    assert runtime.status()['orderbook']['pending_receipts']==1
                    rig.boundary.book_depth=99;rig.clock.value=5;runtime.wake()
                    await rig.wait(lambda:runtime.status()['ready_count']==1)
                    rest,=runtime.drain_results()
                    assert rest.complete and rest.sources['book'].checked_at==rig.clock.utc()
                    assert rest.sources['book'].value.get('transport')!='ws'
                    assert runtime.status()['orderbook']['shadow']['rest_cancel_observations']==1
                    runtime.reconcile(())  # Freeze the one actual REST sample; no later periodic read.
                    if phase!='late-first-proof':
                        rig.clock.value=6;await rig.outbound.put('PONG')
                        await rig.wait(lambda:runtime.status()['orderbook']['last_proof_monotonic']==6)
                        await rig.wait(lambda:runtime.status()['orderbook']['shadow']['coverage_seconds']>=6)
                        proof=runtime.orderbook_snapshot('yes')
                        assert proof['sync_identity']==original['sync_identity']
                        assert book_after_buy_acceptance(proof,condition_id=CID,token_id='yes',
                            accepted_at=context['accepted_at']) is True
                    if phase=='no-expiry':
                        rig.clock.value=12;await rig.outbound.put('PONG')
                        await rig.wait(lambda:runtime.status()['orderbook']['last_proof_monotonic']==12)
                        assert runtime.orderbook_snapshot('yes') is not None
                    else:
                        rig.clock.value=16.001;runtime.wake()
                        await rig.wait(lambda:runtime.status()['orderbook']['shadow']['coverage_seconds']==0)
                        unavailable=runtime.status()['orderbook']
                        assert runtime.orderbook_snapshot('yes') is None
                        assert unavailable['connected'] and unavailable['disconnects']==0
                        assert unavailable['tokens']['items'][0]['ws_available'] is False
                        assert unavailable['shadow']['cancel_matches']==0
                        assert unavailable['shadow']['cancel_pending']==1
                        assert evaluate_lp_bid_levels(runtime.orderbook_snapshot('yes'),price=D('.40'),own_remaining=D(20),
                            bid_level=1,baseline_levels=((D('.40'),D(80)),),condition_id=CID,token_id='yes')['state']=='unknown'
                    if phase=='connection-loss':
                        # Real reader disconnect while both available-book signatures are None.
                        await rig.outbound.put(None)
                        await rig.wait(lambda:not runtime.status()['orderbook']['connected'])
                        await rig.wait(lambda:runtime.status()['orderbook']['shadow']['cancel_pending']==0)
                        assert runtime.orderbook_snapshot('yes') is None
                        assert runtime.status()['orderbook']['generation']!=original['connection_generation']
                        assert runtime.status()['orderbook']['pending_receipts']==0
                        release.set()
                    else:
                        rig.clock.value=18;await rig.outbound.put('PONG')
                        await rig.wait(lambda:runtime.status()['orderbook']['last_proof_monotonic']==18)
                        recovered=runtime.orderbook_snapshot('yes')
                        assert recovered['sync_identity']==original['sync_identity']
                        assert runtime.status()['orderbook']['disconnects']==0
                        release.set()
                        await rig.wait(lambda:(book:=runtime.orderbook_snapshot('yes')) is not None and book['source_timestamp']==1002)
                        await rig.wait(lambda:runtime.status()['orderbook']['shadow']['cancel_pending']==0)
                    report=runtime.status()['orderbook']['shadow']
                    timely=phase in ('expired','no-expiry')
                    assert report['cancel_matches']==(1 if timely else 0)
                    assert report['ws_missed_cancels']==(0 if timely else 1)
                    if timely:assert report['cancel_timing']['last_seconds']==1
                finally:release.set()
        finally:release.set()
    asyncio.run(scenario())
