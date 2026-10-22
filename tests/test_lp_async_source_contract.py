"""HTTP-boundary contracts for the LP-owned pinned SDK adapter."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import json
import time
import httpx
import pytest
import faulthandler
from open_trader.polymarket_lp_async_source import LPAsyncSourcePort, SourceRequest

@pytest.fixture(autouse=True)
def independent_watchdog():
    # Native watchdog remains independent of every Python state/Condition lock,
    # including blocked public status and finally/close. It records all stacks
    # and exits this test process with a nonzero status; no retry is performed.
    faulthandler.dump_traceback_later(15,exit=True)
    try:yield
    finally:faulthandler.cancel_dump_traceback_later()

NOW = datetime(2026, 10, 21, tzinfo=UTC)
CID = '0x'+'1'*64
ASSET = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'

def market():
    return dict(id='42', conditionId=CID, slug='winner', question='Winner?',
        outcomes='["Yes","No"]', outcomePrices='["0.4","0.6"]',
        clobTokenIds='["yes","no"]', acceptingOrders=True, closed=False,
        feesEnabled=False, orderPriceMinTickSize=0.01, orderMinSize=1,
        rewardsMinSize=1, rewardsMaxSpread=10, events=[dict(id='7',slug='event')])

def reward(asset=ASSET):
    config=dict(id='config',asset_address=asset,start_date='2026-01-01T00:00:00Z',
        end_date='2026-12-31T00:00:00Z',rate_per_day='24')
    return dict(condition_id=CID,question='Winner?',rewards_max_spread=10,
        rewards_min_size='1',tokens=[dict(token_id='yes',outcome='Yes',price='0.4')],
        rewards_config=[config,config])

def book():
    return dict(market=CID,asset_id='yes',timestamp=str(int(NOW.timestamp()*1000)),
        bids=[dict(price='0.40',size='100')],asks=[dict(price='0.42',size='100')],
        min_order_size='1',tick_size='0.01',neg_risk=False,hash='b')

def test_sdk_read_projection_preserves_complete_source_facts():
    async def scenario():
        requests=[]
        mode=['ok']
        async def http(req):
            requests.append(req)
            path=req.url.path
            if path == '/markets/keyset':
                assert req.url.params.get_list('condition_ids') == [CID]
                if req.url.params.get('closed') != 'true':
                    value=dict(markets=[],next_cursor=None)
                else:
                    value=dict(markets=[market()],next_cursor=None)
            elif path == '/events/keyset':
                value=dict(events=[dict(id='7',slug='event',title='Event',ended=True,
                    startDate='2026-10-20T00:00:00Z',endDate='2026-10-21T00:00:00Z')],next_cursor=None)
            elif path == '/book': value=book()
            elif path.startswith('/rewards/markets/'):
                cursor=req.url.params.get('next_cursor')
                if cursor is None:value=dict(data=[],next_cursor='page2')
                elif cursor == 'page2':
                    if mode[0]=='failure':return httpx.Response(503)
                    value=dict(data=[reward('0x'+'9'*40 if mode[0]=='asset' else ASSET)],next_cursor='page3')
                else:value=dict(data=[reward('0x'+'9'*40 if mode[0]=='asset' else ASSET)],next_cursor='LTE=')
            else:raise AssertionError(path)
            return httpx.Response(200,json=value)
        async with httpx.AsyncClient(transport=httpx.MockTransport(http),base_url='http://127.0.0.1') as client:
            port=LPAsyncSourcePort(dict(gamma=client,clob=client),utc_now=lambda:NOW)
            req=SourceRequest('market','account',CID,'yes',request_id='attempt-1')
            result=await port.read(req,time.monotonic()+5)
            assert result.complete, result
            assert result.source_id == 'attempt-1'
            assert result.checked_at == NOW
            assert result.value['metadata']['condition_id'] == CID
            assert result.value['metadata']['market_id'] == '42'
            assert result.value['metadata']['fees_checked_at'] == NOW
            assert result.value['metadata']['event_ended'] is True
            assert result.value['rewards']['daily_pool_usd'] == Decimal('24')
            assert result.value['book']['received_at'] == NOW
            for failure in ('failure','asset'):
                mode[0]=failure
                failed=await port.read(replace(req,request_id=failure),time.monotonic()+5)
                assert not failed.complete
                assert failed.value is None or failed.value['rewards']['daily_pool_usd'] is None
    asyncio.run(scenario())

def test_retry_after_survives_sdk_errors_and_concurrent_requests():
    async def scenario():
        entered={name:asyncio.Event() for name in ('a','b')}
        async def http(req):
            name=req.url.params['token_id']
            if name=='network':raise httpx.ConnectError('credential-sentinel',request=req)
            entered[name].set()
            await entered['b' if name=='a' else 'a'].wait()
            headers=({'Retry-After':'7'} if name=='a' else
                {'Date':'Wed, 21 Oct 2026 00:00:00 GMT','Retry-After':'Wed, 21 Oct 2026 00:00:10 GMT'})
            return httpx.Response(429,headers=headers)
        async with httpx.AsyncClient(transport=httpx.MockTransport(http),base_url='http://127.0.0.1') as client:
            port=LPAsyncSourcePort(dict(clob=client),clock=lambda:100,utc_now=lambda:NOW)
            results=await asyncio.gather(*(port.read(SourceRequest('book','wallet',CID,name,request_id=name),105) for name in ('a','b')))
            assert {r.source_id:r.retry_at for r in results}=={'a':107,'b':110}
            assert all(r.error_facts['status']==429 for r in results)
            assert results[0].error_facts['retry_after_seconds']==7
            assert results[1].error_facts['retry_after_at']==datetime(2026,10,21,0,0,10,tzinfo=UTC)
            failed=await port.read(SourceRequest('book','wallet',CID,'network',request_id='network'),105)
            assert failed.retry_at is None
            assert 'credential-sentinel' not in repr(failed)
            # A second port shares the actual provider/account cooling scope.
            other=LPAsyncSourcePort(dict(clob=client),clock=lambda:100,utc_now=lambda:NOW,
                provider_limits=port.provider_limits)
            assert other.not_before('clob','wallet')==110
            assert other.not_before('clob','other-wallet')==0
    asyncio.run(scenario())

def test_size_page_and_item_limits_preserve_unknown_instead_of_truncation():
    import gzip
    class Stream(httpx.AsyncByteStream):
        def __init__(self,body,events):self.body=body;self.events=events
        async def __aiter__(self):
            for offset in range(0,len(self.body),17):yield self.body[offset:offset+17]
        async def aclose(self):self.events.append('closed')
    async def check(source,pages,limits,expected,*,compressed=False):
        events=[];requests=[]
        async def http(req):
            index=len(requests);requests.append(req)
            raw=json.dumps(pages[min(index,len(pages)-1)],separators=(',',':')).encode()
            body=gzip.compress(raw) if compressed else raw
            headers={'Content-Encoding':'gzip'} if compressed else {}
            return httpx.Response(200,headers=headers,stream=Stream(body,events))
        async with httpx.AsyncClient(transport=httpx.MockTransport(http),base_url='http://127.0.0.1') as client:
            port=LPAsyncSourcePort(dict(clob=client,data=client),utc_now=lambda:NOW,**limits)
            result=await port.read(SourceRequest(source,'account',CID,'yes',request_id='bounded'),time.monotonic()+5)
            assert result.complete is expected, result
            if not expected:assert result.value is None and result.error is not None
            assert len(events)==len(requests), (events,requests)
            return result,len(requests)
    async def scenario():
        await check('book',[dict(book(),padding='x'*4096)],dict(response_bytes=1024),False,compressed=True)
        page=dict(data=[reward()],next_cursor='next')
        end=dict(data=[reward()],next_cursor='LTE=')
        size=len(json.dumps(page,separators=(',',':')).encode())
        await check('native_rewards',[page,end],dict(response_bytes=size+100,branch_bytes=size+1),False)
        _,calls=await check('native_rewards',[dict(data=[],next_cursor='next')],dict(page_limit=2),False)
        assert calls==2
        await check('native_rewards',[dict(data=[reward(),reward()],next_cursor='LTE=')],dict(item_limit=1),False)
        positions=[dict(conditionId=CID,asset='yes',size='1',avgPrice='0.4')]*2
        await check('positions',[positions],dict(item_limit=1),False)
        raw_size=len(json.dumps(book(),separators=(',',':')).encode())
        valid,_=await check('book',[book()],dict(response_bytes=raw_size,branch_bytes=raw_size,page_limit=1,item_limit=1),True)
        assert valid.value['bids'][0]['size']==100
        pages=[dict(data=[],next_cursor='next'),end]
        sizes=[len(json.dumps(p,separators=(',',':')).encode()) for p in pages]
        valid,calls=await check('native_rewards',pages,dict(response_bytes=max(sizes),branch_bytes=sum(sizes),page_limit=2,item_limit=1),True)
        assert calls==2 and len(valid.value)==1
    asyncio.run(scenario())
