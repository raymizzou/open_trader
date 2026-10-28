"""PR489 review regressions at real WS/HTTP and public LP acceptance seams."""
import asyncio
import json
import time
from decimal import Decimal

import pytest
from websockets.asyncio.server import serve

from test_lp_async_source_contract import independent_watchdog, CID
from test_lp_async_read_runtime_contract import Clock, Boundary, demand
from test_lp_orderbook_ws_487 import wire_book, change
from open_trader.polymarket_lp_orderbook import OrderBooks, OrderBookWebSocket
from open_trader.polymarket_lp_read_runtime import LPReadRuntime


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.001)


def test_receive_deadline_never_loses_a_dequeued_update(monkeypatch):
    async def scenario():
        advance=asyncio.Event();later=asyncio.Event();applied=[];consumed=[];queues=[];stalls=[]
        async def server(ws):
            await ws.recv();await ws.send(json.dumps(wire_book()))
            await advance.wait();await ws.send(json.dumps(change(timestamp=1100,size='30')))
            await later.wait();await ws.send(json.dumps(change(timestamp=1101,size='31')))
            await ws.wait_closed()
        value=OrderBooks();value.subscribe({'yes':CID})
        def observed(tokens):
            snapshot=value.snapshot('yes')
            if snapshot is not None:applied.append(str(snapshot['timestamp']))
        async with serve(server,'127.0.0.1',0) as listener:
            client=OrderBookWebSocket(value,url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}',on_update=observed)
            actual_put=asyncio.Queue.put_nowait
            def instrument_put(queue,item):
                actual_put(queue,item)  # real queue semantics, no replaced get/timer/cancellation
                if (isinstance(item,tuple) and len(item)==5 and isinstance(item[0],dict)
                        and item[0].get('timestamp')=='1100' and not stalls):
                    queues.append(queue)
                    getter=client._waiter  # observation only; never alter this real task
                    def completed(task):
                        if not task.cancelled() and task.exception() is None:
                            consumed.append(str(task.result()[0]['timestamp']))
                    getter.add_done_callback(completed)
                    start=time.monotonic();time.sleep(.02484)
                    stalls.append(time.monotonic()-start)
            monkeypatch.setattr(asyncio.Queue,'put_nowait',instrument_put)
            task=asyncio.create_task(client.run())
            try:
                await until(lambda:value.snapshot('yes') is not None)
                assert value.snapshot('yes')['bids'][0][1]==100
                advance.set();await until(lambda:bool(consumed) or not value.status()['connected'])
                await asyncio.sleep(0)  # allow the original parent waiter to settle
                snapshot=value.snapshot('yes')
                trace=dict(consumed=consumed,applied=applied,empty=queues[0].empty(),
                    connected=value.status()['connected'],snapshot_timestamp=str(snapshot['timestamp']) if snapshot else None,
                    stalls=stalls)
                print('receive-deadline trace:',json.dumps(trace))
                assert snapshot is None or snapshot['timestamp']==1100,trace
                later.set()
                await until(lambda:value.snapshot('yes') is None or value.snapshot('yes')['timestamp']==1101)
                if value.snapshot('yes') is not None:
                    assert applied.count('1100')==1 and applied.count('1101')==1
            finally:
                advance.set();later.set();await client.aclose();await asyncio.wait_for(task,3)
            assert client.closed and client.managed_tasks()==0
            assert not [t for t in asyncio.all_tasks() if not t.done() and
                getattr(t.get_coro(),'__qualname__','')=='Queue.get']
    asyncio.run(scenario())


@pytest.mark.parametrize('pending',[True,False],ids=['pending-rest','accepted-rest'])
def test_ws_outage_preserves_pending_and_accepted_rest_books(pending):
    from test_lp_async_state_contract import state,financial,row
    async def scenario():
        clock=Clock();boundary=Boundary(clock);boundary.book_depth=30
        owner=state(clock);financial(owner,clock,(row('A'),))
        sockets=[];drop=asyncio.Event();tick=asyncio.Event();ticked=asyncio.Event()
        async def server(ws):
            sockets.append(ws);await ws.recv()
            if len(sockets)==1:
                await ws.send(json.dumps(wire_book()))
                await drop.wait();await ws.close()
            else:
                await tick.wait()
                await ws.send(json.dumps(dict(event_type='tick_size_change',market=CID,asset_id='yes',timestamp='1100')))
                ticked.set();await ws.wait_closed()
        async with serve(server,'127.0.0.1',0) as listener:
            runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            d=demand('A');runtime.start();runtime.configure_orderbook('use',{'yes':CID},eligible_keys=(d.key,))
            try:
                await until(lambda:runtime.status()['orderbook']['synced']==1)
                runtime.reconcile((d,));await until(lambda:runtime.status()['ready_count']==1)
                initial,=runtime.drain_results();assert owner.apply_market_attempt(initial)=='applied'
                drop.set();await until(lambda:not runtime.status()['orderbook']['connected'])
                await until(lambda:runtime.status()['orderbook']['next_retry_monotonic'] is not None)
                clock.value=runtime.status()['orderbook']['next_retry_monotonic']
                await until(lambda:len(sockets)==2)
                assert runtime.orderbook_snapshot('yes') is None
                clock.value=5;runtime.wake();await until(lambda:runtime.status()['ready_count']==1)
                if not pending:
                    rest,=runtime.drain_results();assert rest.complete
                    assert owner.apply_market_attempt(rest)=='applied'
                    accepted=owner.snapshot()
                tick.set();await ticked.wait()
                await until(lambda:runtime.orderbook_snapshot('yes') is None)
                notices=runtime.drain_orderbook_results()
                # A pending REST mailbox stays intact while the previously accepted WS fact loses confidence.
                assert all(n.ws_invalidation and not n.complete for n in notices)
                if not pending:assert notices==()
                for notice in notices:assert owner.apply_market_attempt(notice)=='applied'
                if pending:
                    rest,=runtime.drain_results();assert rest.complete
                    assert owner.apply_market_attempt(rest)=='applied'
                    accepted=owner.snapshot()
                assert rest.sources['book'].value.get('transport') is None
                assert rest.sources['book'].value['bids'][0]['size']==30
                assert rest.sources['book'].checked_at==clock.utc()
                assert owner.snapshot()==accepted
                assert owner.snapshot().read_failures[('account',CID,'yes','A')].consecutive_failures==0
                assert owner.apply_market_attempt(initial)=='stale'
                boundary.mode[('A','book')]='failure';clock.value=10;runtime.wake()
                await until(lambda:runtime.status()['ready_count']==1)
                failed,=runtime.drain_results()
                assert not failed.complete and failed.sources['book'].error!='ws_invalidated'
                assert owner.apply_market_attempt(failed)=='applied'
                assert owner.snapshot().read_failures[('account',CID,'yes','A')].consecutive_failures==1
            finally:
                drop.set();tick.set();runtime.reconcile(());assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(scenario())


@pytest.mark.parametrize('rest_total',[30,120])
def test_ws_outage_keeps_rest_cancel_and_never_invents_read_failure_limit(tmp_path,rest_total):
    from test_lp_orderbook_ws_487 import ws_live
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_d2_execution_bridge import attach
    async def scenario():
        sockets=[];drop=asyncio.Event()
        async def server(ws):
            sockets.append(ws);await ws.recv()
            if len(sockets)==1:
                await ws.send(json.dumps(wire_book(bids=(('.40','120'),))))
                await drop.wait();await ws.close()
            else:
                await ws.send('PONG');await ws.close()  # healthy proof, no usable book
        async with serve(server,'127.0.0.1',0) as listener:
            clock,boundary,trading,live,store,account=ws_live(tmp_path,
                f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            boundary.book_depth=120
            store.lp_save_observed_pool_roles(account,expected_revision=0,roles={CID:dict(pool='trial',episode='A')})
            producer=PolymarketLPService(store,trading,clock=clock.utc)
            execution=attach(live,producer,tmp_path);scheduler=LPAutoScheduler(execution,clock=clock.utc)
            async def pump(predicate):
                async with asyncio.timeout(4):
                    while not predicate():
                        scheduler.request_check();scheduler.run_due();await asyncio.sleep(.001)
            try:
                await pump(lambda:any(b.baseline_front.value==100 for b in live.state.snapshot().baselines.values())
                    and live.read_runtime.status()['orderbook']['synced']==1)
                assert trading.cancels==[]
                drop.set();await until(lambda:sockets[0].state.name=='CLOSED')
                boundary.holds={('A','book')}
                boundary.book_depth=rest_total;clock.value=5;live.read_runtime.wake()
                key=('account',CID,'yes','A')
                await pump(lambda:live.state.snapshot().yields[key].evaluated_at==clock.utc()
                    and any(e[:3]==('entered','A','book') and e[3]==5 for e in boundary.events))
                frame=live.state.snapshot().observation.source_frame
                old=frame.sources.get(f'market_book:{CID}:yes')
                print('outage held REST vs yield',dict(yield_at=live.state.snapshot().yields[key].evaluated_at,
                    source_at=old.checked_at if old else None,request_entered=True,rest_response_held=True))
                assert not boundary.release.is_set() and trading.cancels==[]
                boundary.release.set()
                def actual_rest():
                    observation=live.state.snapshot().observation
                    source=observation.source_frame.sources.get(f'market_book:{CID}:yes') if observation else None
                    book=source.value.get('book') if source and source.complete and source.value else None
                    return (book is not None and source.checked_at==clock.utc()
                        and book.get('transport') is None
                        and next(r['size'] for r in book['bids'] if r['price']==Decimal('.40'))==rest_total
                        and (rest_total!=30 or bool(trading.cancels)))
                await pump(actual_rest)
                # The scheduler has collected the actual REST book and run protection.
                for number in range(12):
                    if rest_total==120:
                        await until(lambda:(live.read_runtime.status()['orderbook']['next_retry_monotonic'] or 0)>clock())
                        connections=len(sockets)
                        clock.value=live.read_runtime.status()['orderbook']['next_retry_monotonic']
                        await until(lambda:len(sockets)>connections)
                        await until(lambda:not live.read_runtime.status()['orderbook']['connected'])
                    live.read_runtime.wake()
                    live.run_protection_once()
                    failure=live.state.snapshot().read_failures[key]
                    assert failure.consecutive_failures==0
                    assert 'read_failure_limit' not in failure.reason_codes
                if rest_total==30:
                    await until(lambda:bool(trading.cancels))
                    assert trading.cancels==[('A',)]
                    progress=tuple(live.state.snapshot().progress.values())[-1]
                    assert progress.ratio.value==Decimal(1)/3
                else:
                    assert trading.cancels==[]
                    progress=tuple(live.state.snapshot().progress.values())[-1]
                    assert progress.ratio.value==Decimal(5)/6
                assert live.state.snapshot().observation.source_frame.sources[
                    f'market_book:{CID}:yes'].value['book'].get('transport') is None
            finally:
                boundary.release.set();drop.set();assert await asyncio.to_thread(live.close,4)
    asyncio.run(scenario())


@pytest.mark.parametrize('prior_failure',[False,True],ids=['known-ws','real-failure'])
@pytest.mark.parametrize('invalidate',['token','connection'])
def test_ws_source_invalidation_preserves_real_failure_count(prior_failure,invalidate):
    from dataclasses import replace
    from test_lp_async_state_contract import state,financial,row
    async def scenario():
        clock=Clock();boundary=Boundary(clock);owner=state(clock);financial(owner,clock,(row('A'),))
        commands=asyncio.Queue();sockets=[]
        async def server(ws):
            sockets.append(ws);await ws.recv()
            if len(sockets)>1:await ws.wait_closed();return
            await ws.send(json.dumps(wire_book()))
            while True:
                command=await commands.get()
                if command=='close':await ws.close();return
                if command is None:return
                await ws.send(json.dumps(command))
        async with serve(server,'127.0.0.1',0) as listener:
            runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            d=demand('A');runtime.start();runtime.configure_orderbook('use',{'yes':CID},eligible_keys=(d.key,))
            try:
                await until(lambda:runtime.status()['orderbook']['synced']==1)
                runtime.reconcile((d,));await until(lambda:runtime.status()['ready_count']==1)
                original,=runtime.drain_results();assert owner.apply_market_attempt(original)=='applied'
                held=None
                if not prior_failure:
                    runtime.configure_orderbook('use',{'yes':CID},eligible_keys=(d.key,),buy_prices={'yes':(Decimal('.40'),)})
                    await commands.put(wire_book(timestamp=1001,bids=(('.40','99'),)))
                    await until(lambda:runtime.status()['ready_count']==1)
                    held,=runtime.drain_results()  # owner has not accepted this newer version yet
                    assert held.complete and held.sources['book'].value['bids'][0]['size']==99
                if prior_failure:
                    for stamp in range(5,31,5):
                        if stamp==30:boundary.mode[('A','metadata')]='failure'
                        clock.value=stamp;await commands.put(wire_book(timestamp=1000+stamp))
                        await until(lambda:runtime.status()['orderbook']['last_proof_monotonic']==stamp)
                        runtime.wake();await until(lambda:runtime.status()['ready_count']==1)
                        result,=runtime.drain_results();assert owner.apply_market_attempt(result)=='applied'
                key=('account',CID,'yes','A');before=owner.snapshot();count=int(prior_failure)
                assert before.read_failures[key].consecutive_failures==count
                await commands.put('close' if invalidate=='connection' else
                    dict(event_type='tick_size_change',market=CID,asset_id='yes',timestamp='1100'))
                await until(lambda:runtime.orderbook_snapshot('yes') is None)
                notices=runtime.drain_orderbook_results()
                if not prior_failure:assert len(notices)==1
                for notice in notices:
                    for fields in (dict(account='wrong'),dict(process_epoch='wrong'),dict(generation='wrong'),
                        dict(order_id='wrong'),dict(terms_version=99)):
                        assert owner.apply_market_attempt(replace(notice,demand=replace(d,**fields))) in ('stale','invalid_identity')
                    for fields in (dict(condition_id='wrong'),dict(token_id='wrong')):
                        source=notice.sources['book'];version=source.value['book_version']
                        wrong=dict(source.value,**fields,book_version=(version[0]+100,*version[1:]))
                        forged=replace(notice,sources={**notice.sources,'book':replace(source,value=wrong)})
                        assert owner.apply_market_attempt(forged) in ('stale','invalid_identity')
                        assert owner.snapshot()==before
                    assert owner.apply_market_attempt(notice)=='applied'
                after=owner.snapshot()
                assert after.yields[key].yield_pct_per_hour.state=='unknown'
                assert after.read_failures[key].consecutive_failures==count
                assert (after.buy_reserved,after.new_capacity)==(before.buy_reserved,before.new_capacity)
                assert owner.apply_market_attempt(original)=='stale'
                if held is not None:
                    assert owner.apply_market_attempt(held)=='stale'
                boundary.mode.clear();clock.value=35 if prior_failure else 5;runtime.wake()
                await until(lambda:runtime.status()['ready_count']==1)
                rest,=runtime.drain_results();assert rest.complete
                assert rest.sources['book'].value.get('transport') is None
                assert owner.apply_market_attempt(rest)=='applied'
                restored=owner.snapshot()
                assert restored.yields[key].yield_pct_per_hour.state=='known'
                assert restored.read_failures[key].consecutive_failures==0
                for notice in notices:
                    assert owner.apply_market_attempt(replace(notice,attempt=rest.attempt+100))=='stale'
                    assert owner.snapshot()==restored
                # Even a later queued attempt cannot revive a retired WS book version.
                if notices:
                    assert owner.apply_market_attempt(replace(original,attempt=rest.attempt+200))=='stale'
                    assert owner.snapshot()==restored
            finally:
                await commands.put(None);runtime.reconcile(());assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(scenario())


@pytest.mark.parametrize('valid_message',['PONG','book','price-change-after-rest'])
@pytest.mark.parametrize('mode',['refused','no-data','malformed'])
@pytest.mark.parametrize('jitter',[0.0,0.2],ids=['no-jitter','max-jitter'])
def test_reconnect_backoff_bounds_attempts_and_resets_only_after_data(monkeypatch,mode,jitter,valid_message):
    import random
    from websockets.http11 import Response
    from websockets.datastructures import Headers
    monkeypatch.setattr(random,'uniform',lambda low,high:jitter)
    async def scenario():
        clock=Clock();attempts=[];valid=asyncio.Event();finish=asyncio.Event();send_data=asyncio.Event()
        async def request(connection,request):
            attempts.append(clock())
            if mode=='refused' and not valid.is_set():
                return Response(503,'Unavailable',Headers(),b'')
        async def server(ws):
            await ws.recv()
            if valid.is_set():
                if valid_message=='price-change-after-rest':
                    await send_data.wait()
                    await ws.send(json.dumps(change(timestamp=1001,size='30')))
                else:await ws.send('PONG' if valid_message=='PONG' else json.dumps(wire_book()))
                await finish.wait()
            elif mode=='malformed':await ws.send('{broken')
            await ws.close()
        books=OrderBooks(clock=clock,utc_now=clock.utc);books.subscribe({'yes':CID})
        async with serve(server,'127.0.0.1',0,process_request=request) as listener:
            url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}'
            client=OrderBookWebSocket(books,url=url)
            task=asyncio.create_task(client.run())
            try:
                await until(lambda:len(attempts)==1 and not books.status()['connected'])
                # A frozen business clock must forbid an early real connection.
                await asyncio.sleep(.32)
                assert len(attempts)==1,(attempts,books.status())
                waiters=[t for t in asyncio.all_tasks() if not t.done()
                    and getattr(t.get_coro(),'__qualname__','')=='Event.wait']
                assert waiters==[]  # backoff awaits the Event in the owned resident runner
                assert client.managed_tasks()==1
                for streak,base in enumerate((.25,.5,1,2,4,8,16,30,30),1):
                    await until(lambda:books.status().get('failure_streak')==streak)
                    status=books.status();delay=min(30,base*(1+jitter))
                    assert status['next_retry_monotonic']==pytest.approx(clock()+delay)
                    assert isinstance(status['retry_reason'],str) and len(status['retry_reason'])<=64
                    before=len(attempts);clock.value=status['next_retry_monotonic']-.001
                    await asyncio.sleep(.035);assert len(attempts)==before
                    if streak==9:break
                    clock.value=status['next_retry_monotonic'];await until(lambda:len(attempts)==before+1)
                # The returned diagnostics own no mutable runtime state.
                status['failure_streak']=-1;assert books.status()['failure_streak']==9
                valid.set();clock.value=books.status()['next_retry_monotonic']
                await until(lambda:len(attempts)==10)
                if valid_message=='price-change-after-rest':
                    await until(lambda:books.status()['connected'])
                    assert books.seed('yes',wire_book(),books.seed_ticket('yes'))
                    assert books.status()['failure_streak']==9  # REST seed is not received WS data
                    send_data.set()
                    await until(lambda:books.snapshot('yes')['bids'][0][1]==30)
                await until(lambda:books.status()['failure_streak']==0)
                if valid_message=='book':assert books.snapshot('yes') is not None
                finish.set();await until(lambda:books.status()['failure_streak']==1)
                assert books.status()['next_retry_monotonic']==pytest.approx(clock()+.25*(1+jitter))
                # Close during a retry wait must not wait for business time.
                await client.aclose();await asyncio.wait_for(task,1)
                assert client.closed and client.managed_tasks()==0
                assert not [t for t in asyncio.all_tasks() if not t.done()
                    and getattr(t.get_coro(),'__qualname__','')=='Event.wait']
                assert books.status()['next_retry_monotonic'] is None
            finally:
                send_data.set();finish.set();await client.aclose();await asyncio.wait_for(task,3)
        # Runtime mode off also owns and drains a resident backoff task.
        boundary=Boundary(clock)
        runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,
            orderbook_url=url)
        runtime.start();runtime.configure_orderbook('use',{'yes':CID})
        try:
            for streak,delay in enumerate((.25,.5,1,2,4,8,16,30),1):
                await until(lambda:runtime.status()['orderbook'].get('failure_streak')==streak)
                if streak<8:clock.value=runtime.status()['orderbook']['next_retry_monotonic']
            runtime.configure_orderbook('off',{})
            await until(lambda:runtime.status()['orderbook']['owned_tasks']==0)
            # Close itself must also interrupt a new resident 30-second wait.
            runtime.configure_orderbook('use',{'yes':CID})
            for streak in range(1,9):
                await until(lambda:runtime.status()['orderbook']['failure_streak']==streak
                    and runtime.status()['orderbook']['next_retry_monotonic'] is not None)
                if streak<8:clock.value=runtime.status()['orderbook']['next_retry_monotonic']
            assert runtime.status()['orderbook']['next_retry_monotonic']-clock()==30
            assert await asyncio.wait_for(asyncio.to_thread(runtime.close,4),1)
        finally:assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(scenario())
