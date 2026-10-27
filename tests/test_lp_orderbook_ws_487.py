"""#487 approved public WS book/runtime/live contracts; only external boundaries doubled."""
from decimal import Decimal
import pytest
from test_lp_async_source_contract import independent_watchdog, CID
from test_lp_auto_cli import service
from test_lp_async_read_runtime_contract import Clock, Boundary, demand, collect
from open_trader.polymarket_lp_read_runtime import LPReadRuntime
from open_trader.polymarket_lp_market_calculation import CalculationPolicy


def test_off_mode_keeps_rest_and_starts_no_websocket(monkeypatch):
    import websockets.asyncio.client
    connections=[]
    def connect(*args, **kwargs):
        connections.append(args)
        raise AssertionError('off mode opened websocket')
    monkeypatch.setattr(websockets.asyncio.client,'connect',connect)
    assert CalculationPolicy(None).orderbook_mode == 'off'
    clock=Clock(); boundary=Boundary(clock)
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
    runtime.start()
    try:
        runtime.reconcile((demand(sources=('book',)),))
        assert collect(runtime,boundary,bool)[0].complete
        clock.value=4.99; runtime.wake()
        assert runtime.drain_results()==()
        clock.value=5; runtime.wake()
        assert collect(runtime,boundary,bool)[0].complete
        assert [e[3] for e in boundary.events if e[:3]==('entered','B','book')]==[0,5]
        assert not connections
    finally:
        runtime.reconcile(()); assert runtime.close(4)


def wire_book(token='yes', timestamp=1000, bids=(('.40','100'),), asks=(('.60','80'),)):
    return dict(event_type='book',market=CID,asset_id=token,timestamp=str(timestamp),
        bids=[dict(price=p,size=s) for p,s in bids],asks=[dict(price=p,size=s) for p,s in asks])


def books(clock=None):
    from open_trader.polymarket_lp_orderbook import OrderBooks
    clock=clock or Clock()
    value=OrderBooks(clock=clock,utc_now=clock.utc)
    value.subscribe({'yes':CID,'no':CID});value.connected()
    return value


def test_book_snapshot_replaces_all_levels():
    value=books();value.handle(wire_book())
    first=value.snapshot('yes')
    assert first is not None
    assert first['bids']==((Decimal('.40'),Decimal('100')),)
    assert first['asks']==((Decimal('.60'),Decimal('80')),)
    value.handle(wire_book(timestamp=1001,bids=(('.41','70'),),asks=(('.61','60'),)))
    second=value.snapshot('yes')
    assert second['bids']==((Decimal('.41'),Decimal('70')),)
    assert second['asks']==((Decimal('.61'),Decimal('60')),)
    assert first['bids']==((Decimal('.40'),Decimal('100')),)
    with pytest.raises(TypeError): second['bids']=()


def change(price='.40', size='50', side='BUY', token='yes', timestamp=1001, **extra):
    return dict(event_type='price_change',market=CID,timestamp=str(timestamp),
        price_changes=[dict(asset_id=token,price=price,size=size,side=side,**extra)])


def test_price_change_replaces_quantity_and_deletes_zero():
    value=books();value.handle(wire_book())
    for _ in range(2):
        value.handle(change())
        assert value.snapshot('yes')['bids']==((Decimal('.40'),Decimal('50')),)
    value.handle(change(size='0'))
    assert value.snapshot('yes')['bids']==()
    value.handle(change(price='.60',size='12',side='SELL'))
    assert value.snapshot('yes')['asks']==((Decimal('.60'),Decimal('12')),)
    value.handle(change(price='.60',size='0',side='SELL'))
    assert value.snapshot('yes')['asks']==()


@pytest.mark.parametrize('bad',[
    dict(timestamp=999),dict(best_bid='.41'),dict(best_ask='.61'),
    dict(price='.60'),dict(price='-1'),dict(price='1.01'),dict(price='NaN'),
    dict(size='-1'),dict(size='Infinity'),dict(size='NaN'),dict(tick=True),
])
def test_invalid_updates_fall_back_per_token(bad):
    clock=Clock();value=books(clock)
    value.handle(wire_book());value.handle(wire_book(token='no'))
    clock.value=1
    if bad.get('tick'):
        event=dict(event_type='tick_size_change',asset_id='yes',market=CID,timestamp='1001')
    else:event=change(**bad)
    value.handle(event)
    assert value.snapshot('yes') is None
    assert value.snapshot('no')['bids']==((Decimal('.40'),Decimal('100')),)
    value.handle(change(timestamp=1002))
    assert value.snapshot('yes') is None  # delta cannot restore sync


def test_rest_seed_replays_buffer_at_or_after_timestamp():
    value=books()
    for timestamp,size in ((999,'500'),(1000,'50'),(1000,'50'),(1001,'25')):
        value.handle(change(timestamp=timestamp,size=size))
    ticket=value.seed_ticket('yes')
    assert value.seed('yes',wire_book(),ticket)
    assert value.snapshot('yes')['bids']==((Decimal('.40'),Decimal('25')),)
    for patch in (dict(asset_id='wrong'),dict(market='wrong'),dict(timestamp=None)):
        broken=books();raw=wire_book();raw.update(patch)
        assert not broken.seed('yes',raw,broken.seed_ticket('yes'))
        assert broken.snapshot('yes') is None


def test_full_ws_book_supersedes_pending_rest_seed():
    import asyncio
    async def scenario():
        value=books();entered=asyncio.Event();release=asyncio.Event()
        async def rest():
            ticket=value.seed_ticket('yes');entered.set();await release.wait()
            return value.seed('yes',wire_book(),ticket)
        pending=asyncio.create_task(rest());await entered.wait()
        value.handle(wire_book(timestamp=1002,bids=(('.42','7'),)))
        release.set()
        assert not await pending
        assert value.snapshot('yes')['bids']==((Decimal('.42'),Decimal('7')),)
        ticket=value.seed_ticket('yes');value.subscribe({'no':CID});value.subscribe({'yes':CID})
        assert not value.seed('yes',wire_book(),ticket)
        ticket=value.seed_ticket('yes');value.connected()
        assert not value.seed('yes',wire_book(),ticket)
    asyncio.run(scenario())


def test_quiet_book_freshness_follows_connection_liveness():
    clock=Clock();value=books(clock);value.handle(wire_book())
    clock.value=9;value.proof()
    quiet=value.snapshot('yes')
    assert quiet is not None and quiet['received_at']==clock.utc()
    clock.value=19
    assert value.snapshot('yes') is not None
    clock.value=19.001
    assert value.snapshot('yes') is None
    value.status();value.snapshot('yes')
    clock.value=24.001;value.check_liveness()
    assert not value.status()['connected']
    value.proof()
    assert value.snapshot('yes') is None  # old generation cannot revive


def test_subscription_changes_stay_on_one_connection():
    import asyncio,json
    from websockets.asyncio.server import serve
    from open_trader import polymarket_lp_orderbook as api
    async def scenario():
        clock=Clock();events=[];changed=asyncio.Event();sockets=[]
        async def server(ws):
            sockets.append(ws)
            async for raw in ws:
                events.append(raw);changed.set()
                if raw=='PING':await ws.send('PONG')
                else:
                    message=json.loads(raw)
                    for token in message['assets_ids']:await ws.send(json.dumps(wire_book(token=token)))
        async def wait(predicate):
            async with asyncio.timeout(3):
                while not predicate():
                    changed.clear()
                    try:await asyncio.wait_for(changed.wait(),.02)
                    except TimeoutError:pass
        async with serve(server,'127.0.0.1',0) as listener:
            value=api.OrderBooks(clock=clock,utc_now=clock.utc)
            client=api.OrderBookWebSocket(value,url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            value.subscribe({'yes':CID});task=asyncio.create_task(client.run())
            try:
                await wait(lambda:value.snapshot('yes') is not None)
                value.subscribe({'yes':CID,'no':CID})
                await wait(lambda:value.snapshot('no') is not None)
                value.subscribe({'no':CID})
                await wait(lambda:any(r!='PING' and json.loads(r).get('operation')=='unsubscribe' for r in events))
                assert value.snapshot('yes') is None
                clock.value=10;await wait(lambda:'PING' in events)
                await wait(lambda:value.snapshot('no') is not None)
                assert len(sockets)==1
                initial=json.loads(events[0]);assert initial==dict(type='market',assets_ids=['yes'])
                assert any(json.loads(r)==dict(operation='subscribe',assets_ids=['no']) for r in events if r!='PING')
            finally:
                await client.aclose();await asyncio.wait_for(task,3)
            assert sockets[0].state.name=='CLOSED'
            assert client.closed
    asyncio.run(scenario())

    async def delayed_consumer():
        clock=Clock();consumed=asyncio.Event();seen=[]
        async def server(ws):
            await ws.recv();await ws.send(json.dumps([wire_book(),change(size='33')]))
            await ws.wait_closed()
        def update(tokens):
            seen.append(tokens)
            if len(seen)==1:
                clock.value=9  # next queued event was received at zero
                value.subscribe({'no':CID});value.subscribe({'yes':CID})
            if len(seen)==2:consumed.set()
        async with serve(server,'127.0.0.1',0) as listener:
            value=api.OrderBooks(clock=clock,utc_now=clock.utc);value.subscribe({'yes':CID})
            client=api.OrderBookWebSocket(value,url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}',on_update=update)
            task=asyncio.create_task(client.run())
            try:
                await asyncio.wait_for(consumed.wait(),3)
                assert value.status()['last_proof_monotonic']==0
                assert value.seed('yes',wire_book(),value.seed_ticket('yes'))
                assert value.snapshot('yes')['bids'][0][1]==100  # old subscription delta is not replayed
                clock.value=10.001
                assert value.snapshot('yes') is None
            finally:await client.aclose();await task
    asyncio.run(delayed_consumer())


@pytest.mark.parametrize('failure',['disconnect','silence'])
def test_disconnect_and_silence_restore_rest_cadence(failure):
    import asyncio,json
    from websockets.asyncio.server import serve
    async def scenario():
        clock=Clock();boundary=Boundary(clock);drop=asyncio.Event();sockets=[];reconnect=asyncio.Event();handshakes=0
        async def handshake(connection, request):
            nonlocal handshakes
            handshakes+=1
            if handshakes>1:await reconnect.wait()
        async def server(ws):
            sockets.append(ws)
            await ws.recv()
            if len(sockets)==1:
                await ws.send(json.dumps(wire_book()))
                await drop.wait()
                if failure=='disconnect':await ws.close()
            else:await drop.wait()
            await ws.wait_closed()
        async def wait(predicate):
            async with asyncio.timeout(3):
                while not predicate():await asyncio.sleep(.01)
        async with serve(server,'127.0.0.1',0,process_request=handshake) as listener:
            runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            runtime.start();d=demand(sources=('book',))
            try:
                runtime.configure_orderbook('use',{'yes':CID},eligible_keys=(d.key,))
                await wait(lambda:runtime.status()['orderbook']['synced']==1)
                runtime.reconcile((d,));rows=[]
                await wait(lambda:bool(rows.extend(runtime.drain_results()) or rows))
                assert rows[0].complete
                assert not [e for e in boundary.events if e[0]=='entered']
                if failure=='disconnect':drop.set()
                else:clock.value=15;runtime.wake()
                await wait(lambda:runtime.status()['orderbook']['synced']==0)
                clock.value=20;runtime.wake();rows=[]
                await wait(lambda:bool(rows.extend(runtime.drain_results()) or rows))
                assert rows[0].complete
                assert [e[3] for e in boundary.events if e[:3]==('entered','B','book')]==[20]
                clock.value=24.99;runtime.wake();assert runtime.drain_results()==()
                boundary.mode[('B','book')]='cooling'
                clock.value=25;runtime.wake();rows=[]
                await wait(lambda:bool(rows.extend(runtime.drain_results()) or rows))
                assert [e[3] for e in boundary.events if e[:3]==('entered','B','book')]==[20,25]
                assert not rows[0].complete
                for stamp in (30,36.999):
                    clock.value=stamp;runtime.wake();assert runtime.drain_results()==()
                assert [e[3] for e in boundary.events if e[:3]==('entered','B','book')]==[20,25]
                boundary.mode[('B','book')]='success';clock.value=37;runtime.wake();rows=[]
                await wait(lambda:bool(rows.extend(runtime.drain_results()) or rows))
                assert rows[0].complete
                assert [e[3] for e in boundary.events if e[:3]==('entered','B','book')]==[20,25,37]
                reconnect.set();await wait(lambda:len(sockets)>=2)
                assert runtime.status()['orderbook']['synced']==0
                await sockets[-1].send(json.dumps(wire_book(timestamp=int(clock.utc().timestamp()*1000))))
                await wait(lambda:runtime.status()['orderbook']['synced']==1)
                clock.value=42;runtime.wake();rows=[]
                await wait(lambda:bool(rows.extend(runtime.drain_results()) or rows))
                assert rows[0].complete and rows[0].sources['book'].value['transport']=='ws'
                assert [e[3] for e in boundary.events if e[:3]==('entered','B','book')]==[20,25,37]
            finally:
                drop.set();reconnect.set();runtime.reconcile(())
                assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(scenario())


def test_queue_overflow_invalidates_connection_without_dropping_through():
    value=books();ticket=value.seed_ticket('yes')
    for _ in range(257):value.handle(change(timestamp=1001,size='1'))
    assert not value.seed('yes',wire_book(timestamp=1000),ticket)
    assert value.snapshot('yes') is None
    assert not value.status()['connected']
    import asyncio,json
    from websockets.asyncio.server import serve
    from open_trader.polymarket_lp_orderbook import OrderBooks,OrderBookWebSocket
    async def scenario():
        overflow=asyncio.Event();sent=asyncio.Event();connections=[]
        async def server(ws):
            connections.append(ws);await ws.recv()
            if len(connections)==1:
                await ws.send(json.dumps(wire_book()))
                await overflow.wait()
                # One frame makes receiver admission atomic relative to consumer.
                await ws.send(json.dumps([change(size=str(i)) for i in (10,20,30)]));sent.set()
            await ws.wait_closed()
        async def wait(predicate):
            async with asyncio.timeout(3):
                while not predicate():await asyncio.sleep(.01)
        async with serve(server,'127.0.0.1',0) as listener:
            value=OrderBooks();value.subscribe({'yes':CID})
            client=OrderBookWebSocket(value,url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}',queue_limit=2)
            task=asyncio.create_task(client.run())
            try:
                await wait(lambda:value.snapshot('yes') is not None)
                overflow.set();await sent.wait()
                await wait(lambda:value.status()['queue_overflows']==1)
                assert value.snapshot('yes') is None
                await wait(lambda:len(connections)==2)
                assert value.snapshot('yes') is None
            finally:await client.aclose();await task
    asyncio.run(scenario())


def test_reconciliation_compares_full_book_and_skips_older_rest():
    value=books();value.handle(wire_book(timestamp=1002,bids=(('.40','100'),('.39','5'))))
    assert not value.reconcile('yes',wire_book(timestamp=1001))
    assert value.snapshot('yes')['bids'][1]==(Decimal('.39'),Decimal('5'))
    assert value.status()['reconciliation_mismatches']==0
    raw=wire_book(timestamp=1002,bids=(('.40','100'),('.39','7')))
    assert value.reconcile('yes',raw)
    assert value.snapshot('yes')['bids'][1]==(Decimal('.39'),Decimal('7'))
    assert value.status()['reconciliation_mismatches']==1
    assert value.status()['last_reconciliation']['reason']=='full_book_mismatch'
    value.handle(change(timestamp=1003,size='50'))
    assert value.snapshot('yes')['bids']==((Decimal('.40'),Decimal('50')),(Decimal('.39'),Decimal('7')))


@pytest.mark.parametrize('audit_advances',[False,True])
def test_synced_use_mode_only_fetches_rest_book_each_sixty_seconds(audit_advances):
    import asyncio,json
    from websockets.asyncio.server import serve
    async def scenario():
        clock=Clock();boundary=Boundary(clock);ready=asyncio.Event();allow_book=asyncio.Event();sockets=[]
        async def server(ws):
            sockets.append(ws)
            await ws.recv();ready.set();await allow_book.wait()
            await ws.send(json.dumps(wire_book(timestamp=int(clock.utc().timestamp()*1000))))
            async for raw in ws:
                if raw=='PING':await ws.send('PONG')
        async def wait(predicate):
            async with asyncio.timeout(3):
                while not predicate():await asyncio.sleep(.01)
        async with serve(server,'127.0.0.1',0) as listener:
            runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            runtime.start();ds=(demand('A'),demand('B'))
            runtime.configure_orderbook('use',{'yes':CID},eligible_keys=tuple(d.key for d in ds))
            runtime.reconcile(ds)
            try:
                await ready.wait();initial=[]
                await wait(lambda:len(initial.extend(runtime.drain_results()) or initial)==2)
                assert all(r.complete for r in initial)
                initial_gets=sum(e[0]=='entered' and e[2]=='book' for e in boundary.events)
                assert initial_gets>=1
                allow_book.set();await wait(lambda:runtime.status()['orderbook']['synced']==1)
                for stamp in range(5,60,5):
                    clock.value=stamp;runtime.wake();rows=[]
                    if stamp%10==0:
                        await wait(lambda:runtime.status()['orderbook']['last_proof_monotonic']==stamp)
                    await wait(lambda:len(rows.extend(runtime.drain_results()) or rows)==2)
                    assert all(r.complete for r in rows)
                clock.value=59;runtime.wake()
                assert sum(e[0]=='entered' and e[2]=='book' for e in boundary.events)==initial_gets
                if audit_advances:boundary.holds={('A','book'),('B','book')}
                clock.value=60;runtime.wake();rows=[]
                if audit_advances:
                    await wait(lambda:any(e[0]=='entered' and e[2]=='book' and e[3]==60 for e in boundary.events))
                    await sockets[0].send(json.dumps(change(size='77',timestamp=int(clock.utc().timestamp()*1000)+1)))
                    await wait(lambda:runtime.orderbook_snapshot('yes')['bids'][0]['size']==77)
                    boundary.release.set()
                await wait(lambda:len(rows.extend(runtime.drain_results()) or rows)==2)
                assert sum(e[0]=='entered' and e[2]=='book' for e in boundary.events)==initial_gets+1
                if audit_advances:assert all(r.sources['book'].value['bids'][0]['size']==77 for r in rows)
                for source in ('metadata','native_rewards','sponsored_rewards'):
                    assert sorted({e[3] for e in boundary.events if e[0]=='entered' and e[2]==source})==[0,30,60]
            finally:
                allow_book.set();runtime.reconcile(());assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(scenario())


def ws_live(tmp_path, url=None, mode='use'):
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from test_lp_async_live_contract import Trading,wire_row
    import hashlib
    clock=Clock();boundary=Boundary(clock);boundary.orders=[wire_row('A')]
    trading=Trading();store=PredictionArbitrageStore(tmp_path)
    account=hashlib.sha256(b'account').hexdigest()
    store.lp_save_current_settings(account,expected_config_version=0,changes=dict(api_retry_interval_seconds=60))
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,orderbook_url=url)
    live=LPD2LiveExecution(store,trading,account_id='account',venue='polymarket',process_epoch='epoch',
        policy=CalculationPolicy(Decimal(100),allocation_mode='observed_pools',orderbook_mode=mode),
        read_runtime=runtime,clock=clock,utc_now=clock.utc)
    live.initialize(ValidatedStartAdmission('epoch','offline-permit'))
    return clock,boundary,trading,live,store,account


def test_pool_membership_limits_subscriptions_and_use(tmp_path,monkeypatch):
    import asyncio,json
    from websockets.asyncio.server import serve
    from test_lp_async_live_contract import wire_row
    async def scenario():
        subscribed=set();messages=[]
        async def server(ws):
            try:
                async for raw in ws:
                    if raw=='PING':await ws.send('PONG');continue
                    row=json.loads(raw);messages.append(row)
                    if row.get('operation')=='unsubscribe':subscribed.difference_update(row['assets_ids'])
                    else:subscribed.update(row['assets_ids'])
            finally:subscribed.clear()  # physical close also removes subscriptions
        async with serve(server,'127.0.0.1',0) as listener:
            url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}'
            clock,boundary,trading,live,store,account=ws_live(tmp_path,url)
            other='0x'+'2'*64;stale='0x'+'3'*64
            boundary.orders += [dict(wire_row('B'),market=other,asset_id='b'),
                dict(wire_row('manual'),market=stale,asset_id='manual'),
                dict(wire_row('sell',side='SELL'),market='0x'+'4'*64,asset_id='sell'),
                dict(wire_row('foreign'),market='0x'+'6'*64,asset_id='foreign',maker_address='other-account')]
            store.lp_save_observed_pool_roles(account,expected_revision=0,roles={
                CID:dict(pool='trial',episode='A'),other:dict(pool='income',episode='B'),
                stale:dict(pool='trial',episode='old-manual'),
                '0x'+'5'*64:dict(pool='trial',episode='candidate-only')})
            try:
                async with asyncio.timeout(4):
                    while subscribed!={'yes','b'}:
                        live.run_once();await asyncio.sleep(.01)
                assert live.read_runtime.status()['orderbook']['subscribed']==2
                assert any(e[:3]==('entered','manual','book') for e in boundary.events)
                saved=store.lp_load_observed_pool_roles(account)
                store.lp_save_observed_pool_roles(account,expected_revision=saved['revision'],roles={})
                async with asyncio.timeout(4):
                    while subscribed:
                        live.run_once();await asyncio.sleep(.01)
                assert live.read_runtime.status()['orderbook']['subscribed']==0
                assert trading.cancels==[]
            finally:assert await asyncio.to_thread(live.close,4)
    asyncio.run(scenario())

    async def detached_pricing():
        async def server(ws):
            await ws.recv();await ws.send(json.dumps(wire_book(bids=(('.45','100'),))))
            await ws.wait_closed()
        async with serve(server,'127.0.0.1',0) as listener:
            clock=Clock();boundary=Boundary(clock)
            runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            ds=(demand('incumbent',sources=('book',)),demand('new-entry',kind='income',sources=('book',)))
            runtime.start();runtime.configure_orderbook('use',{'yes':CID},eligible_keys=tuple(d.key for d in ds))
            try:
                async with asyncio.timeout(3):
                    while runtime.status()['orderbook']['synced']!=1:await asyncio.sleep(.01)
                runtime.reconcile(ds);rows=[]
                async with asyncio.timeout(3):
                    while len(rows)<2:rows.extend(runtime.drain_results());await asyncio.sleep(.01)
                by_id={r.demand.order_id:r for r in rows}
                assert by_id['incumbent'].sources['book'].value['bids'][0]['price']==Decimal('.45')
                assert by_id['new-entry'].sources['book'].value['bids'][0]['price']==Decimal('.40')
                assert any(e[:3]==('entered','new-entry','book') for e in boundary.events)
            finally:assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(detached_pricing())

    async def pricing_scenario():
        from tests.test_lp_observed_pools import AsyncPoolRig
        import websockets.asyncio.client
        original_connect=websockets.asyncio.client.connect
        rig=None
        async def server(ws):
            async for raw in ws:
                if raw=='PING':await ws.send('PONG')
                else:
                    packet=wire_book(token='tA',bids=(('.45','10000'),))
                    packet['market']=rig.cid('A')
                    await ws.send(json.dumps(packet))
        async with serve(server,'127.0.0.1',0) as listener:
            local_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}'
            with monkeypatch.context() as patch:
                patch.setattr(websockets.asyncio.client,'connect',lambda url,**kw:original_connect(local_url,**kw))
                rig=AsyncPoolRig(tmp_path/'pricing',patch)
                try:
                    rig.market('A');rig.add_order('A','1')
                    roles=rig.store.lp_load_observed_pool_roles(rig.account)
                    rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=roles['revision'],
                        roles={rig.cid('A'):dict(pool='trial',episode='old-A')})
                    rig.tick(5)  # next original account read can observe the newly added order
                    rig.live.configure(dict(orderbook_mode='use'))
                    async with asyncio.timeout(4):
                        while rig.live.read_runtime.orderbook_snapshot('tA') is None:
                            rig.live.run_once();await asyncio.sleep(.01)
                    assert rig.live.read_runtime.orderbook_snapshot('tA')['bids'][0]['price']==Decimal('.45')
                    rig.release('old-A')  # exchange departure; same token remains watched during fresh preview
                    reads=[];actual=rig.public.get_order_book
                    def rest_book(*,token_id):
                        result=actual(token_id=token_id);reads.append((token_id,result.bids[0].price));return result
                    rig.public.get_order_book=rest_book
                    preview=rig.live.manual_preview(rig.producer,dict(condition_id=rig.cid('A'),
                        token_id='tA',market_id='mA',outcome='YES'),candidate=True)
                    assert preview['state']=='previewed',preview
                    assert preview['request']['price']==Decimal('.40')
                    assert ('tA',Decimal('.40')) in reads
                    assert rig.sdk.posts==[]
                finally:assert await asyncio.to_thread(rig.close) is None
    asyncio.run(pricing_scenario())


def test_buy_depth_wakes_once_and_delivers_latest_before_protection(tmp_path):
    import asyncio,json,threading
    from websockets.asyncio.server import serve
    async def scenario():
        clock=Clock();boundary=Boundary(clock);messages=asyncio.Queue();wake=threading.Event();wakes=[]
        async def server(ws):
            await ws.recv()
            await ws.send(json.dumps(wire_book(bids=(('.45','10'),('.42','20'),('.40','100'),('.39','40')))))
            while True:
                row=await messages.get()
                if row is None:return
                await ws.send(json.dumps(row))
        async def wait(predicate):
            async with asyncio.timeout(3):
                while not predicate():await asyncio.sleep(.01)
        async with serve(server,'127.0.0.1',0) as listener:
            runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            runtime.start(lambda:(wakes.append(clock()),wake.set()));d=demand(sources=('book',))
            runtime.configure_orderbook('use',{'yes':CID},eligible_keys=(d.key,),buy_prices={'yes':(Decimal('.40'),)})
            try:
                await wait(lambda:runtime.status()['orderbook']['synced']==1)
                runtime.reconcile((d,));initial=[]
                await wait(lambda:bool(initial.extend(runtime.drain_results()) or initial))
                runtime.complete_orderbook_check();wake.clear();wakes.clear()
                starting_wakes=runtime.status()['orderbook']['wake_requests']
                for row in (change(price='.39',size='41'),change(price='.60',size='81',side='SELL')):
                    await messages.put(row)
                await wait(lambda:runtime.orderbook_snapshot('yes')['asks'][0]['size']==81)
                assert runtime.drain_results()==()
                assert runtime.status()['orderbook']['wake_requests']==starting_wakes
                await messages.put([change(price=p,size=s) for p,s in (('.45','11'),('.42','21'),('.40','30'))])
                await wait(lambda:runtime.status()['ready_count']==1)
                before=runtime.status()['orderbook']['wake_requests'];rows=runtime.drain_results()
                assert before==starting_wakes+1
                latest=rows[0].sources['book'].value
                assert {r['price']:r['size'] for r in latest['bids']}[Decimal('.40')]==30
                assert runtime.status()['orderbook']['pending_checks']==1
                await messages.put(change(price='.40',size='31',timestamp=1002))
                await wait(lambda:runtime.orderbook_snapshot('yes')['bids'][2]['size']==31)
                assert runtime.status()['orderbook']['wake_requests']==before
                runtime.complete_orderbook_check()
                await messages.put(change(price='.40',size='32',timestamp=1003))
                await wait(lambda:runtime.status()['ready_count']==1 and runtime.orderbook_snapshot('yes')['bids'][2]['size']==32)
                assert runtime.drain_results()[0].sources['book'].value['bids'][2]['size']==32
            finally:
                await messages.put(None);runtime.reconcile(());assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(scenario())

    async def live_scenario():
        from open_trader.polymarket_lp import PolymarketLPService
        from open_trader.polymarket_lp_scheduler import LPAutoScheduler
        from tests.test_lp_d2_execution_bridge import attach
        outbound=asyncio.Queue()
        async def server(ws):
            await ws.recv();await ws.send(json.dumps(wire_book(bids=(('.40','120'),))))
            while True:
                row=await outbound.get()
                if row is None:return
                await ws.send(json.dumps(row))
        async with serve(server,'127.0.0.1',0) as listener:
            clock,boundary,trading,live,store,account=ws_live(tmp_path,
                f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            boundary.book_depth=120
            store.lp_save_observed_pool_roles(account,expected_revision=0,roles={CID:dict(pool='trial',episode='A')})
            producer=PolymarketLPService(store,trading,clock=clock.utc)
            execution=attach(live,producer,tmp_path)
            scheduler=LPAutoScheduler(execution,clock=clock.utc)
            try:
                async with asyncio.timeout(4):
                    while not (live.read_runtime.status()['orderbook']['synced']==1
                            and any(b.baseline_front.value==100 for b in live.state.snapshot().baselines.values())):
                        scheduler.request_check();scheduler.run_due();await asyncio.sleep(.01)
                assert trading.cancels==[]
                await outbound.put([change(price=p,size=q) for p,q in (('.45','10'),('.42','20'),('.40','30'))])
                async with asyncio.timeout(3):
                    while live.read_runtime.status()['ready_count']!=1:await asyncio.sleep(.01)
                assert scheduler.run_due()
                async with asyncio.timeout(3):
                    while not trading.cancels:await asyncio.sleep(.01)
                assert trading.cancels==[('A',)]
                progress=tuple(live.state.snapshot().progress.values())[-1]
                assert progress.ratio.value==Decimal(1)/3
                assert live.read_runtime.status()['orderbook']['pending_checks']==0
            finally:
                await outbound.put(None);assert await asyncio.to_thread(live.close,4)
    asyncio.run(live_scenario())


@pytest.mark.parametrize('bad_source',['metadata','native_rewards','sponsored_rewards','timeout'])
def test_ws_market_results_keep_identity_watermark_and_unknown_guards(bad_source):
    import asyncio,json
    from dataclasses import replace
    from websockets.asyncio.server import serve
    from test_lp_async_state_contract import state,financial,row
    async def scenario():
        clock=Clock();owner=state(clock);financial(owner,clock,(row('A'),))
        boundary=Boundary(clock);outbound=asyncio.Queue()
        async def server(ws):
            await ws.recv();await ws.send(json.dumps(wire_book()))
            while True:
                event=await outbound.get()
                if event is None:return
                await ws.send(json.dumps(event))
        async def wait(predicate):
            async with asyncio.timeout(3):
                while not predicate():await asyncio.sleep(.01)
        async with serve(server,'127.0.0.1',0) as listener:
            runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            runtime.start();d=demand('A')
            runtime.configure_orderbook('use',{'yes':CID},eligible_keys=(d.key,))
            try:
                await wait(lambda:runtime.status()['orderbook']['synced']==1)
                runtime.reconcile((d,));rows=[]
                await wait(lambda:bool(rows.extend(runtime.drain_results()) or rows))
                result=rows[0];assert result.complete
                assert owner.apply_market_attempt(result)=='applied'
                current=owner.snapshot()
                assert current.yields[('account',CID,'yes','A')].yield_pct_per_hour.state=='known'
                for fields in (dict(account='other'),dict(process_epoch='old'),dict(generation='old'),
                    dict(order_id='other'),dict(terms_version=999)):
                    assert owner.apply_market_attempt(replace(result,demand=replace(d,**fields))) in ('invalid_identity','stale')
                    assert owner.snapshot()==current
                assert owner.apply_market_attempt(replace(result,attempt=result.attempt-1))=='stale'
                financial(owner,clock,(row('A'),),complete=False)
                assert owner.apply_market_attempt(replace(result,attempt=result.attempt+100))=='applied'
                assert owner.snapshot().new_capacity.state=='unknown'
                await outbound.put(dict(event_type='tick_size_change',asset_id='yes',market=CID,timestamp='1001'))
                await wait(lambda:runtime.orderbook_snapshot('yes') is None)
                invalid_now=runtime.drain_orderbook_results()
                assert len(invalid_now)==1 and not invalid_now[0].complete
                owner.apply_market_attempt(replace(invalid_now[0],attempt=result.attempt+101))
                assert owner.snapshot().yields[('account',CID,'yes','A')].yield_pct_per_hour.state=='unknown'
                runtime.complete_orderbook_check()
                clock.value=5;runtime.wake()
                await wait(lambda:runtime.status()['ready_count']==1)
                await outbound.put(dict(event_type='tick_size_change',asset_id='yes',market=CID,timestamp='1001'))
                await wait(lambda:runtime.orderbook_snapshot('yes') is None)
                invalid,=runtime.drain_results()
                assert not invalid.complete
                owner.apply_market_attempt(replace(invalid,attempt=result.attempt+102))
                assert owner.snapshot().yields[('account',CID,'yes','A')].yield_pct_per_hour.state=='unknown'
                clock.value=10.001
                assert owner.snapshot().new_capacity.state=='unknown'
            finally:
                await outbound.put(None);runtime.reconcile(());assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(scenario())

    async def failed_refresh():
        clock=Clock();owner=state(clock);financial(owner,clock,(row('A'),))
        boundary=Boundary(clock);messages=asyncio.Queue()
        async def server(ws):
            await ws.recv();await ws.send(json.dumps(wire_book()))
            while True:
                message=await messages.get()
                if message is None:return
                await ws.send(json.dumps(message))
        async def wait(predicate):
            async with asyncio.timeout(3):
                while not predicate():await asyncio.sleep(.01)
        async with serve(server,'127.0.0.1',0) as listener:
            runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc,
                orderbook_url=f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
            d=demand('A');runtime.start()
            runtime.configure_orderbook('use',{'yes':CID},eligible_keys=(d.key,),buy_prices={'yes':(Decimal('.40'),)})
            try:
                await wait(lambda:runtime.status()['orderbook']['synced']==1)
                runtime.reconcile((d,))
                for stamp in range(0,31,5):
                    if stamp==30:
                        if bad_source=='timeout':boundary.holds={('A','metadata')}
                        else:boundary.mode[('A',bad_source)]='failure'
                    clock.value=stamp
                    await messages.put(wire_book(timestamp=1000+stamp))
                    await wait(lambda:runtime.status()['orderbook']['last_proof_monotonic']==stamp)
                    runtime.wake();results=[]
                    if stamp==30 and bad_source=='timeout':
                        await wait(lambda:('entered','A','metadata',30) in boundary.events)
                        clock.value=35;runtime.wake()
                    await wait(lambda:bool(results.extend(runtime.drain_results()) or results))
                    current=results[0]
                    assert current.complete==(stamp!=30)
                    assert owner.apply_market_attempt(current)=='applied'
                    runtime.complete_orderbook_check()
                key=('account',CID,'yes','A')
                assert owner.snapshot().read_failures[key].consecutive_failures==1
                assert owner.snapshot().yields[key].yield_pct_per_hour.state=='unknown'
                await messages.put(change(size='30',timestamp=1100))
                await wait(lambda:runtime.orderbook_snapshot('yes')['bids'][0]['size']==30)
                only_book=(*runtime.drain_results(),*runtime.drain_orderbook_results())
                assert not any(r.complete for r in only_book)
                for result in only_book:owner.apply_market_attempt(result)
                assert owner.snapshot().read_failures[key].consecutive_failures==1
                assert owner.snapshot().yields[key].yield_pct_per_hour.state=='unknown'
                boundary.holds.clear();boundary.mode.clear();boundary.release.set()
                clock.value=35;runtime.wake();healthy=[]
                await wait(lambda:any(r.complete for r in (healthy.extend(runtime.drain_results()) or healthy)))
                latest=next(r for r in healthy if r.complete)
                assert owner.apply_market_attempt(latest)=='applied'
                assert owner.snapshot().read_failures[key].consecutive_failures==0
                assert latest.sources['metadata' if bad_source=='timeout' else bad_source].checked_at==clock.utc()
            finally:
                await messages.put(None);boundary.release.set();runtime.reconcile(())
                assert await asyncio.to_thread(runtime.close,4)
    asyncio.run(failed_refresh())


@pytest.mark.parametrize('sink',['normal','raises','blocked'])
@pytest.mark.parametrize('ws_total',['30','120',None])
def test_shadow_rest_cancel_implies_ws_cancel_without_sending_shadow(tmp_path,ws_total,sink):
    import asyncio,json,logging,threading
    from websockets.asyncio.server import serve
    async def scenario():
        outbound=asyncio.Queue()
        async def server(ws):
            await ws.recv();await ws.send(json.dumps(wire_book(bids=(('.40','120'),))))
            while True:
                event=await outbound.get()
                if event is None:return
                await ws.send(json.dumps(event))
        async with serve(server,'127.0.0.1',0) as listener:
            clock,boundary,trading,live,store,account=ws_live(tmp_path,
                f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}',mode='shadow')
            boundary.book_depth=120
            store.lp_save_observed_pool_roles(account,expected_revision=0,roles={CID:dict(pool='trial',episode='A')})
            async def pump(predicate):
                async with asyncio.timeout(4):
                    while not predicate():
                        live.run_once();await asyncio.sleep(.01)
            try:
                await pump(lambda:('account',CID,'yes','A') in live.state.snapshot().yields
                    and live.state.snapshot().yields[('account',CID,'yes','A')].yield_pct_per_hour.state=='known'
                    and live.read_runtime.status()['orderbook']['synced']==1)
                live.run_protection_once()
                baseline,=live.state.snapshot().baselines.values()
                assert baseline.baseline_front.value==100
                assert trading.cancels==[]
                if ws_total is None:await outbound.put(dict(event_type='tick_size_change',market=CID,asset_id='yes',timestamp='1001'))
                else:await outbound.put(change(size=ws_total,timestamp=1001))
                async with asyncio.timeout(3):
                    while (live.read_runtime.orderbook_snapshot('yes') is None) != (ws_total is None):await asyncio.sleep(.01)
                if ws_total is not None:
                    async with asyncio.timeout(3):
                        while live.read_runtime.orderbook_snapshot('yes')['bids'][0]['size']!=Decimal(ws_total):await asyncio.sleep(.01)
                boundary.book_depth=30;clock.value=5;live.read_runtime.wake()
                await pump(lambda:live.state.snapshot().yields[('account',CID,'yes','A')].evaluated_at==clock.utc())
                if ws_total is None:
                    await outbound.put(dict(event_type='tick_size_change',market=CID,asset_id='yes',timestamp='1002'))
                    async with asyncio.timeout(3):
                        while live.read_runtime.orderbook_snapshot('yes') is not None:await asyncio.sleep(.01)
                failures=live.state.snapshot().failure_states
                entered=threading.Event();release=threading.Event()
                class Handler(logging.Handler):
                    def emit(self,record):
                        if not record.msg.startswith('prediction_lp_orderbook_shadow'):return
                        entered.set()
                        if sink=='raises':raise RuntimeError('broken external log sink')
                        if sink=='blocked':assert release.wait(4)
                logger=logging.getLogger('open_trader.polymarket_lp_read_runtime')
                old_level=logger.level;handler=Handler();logger.addHandler(handler);logger.setLevel(logging.INFO)
                try:
                    handles=await asyncio.wait_for(asyncio.to_thread(live.run_protection_once),1)
                    assert await asyncio.to_thread(entered.wait,1)
                    assert await asyncio.to_thread(trading.cancelled.wait,1)
                finally:
                    release.set();logger.removeHandler(handler);logger.setLevel(old_level)
                for handle in handles:
                    await asyncio.to_thread(handle.result,4);await asyncio.to_thread(handle._thread.join,4)
                assert trading.cancels==[('A',)]  # REST always owns the one real cancel
                progress=tuple(live.state.snapshot().progress.values())[-1]
                assert progress.ratio.value==Decimal(1)/3
                shadow=live.read_runtime.status()['orderbook']['shadow']
                last=shadow['last']
                assert last['rest_can_cancel'] is True
                if ws_total=='30':assert last['comparable'] and last['ws_can_cancel'] and last['consistent']
                elif ws_total=='120':assert last['comparable'] and not last['ws_can_cancel'] and not last['consistent']
                else:assert not last['comparable'] and not last['consistent']
                # Comparison itself has no second native sender or failure-state mutation.
                assert len(trading.cancels)==1
                assert all(v.data_failures==0 for v in live.state.snapshot().failure_states.values())
            finally:
                await outbound.put(None);assert await asyncio.to_thread(live.close,4)
    asyncio.run(scenario())


def test_shadow_records_two_hour_coverage_and_bounded_diagnostics():
    from open_trader import polymarket_lp_orderbook as api
    clock=Clock();summary=api.ShadowDiagnostics(clock=clock)
    assert summary.snapshot()['assessment']=='UNKNOWN'
    rest=dict(state='triggered',reason_codes=['credential-secret-account-order'])
    ws=dict(state='triggered')
    for stamp in range(0,7201,10):
        clock.value=stamp;summary.record(rest,ws)
        summary.update_latency(.002);summary.decision_latency(.003)
    report=summary.snapshot()
    assert report['coverage_seconds']==7200
    assert report['assessment']=='PASS'
    assert report['rest_can_cancel_implies_ws_can_cancel'] is True
    assert report['automatic_enable'] is False
    assert report['arrival_to_update']['retained']<=256
    assert report['arrival_to_update']['p95_seconds']==.002
    assert report['wake_to_decision']['p95_seconds']==.003
    clock.value=7210;summary.record(rest,dict(state='monitoring'))
    clock.value=7220;summary.record(rest,dict(state='unknown',reason_codes=['raw-exception-secret']))
    after=summary.snapshot()
    assert report['mismatches']==0 and after['mismatches']==1
    assert after['not_comparable']==1 and after['assessment']=='FAIL'
    assert after['reasons']==dict(consistent=721,decision_mismatch=1,ws_unknown=1)
    import json
    assert 'secret' not in json.dumps(after)
    assert len(json.dumps(after))<4096
    after['reasons'].clear()
    assert summary.snapshot()['reasons']['consistent']==721

    value=books(clock);value.handle(wire_book());value.handle(wire_book(token='no'))
    clock.value=7221;value.handle(change(timestamp=999))
    diagnostic=value.status()['tokens'];by_token={r['token_id']:r for r in diagnostic['items']}
    assert by_token['yes']['route']=='rest'
    assert by_token['yes']['fallback_reason']=='timestamp_regression'
    assert by_token['yes']['fallback_at']==clock.utc().isoformat()
    assert by_token['no']['route']=='ws' and by_token['no']['fallback_reason'] is None
    assert all(r['reconciliation_mismatches']==0 for r in by_token.values())
    clock.value=7222;value.disconnected('disconnect')
    latest=value.status()['tokens']
    assert all(r['route']=='rest' and r['fallback_reason']=='disconnect' for r in latest['items'])
    assert by_token['no']['route']=='ws'  # old snapshot detached
    latest['items'][0]['fallback_reason']='mutated'
    assert value.status()['tokens']['items'][0]['fallback_reason']=='disconnect'
    value.subscribe({str(i):CID for i in range(64)})
    bounded=value.status()['tokens']
    assert bounded['total']==64 and len(bounded['items'])==16 and bounded['omitted']==48
    detached=books(clock);detached.handle(wire_book())
    detached.reconcile('yes',wire_book(timestamp=1001,bids=(('.40','99'),)))
    old=detached.status();old['last_reconciliation']['reason']='mutated'
    assert detached.status()['last_reconciliation']['reason']=='full_book_mismatch'


def test_mode_switch_is_live_validated_and_does_not_change_trading_settings(tmp_path,service,capsys):
    import asyncio,json
    from websockets.asyncio.server import serve
    async def scenario():
        sockets=[]
        async def server(ws):
            sockets.append(ws)
            async for raw in ws:
                if raw=='PING':await ws.send('PONG')
                else:await ws.send(json.dumps(wire_book(bids=(('.40','130'),))))
        async with serve(server,'127.0.0.1',0) as listener:
            clock,boundary,trading,live,store,account=ws_live(tmp_path,
                f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}',mode='off')
            settings=store.lp_load_current_settings(account)
            assert settings.get('orderbook_mode','off')=='off'
            store.lp_save_current_settings(account,expected_config_version=settings['config_version'],
                changes=dict(allocation_mode='observed_pools',budget_usd='100'))
            live.load_settings()
            store.lp_save_observed_pool_roles(account,expected_revision=0,roles={CID:dict(pool='trial',episode='A')})
            async def pump(predicate):
                async with asyncio.timeout(4):
                    while not predicate():live.run_once();await asyncio.sleep(.01)
            async def mode(value):
                live.configure(dict(orderbook_mode=value))
                await pump(lambda:live.settings_status()['effective'].orderbook_mode==value
                    and not live.settings_status()['pending'])
            try:
                live.run_once();assert sockets==[]
                before=live.settings_status()['effective'];identity=live.state.snapshot().current
                await mode('shadow')
                await pump(lambda:live.read_runtime.status()['orderbook']['synced']==1)
                assert live.read_runtime.orderbook_snapshot('yes')['bids'][0]['size']==130
                await mode('use')
                assert len(sockets)==1
                assert live.read_runtime.orderbook_snapshot('yes')['bids'][0]['size']==130
                with pytest.raises(ValueError,match='orderbook_mode_invalid'):
                    live.configure(dict(orderbook_mode='automatic'))
                assert live.settings_status()['effective'].orderbook_mode=='use'
                await mode('off')
                await pump(lambda:sockets[0].state.name=='CLOSED')
                clock.value=5;live.read_runtime.wake()
                await pump(lambda:any(e[:3]==('entered','A','book') and e[3]==5 for e in boundary.events))
                after=live.settings_status()['effective']
                assert (after.budget,after.target_count,after.trading_config_version)==(before.budget,before.target_count,before.trading_config_version)
                assert live.state.snapshot().current.cycle_generation==identity.cycle_generation
                assert live.state.snapshot().current.effective_running==identity.effective_running
                assert store.lp_load_current_control(account)['desired_running'] is False
                assert trading.cancels==[]
            finally:assert await asyncio.to_thread(live.close,4)
    asyncio.run(scenario())

    from copy import deepcopy
    from test_lp_auto_cli import invoke
    service.state.update(settings_pending=False,settings_native_inflight=False,settings_error=None,
        round_interval_seconds=30,api_retry_interval_seconds=60,order_check_interval_seconds=10)
    pending=deepcopy(service.state);pending.update(orderbook_mode='shadow',config_version=8,settings_pending=True)
    service.reply=pending
    assert invoke(service,'config','--orderbook-mode','shadow','--json')==0
    assert __import__('json').loads(capsys.readouterr().out)['result']=='CONFIGURED'
    post=[r for r in service.requests if r[0]=='POST'][-1]
    assert post[2]==dict(orderbook_mode='shadow',expected_config_version=7)
    assert service.state['desired_running'] is False
    service.reply=None
    for mode in ('use','off'):
        assert invoke(service,'config','--orderbook-mode',mode,'--json')==0
        assert __import__('json').loads(capsys.readouterr().out)['result']=='CONFIGURED'


def test_shutdown_retains_owner_until_ws_and_rest_cleanup_finish(monkeypatch):
    import asyncio,json,threading
    from open_trader.polymarket_lp_orderbook import OrderBooks
    import websockets.asyncio.client
    entered=threading.Event();release_ws=threading.Event();closed=threading.Event()
    class Socket:
        def __init__(self):self.first=True
        async def __aenter__(self):return self
        async def __aexit__(self,*args):await self.close()
        async def send(self,message):pass
        def __aiter__(self):return self
        async def __anext__(self):
            if self.first:self.first=False;return json.dumps(wire_book())
            await asyncio.Future()
        async def close(self):
            entered.set()
            while not release_ws.is_set():await asyncio.sleep(.01)
            closed.set()
    monkeypatch.setattr(websockets.asyncio.client,'connect',lambda *a,**k:Socket())
    clock=Clock();boundary=Boundary(clock);boundary.holds={('B','book')};boundary.mode[('B','book')]='swallow'
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc)
    runtime.start();d=demand(sources=('book',))
    try:
        runtime.reconcile((d,))
        boundary.wait(lambda:('entered','B','book',0) in boundary.events)
        runtime.configure_orderbook('shadow',{'yes':CID},eligible_keys=(d.key,))
        boundary.wait(lambda:runtime.status()['orderbook']['synced']==1)
        assert not runtime.close(.02)
        assert entered.wait(3) and not closed.is_set()
        assert runtime.status()['owned_buy_scopes']==1
        release_ws.set();assert closed.wait(3)
        assert not runtime.close(.02)  # real HTTP still owns its response
        boundary.release.set();assert runtime.close(4)
        assert not [t for t in threading.enumerate() if t.name in ('lp-market-io','lp-account-exact-io','lp-read-deadlines')]
        value=OrderBooks(clock=clock,utc_now=clock.utc);value.connected()
        for i in range(300):
            token=str(i);value.subscribe({token:CID});value.handle(wire_book(token=token))
            assert value.status()['retained_tokens']<=1
        value.subscribe({});assert value.status()['retained_tokens']==0
    finally:
        release_ws.set();boundary.release.set();runtime.close(4)
