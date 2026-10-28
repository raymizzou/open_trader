"""WS exchange time and actual local BUY acceptance at public production seams."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from test_lp_async_source_contract import independent_watchdog, CID
from test_lp_async_read_runtime_contract import Clock
from test_lp_orderbook_ws_487 import wire_book
from open_trader.polymarket_lp_orderbook import OrderBooks


@pytest.mark.parametrize('stamp',[
    '1735689600123','1735689600123.456','1735689600123.99999999999999999999',
    None,'bad','NaN','Infinity','-1','253402300800000','1e1000',
],ids=['exchange-ms','exchange-us','no-round-up','missing','bad','nan','infinite','negative','year10000','huge'])
def test_ws_market_book_exports_exchange_utc_timestamp(stamp):
    clock=Clock();books=OrderBooks(clock=clock,utc_now=clock.utc)
    books.subscribe({'yes':CID});books.connected()
    event=wire_book(timestamp=stamp)
    if stamp is None:event.pop('timestamp')
    books.handle(event)
    result=books.market_book('yes')
    if stamp not in ('1735689600123','1735689600123.456','1735689600123.99999999999999999999'):
        assert result is None and books.snapshot('yes') is None
        return
    microseconds={'1735689600123':123000,'1735689600123.456':123456,
        '1735689600123.99999999999999999999':123999}[stamp]
    expected=datetime(2025,1,1,0,0,0,microseconds,tzinfo=UTC)
    assert result['timestamp']==expected
    assert result['timestamp']<expected+timedelta(microseconds=1)
    assert result['timestamp']!=result['received_at']
    assert result['source_timestamp']==D(stamp)
    assert books.snapshot('yes')['timestamp']==D(stamp)
    books.handle(wire_book(timestamp='1735689600122'))
    assert books.market_book('yes') is None  # numeric regression still invalidates the source


async def wait_for(predicate, drive=None):
    import asyncio
    async with asyncio.timeout(4):
        while not predicate():
            if drive:drive()
            await asyncio.sleep(.001)


def milliseconds(value):
    delta=value-datetime(1970,1,1,tzinfo=UTC)
    return str(D(delta.days*86400+delta.seconds)*1000+D(delta.microseconds)/1000)


async def local_rig(tmp_path,monkeypatch,mode,url,*,manual=False):
    """Existing observed-pool owner, actual SDK submission, external books/time only."""
    import asyncio,json
    from polymarket.models.clob.order_book import OrderBookLevel
    from polymarket.models.clob.order_response import AcceptedOrder
    from tests.test_lp_observed_pools import AsyncPoolRig
    import websockets.asyncio.client
    connect=websockets.asyncio.client.connect
    monkeypatch.setattr(websockets.asyncio.client,'connect',lambda target,**kw:connect(url,**kw))
    rig=AsyncPoolRig(tmp_path,monkeypatch,trial='8',income='0')
    try:
        rig.live.request_running(False)
        await wait_for(lambda:not rig.live.control.status().requested_running
            and rig.live.control.status().save_state=='saved',rig.live.poll)
        rig.live.configure(dict(budget_usd='8',target_buy_count=3,buy_price_level=2))
        await wait_for(lambda:rig.live.policy.buy_price_level==2,rig.live.run_once)
        rig.market('A',minimum='8')
        rig.book_time=rig.utc();rig.upper=D(100)
        original_book=rig.public.get_order_book
        def book(*,token_id):
            original=original_book(token_id=token_id)
            own=sum((o['original_size']-o['size_matched'] for o in rig.sdk.orders
                if o['asset_id']==token_id and o['side']=='BUY'),D(0))
            return original.model_copy(update=dict(timestamp=rig.book_time,
                bids=(OrderBookLevel(price=D('.41'),size=rig.upper),OrderBookLevel(price=D('.40'),size=D(100)+own))))
        rig.public.get_order_book=book
        direction=rig.markets['A']['direction']
        direction['book'].update(bids=[dict(price='.41',size='100'),dict(price='.40',size='100')],timestamp=rig.utc())
        post=rig.sdk.post_order
        def accepted(signed):
            rig.tick(.001)  # observed native acceptance is later than captured submit source
            response=post(signed)
            oid=response['order_id']
            rows=[]
            for row in rig.sdk.orders:
                if row['id']==oid:
                    row={**row,'price':signed['price'],'original_size':signed['size'],
                        'order_type':signed['order_type'],'expiration':signed.get('expiration'),
                        'created_at':rig.utc()}
                    rig.history[oid]=row
                rows.append(row)
            rig.sdk.orders=tuple(rows)
            return AcceptedOrder(order_id=oid,status='live',making_amount=D(0),taking_amount=D(0),trade_ids=(),transactions_hashes=())
        rig.sdk.post_order=accepted
        if manual:
            rig.store.lp_save_price_history(rig.cid('A'),'tA',[],dict(state='known',amplitude=D('.005'),
                checked_at=rig.utc(),valid_until=rig.utc()+timedelta(hours=24)))
            from tests.test_lp_d2_manual_bridge import entry
            result=rig.execution.lp_submit_entry(entry(rig.dates,market_id='mA',condition_id=rig.cid('A'),
                token_id='tA',price='.40',quantity='20'))
            assert result['state']=='entry_open' and result['submit_status']=='accepted',result
        else:
            rig.live.request_running(True)
            await wait_for(lambda:rig.live.state.snapshot().current.effective_running,rig.live.poll)
            result=rig.live.run_once(rig.capture(),pool='trial')
            await wait_for(lambda:len(rig.sdk.posts)==1,lambda:rig.live.run_once(rig.capture(),pool='trial'))
        # Await the actual receipt and its production registration, never install owner facts.
        await wait_for(lambda:any(o.key[3].startswith('new-A') for o in rig.live.state.snapshot().orders),rig.live.run_once)
        order=next(o for o in rig.live.state.snapshot().orders if o.key[3].startswith('new-A'))
        assert order.original_size.value==20 and rig.sdk.posts[0]['price']==D('.40')
        rig.oid=order.key[3];rig.accepted_at=order.source.checked_at
        if manual:
            assert not rig.live.state.snapshot().baselines
            rig.original_baseline=None
            roles=rig.store.lp_load_observed_pool_roles(rig.account)
            rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=roles['revision'],
                roles={rig.cid('A'):dict(pool='trial',episode=rig.oid)})
        else:
            rig.original_baseline=next(iter(rig.live.state.snapshot().baselines.values()))
            assert rig.original_baseline.bid_level==2
            assert rig.original_baseline.bid_levels==((D('.41'),D(100)),(D('.40'),D(100)))
        rig.live.configure(dict(orderbook_mode=mode))
        await wait_for(lambda:rig.live.policy.orderbook_mode==mode,rig.live.run_once)
        return rig
    except BaseException:
        await asyncio.to_thread(rig.close)
        raise



def packet(rig,when,upper=100):
    return dict(event_type='book',market=rig.cid('A'),asset_id='tA',timestamp=milliseconds(when),
        bids=[dict(price='.41',size=str(upper)),dict(price='.40',size='120')],
        asks=[dict(price='.42',size='10000')])


async def publish_book(rig,outbound,when,upper,mode,*,ws_when=None):
    rig.book_time=when;rig.upper=D(upper);rig.tick(6)
    if mode!='off':
        await outbound.put(packet(rig,when,upper))
        await wait_for(lambda:(value:=rig.live.read_runtime.orderbook_snapshot('tA')) is not None
            and value['timestamp']==when and {r['price']:r['size'] for r in value['bids']}=={D('.41'):D(upper),D('.40'):D(120)})
    key=(rig.adapter.config.wallet_address,rig.cid('A'),'tA',rig.oid)
    def settled():
        state=rig.live.state.snapshot();observation=state.observation
        if observation is None:return False
        source=observation.source_frame.sources.get(f'market_book:{rig.cid("A")}:tA')
        value=source.value.get('book') if source and source.complete and source.value else None
        return (key in state.yields and state.yields[key].evaluated_at==rig.utc()
            and observation.orders_complete and observation.read_ticket.trade_version_at_start==state.trade_version
            and observation.order_view.orders_source.checked_at==rig.utc() and value is not None
            and value['timestamp']==when and {r['price']:r['size'] for r in value['bids']}=={D('.41'):D(upper),D('.40'):D(120)}
            and (value.get('transport')=='ws')==(mode=='use'))
    def drive_publication():
        rig.live.run_once()
        if settled():return
        status=rig.live.read_runtime.status();due=status['next_dependency_monotonic']
        # Current account proof may require the next owned read deadline.
        if (status['ready_count']==0 and not any(s['active'] for s in status['scopes'])
                and status['observed_monotonic']>=rig.seconds and rig.live.status()['native_inflight']==0
                and due is not None and due>rig.seconds):
            rig.tick(max(due-rig.seconds,.000001))  # The fixture UTC clock has microsecond resolution.
    try:await wait_for(settled,drive_publication)
    except TimeoutError:
        state=rig.live.state.snapshot();observation=state.observation
        source=observation.source_frame.sources.get(f'market_book:{rig.cid("A")}:tA') if observation else None
        value=source.value.get('book') if source and source.value else None
        print('publication trace',dict(now=rig.utc(),yield_at=state.yields.get(key).evaluated_at if key in state.yields else None,
            complete=observation.orders_complete if observation else None,trade=state.trade_version,
            ticket=observation.read_ticket.trade_version_at_start if observation else None,
            account_at=observation.order_view.orders_source.checked_at if observation else None,
            source_time=value.get('timestamp') if value else None,top=value.get('bids') if value else None,
            transport=value.get('transport') if value else None,next_dependency=rig.live.read_runtime.status()['next_dependency_monotonic']))
        raise
    if mode=='shadow':
        # Establish independent WS input after the ordinary REST completion/possible seed.
        await outbound.put(dict(event_type='tick_size_change',market=rig.cid('A'),asset_id='tA',timestamp='1'))
        await wait_for(lambda:rig.live.read_runtime.orderbook_snapshot('tA') is None)
        await outbound.put(packet(rig,ws_when if ws_when is not None else when,upper))
        await wait_for(lambda:(value:=rig.live.read_runtime.orderbook_snapshot('tA')) is not None
            and value['timestamp']==(ws_when if ws_when is not None else when)
            and value['bids'][0]['size']==upper)
    return key


@pytest.mark.parametrize('mode',['use','off'])
@pytest.mark.parametrize('timing',['equal','after','before'])
def test_local_buy_use_respects_exchange_acceptance_time(tmp_path,monkeypatch,mode,timing):
    import asyncio,json
    from websockets.asyncio.server import serve
    async def scenario():
        outbound=asyncio.Queue()
        async def server(ws):
            await ws.recv()
            while True:
                message=await outbound.get()
                if message is None:return
                await ws.send(json.dumps(message))
        async with serve(server,'127.0.0.1',0) as listener:
            rig=None
            try:
                rig=await local_rig(tmp_path,monkeypatch,mode,f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
                when=rig.accepted_at+timedelta(milliseconds={'equal':0,'after':1,'before':-1}[timing])
                key=await publish_book(rig,outbound,when,101 if timing=='before' else 100,mode)
                handles=rig.live.run_protection_once();assert handles==() and rig.sdk.cancels==[]
                snapshot=rig.live.state.snapshot()
                if timing=='before':assert rig.original_baseline.key not in snapshot.progress
                else:assert snapshot.progress[rig.original_baseline.key].state=='monitoring'
                assert snapshot.baselines[rig.original_baseline.key] is rig.original_baseline
                assert snapshot.read_failures[key].consecutive_failures==0
                assert all(f.data_failures==0 for f in snapshot.failure_states.values())
                key=await publish_book(rig,outbound,rig.accepted_at+timedelta(milliseconds=2),101,mode)
                handles=rig.live.run_protection_once()
                for handle in handles:
                    await asyncio.to_thread(handle.result,3);await asyncio.to_thread(handle._thread.join,3)
                    assert handle.done()
                await wait_for(lambda:bool(rig.sdk.cancels))
                assert rig.sdk.cancels==[(rig.oid,)] and len(rig.sdk.posts)==1
                snapshot=rig.live.state.snapshot()
                assert snapshot.baselines[rig.original_baseline.key] is rig.original_baseline
                assert snapshot.read_failures[key].consecutive_failures==0
                assert all(f.data_failures==0 for f in snapshot.failure_states.values())
                targets=[t for p in snapshot.progress.values() for t in p.cancel_targets]
                assert len(targets)==1 and targets[0].reason=='bid_level_changed'
                rig.live.run_protection_once();assert rig.sdk.cancels==[(rig.oid,)]
            finally:
                await outbound.put(None)
                if rig:assert await asyncio.to_thread(rig.close) is None
    asyncio.run(scenario())


@pytest.mark.parametrize('timing',['before','equal','after','missing','bad','out-of-range','wrong-condition','wrong-token'])
def test_shadow_matches_use_local_buy_acceptance_gate(tmp_path,monkeypatch,timing):
    import asyncio,json
    from websockets.asyncio.server import serve
    async def scenario():
        outbound=asyncio.Queue()
        async def server(ws):
            await ws.recv()
            while True:
                message=await outbound.get()
                if message is None:return
                await ws.send(json.dumps(message))
        async with serve(server,'127.0.0.1',0) as listener:
            rig=None
            try:
                rig=await local_rig(tmp_path,monkeypatch,'shadow',f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}')
                ws_time=rig.accepted_at+timedelta(milliseconds={'before':-1,'equal':0}.get(timing,1))
                await publish_book(rig,outbound,rig.accepted_at+timedelta(milliseconds=2),100,'shadow',ws_when=ws_time)
                rig.live.run_protection_once();assert rig.sdk.cancels==[]
                key=await publish_book(rig,outbound,rig.accepted_at+timedelta(milliseconds=3),101,'shadow',ws_when=ws_time)
                if timing in ('missing','bad','out-of-range','wrong-condition','wrong-token'):
                    await outbound.put(dict(event_type='tick_size_change',market=rig.cid('A'),asset_id='tA',timestamp='1'))
                    await wait_for(lambda:rig.live.read_runtime.orderbook_snapshot('tA') is None)
                    rig.tick(.001)
                    invalid=packet(rig,ws_time,101)
                    if timing=='missing':invalid.pop('timestamp')
                    elif timing=='bad':invalid['timestamp']='NaN'
                    elif timing=='out-of-range':invalid['timestamp']='1e1000'
                    elif timing=='wrong-condition':invalid['market']='wrong'
                    else:invalid['asset_id']='wrong'
                    await outbound.put(invalid)
                    # An external marker proves the preceding invalid frame was received, not a sleep.
                    await outbound.put(dict(event_type='tick_size_change',market=rig.cid('A'),asset_id='tA',timestamp='2'))
                    await wait_for(lambda:(item:=rig.live.read_runtime.status()['orderbook']['tokens']['items'][0])['fallback_reason']=='tick_size_change'
                        and item['fallback_at']==rig.utc().isoformat())
                    assert rig.live.read_runtime.orderbook_snapshot('tA') is None
                before=rig.live.state.snapshot()
                rig.live.read_runtime.compare_shadow('tA',dict(state='triggered'),price=D('.40'),own_remaining=D(20),
                    bid_level=rig.original_baseline.bid_level,baseline_levels=rig.original_baseline.bid_levels,
                    condition_id=rig.cid('A'),accepted_at=rig.accepted_at)
                assert rig.live.state.snapshot()==before  # pure comparison has no financial/failure power
                assert rig.sdk.cancels==[]
                handles=rig.live.run_protection_once()
                for handle in handles:
                    await asyncio.to_thread(handle.result,3);await asyncio.to_thread(handle._thread.join,3)
                    assert handle.done()
                await wait_for(lambda:bool(rig.sdk.cancels))
                report=rig.live.read_runtime.status()['orderbook']['shadow'];last=report['last']
                assert last['rest_can_cancel']
                usable=timing in ('equal','after')
                assert last['ws_can_cancel'] is usable and last['consistent'] is usable and last['comparable'] is usable
                assert report['assessment']!='PASS'
                after=rig.live.state.snapshot()
                assert after.baselines[rig.original_baseline.key] is rig.original_baseline
                assert after.read_failures[key].consecutive_failures==0
                assert all(f.data_failures==0 for f in after.failure_states.values())
                assert rig.sdk.cancels==[(rig.oid,)] and len(rig.sdk.posts)==1
                rig.live.run_protection_once();assert rig.sdk.cancels==[(rig.oid,)]
            finally:
                await outbound.put(None)
                if rig:assert await asyncio.to_thread(rig.close) is None
    asyncio.run(scenario())


def test_local_manual_buy_first_baseline_uses_ws_exchange_time(tmp_path,monkeypatch):
    import asyncio,json
    from websockets.asyncio.server import serve
    async def scenario():
        outbound=asyncio.Queue()
        async def server(ws):
            await ws.recv()
            while True:
                message=await outbound.get()
                if message is None:return
                await ws.send(json.dumps(message))
        async with serve(server,'127.0.0.1',0) as listener:
            rig=None
            try:
                rig=await local_rig(tmp_path,monkeypatch,'use',f'ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}',manual=True)
                key=await publish_book(rig,outbound,rig.accepted_at-timedelta(milliseconds=1),101,'use')
                assert rig.live.run_protection_once()==()
                assert not rig.live.state.snapshot().baselines and rig.sdk.cancels==[]
                assert rig.live.state.snapshot().read_failures[key].consecutive_failures==0
                await publish_book(rig,outbound,rig.accepted_at+timedelta(milliseconds=1),100,'use')
                assert rig.live.run_protection_once()==()
                baseline,=rig.live.state.snapshot().baselines.values()
                assert baseline.bid_level==2 and baseline.bid_levels==((D('.41'),D(100)),(D('.40'),D(100)))
                assert rig.sdk.cancels==[] and len(rig.sdk.posts)==1
                await publish_book(rig,outbound,rig.accepted_at+timedelta(milliseconds=2),101,'use')
                handles=rig.live.run_protection_once()
                for handle in handles:
                    await asyncio.to_thread(handle.result,3);await asyncio.to_thread(handle._thread.join,3)
                    assert handle.done()
                await wait_for(lambda:bool(rig.sdk.cancels))
                snapshot=rig.live.state.snapshot()
                assert snapshot.baselines[baseline.key] is baseline
                assert snapshot.read_failures[key].consecutive_failures==0
                assert all(f.data_failures==0 for f in snapshot.failure_states.values())
                assert rig.sdk.cancels==[(rig.oid,)] and len(rig.sdk.posts)==1
                targets=[t for p in snapshot.progress.values() for t in p.cancel_targets]
                assert len(targets)==1 and targets[0].reason=='bid_level_changed'
                rig.live.run_protection_once();assert rig.sdk.cancels==[(rig.oid,)]
            finally:
                await outbound.put(None)
                if rig:assert await asyncio.to_thread(rig.close) is None
    asyncio.run(scenario())
