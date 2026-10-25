"""LP-owned cancellable reads, using pinned SDK builders and parsers only.

Clients and authentication are capabilities supplied by the existing owner.
This module never creates a SecureClient or changes authentication state.
"""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
import time
import uuid
import threading
from email.utils import parsedate_to_datetime
import math
import zlib
import httpx
from .polymarket_lp_market_reader import SourceResult, freeze

@dataclass(frozen=True)
class SourceRequest:
    source: str
    account: str
    condition_id: str = ''
    token_id: str = ''
    order_id: str = ''
    request_id: str = ''
    auth: object = None

class ProviderLimits:
    """Shared provider/account waits; never reset by order or pool rotation."""
    def __init__(self):
        self._lock=threading.Lock()
        self._limits={}

    def observe(self, provider, account, deadline):
        with self._lock:
            key=(provider,account.casefold())
            self._limits[key]=max(self._limits.get(key,0),deadline)

    def not_before(self, provider, account):
        with self._lock:return self._limits.get((provider,account.casefold()),0)

class ReadResponseError(Exception):
    def __init__(self, facts, retry_at):
        super().__init__('provider_error')
        self.facts, self.retry_at=facts,retry_at

class ReadLimitError(ValueError):
    """The complete branch cannot fit its bounded acquisition envelope."""

@dataclass
class _Budget:
    response_limit: int
    byte_limit: int
    page_limit: int
    item_limit: int
    bytes: int = 0
    pages: int = 0
    items: int = 0

    def take_items(self,count):
        self.items+=count
        if self.items>self.item_limit:raise ReadLimitError('branch_items')

_budget=ContextVar('lp_source_budget')

class _OwnedCloseStream(httpx.AsyncByteStream):
    """Keep release owned until the transport has actually finished closing.

    HTTPX marks its response closed before awaiting stream.aclose(), including
    the implicit EOF close in aiter_raw(). Install this boundary before reading
    so native cancellation cannot abandon that one-way transition.
    """
    def __init__(self, stream):
        self._stream=stream
        self._close_task=None

    async def __aiter__(self):
        async for chunk in self._stream:yield chunk

    async def aclose(self):
        if self._close_task is None:
            self._close_task=asyncio.create_task(self._stream.aclose())
        cancellation=None
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError as exc:
                cancellation=exc
            except Exception:
                # Retrieve below even when cancellation and failure coincide.
                break
        try:
            self._close_task.result()
        except BaseException as exc:
            if cancellation is not None:raise cancellation from exc
            raise
        if cancellation is not None:raise cancellation

class LPAsyncSourcePort:
    def __init__(self, clients, *, clock=time.monotonic, utc_now=lambda: datetime.now(UTC), provider_limits=None,auth_resolver=None,signature_type=2,spender="spender",response_bytes=4*1024*1024,branch_bytes=16*1024*1024,page_limit=128,item_limit=20000):
        self.clients, self.clock, self.utc_now = clients, clock, utc_now
        self.auth_resolver,self.signature_type,self.spender=auth_resolver,signature_type,spender
        self._limits=(response_bytes,branch_bytes,page_limit,item_limit)
        self.provider_limits=provider_limits if provider_limits is not None else ProviderLimits()

    def not_before(self, provider, account):
        return self.provider_limits.not_before(provider,account)

    async def _json(self, service, path, params, request, deadline):
        if self.clock() >= deadline:
            raise TimeoutError('attempt_deadline')
        budget=_budget.get()
        budget.pages+=1
        if budget.pages>budget.page_limit:raise ReadLimitError('branch_pages')
        params={k:('true' if v else 'false') if type(v) is bool else v
            for k,v in (params or {}).items() if v is not None}
        headers={'x-lp-order':request.order_id,'x-lp-source':request.source,'accept-encoding':'identity'}
        if request.source in ('balance','open_orders','order','fills','scoring') and self.auth_resolver is not None:
            headers.update(self.auth_resolver('GET',path,None))
        async with self.clients[service].stream('GET',path,params=params,headers=headers) as response:
            response.stream=_OwnedCloseStream(response.stream)
            if response.status_code >= 400:
                facts={'status':response.status_code}
                retry_at=None
                raw=response.headers.get('Retry-After')
                try:
                    seconds=float(raw)
                    if not math.isfinite(seconds) or seconds<0:raise ValueError('invalid_retry')
                    facts['retry_after_seconds']=seconds
                except (TypeError,ValueError):
                    seconds=None
                    if raw:
                        try:
                            target=parsedate_to_datetime(raw)
                            date=parsedate_to_datetime(response.headers['Date']) if 'Date' in response.headers else self.utc_now()
                            if target.tzinfo is None or date.tzinfo is None:raise ValueError('naive_retry')
                            seconds=max(0,(target-date).total_seconds())
                            facts.update(retry_after_at=target,response_date=date)
                        except (ValueError,TypeError,OverflowError):pass
                if seconds is not None:
                    retry_at=self.clock()+seconds
                    self.provider_limits.observe(service,request.account,retry_at)
                raise ReadResponseError(facts,retry_at)
            data=bytearray()
            async for chunk in self._decoded_chunks(response,budget):
                if self.clock()>=deadline:raise TimeoutError('attempt_deadline')
                if len(data)+len(chunk)>budget.response_limit:raise ReadLimitError('response_bytes')
                if budget.bytes+len(chunk)>budget.byte_limit:raise ReadLimitError('branch_bytes')
                budget.bytes+=len(chunk);data.extend(chunk)
            return json.loads(data)

    async def _decoded_chunks(self,response,budget):
        """Bound decompression output before JSON or an expanding body buffer."""
        if response.is_stream_consumed:
            # In-memory external transports may supply an already decoded body.
            yield response.content
            return
        encoding=response.headers.get('content-encoding','identity').lower().strip()
        if encoding in ('','identity'):
            async for chunk in response.aiter_raw(chunk_size=65536):yield chunk
            return
        if encoding not in ('gzip','deflate'):raise ReadLimitError('content_encoding')
        decoder=zlib.decompressobj(31 if encoding=='gzip' else 15)
        produced=0
        async for raw in response.aiter_raw(chunk_size=65536):
            while raw:
                allowance=min(budget.response_limit-produced,budget.byte_limit-budget.bytes)+1
                if allowance<=0:raise ReadLimitError('response_bytes')
                chunk=decoder.decompress(raw,allowance)
                produced+=len(chunk)
                if produced>budget.response_limit:raise ReadLimitError('response_bytes')
                if chunk:yield chunk
                raw=decoder.unconsumed_tail
                if decoder.unused_data:
                    if encoding!='gzip':raise ValueError('compressed_trailing_data')
                    raw=decoder.unused_data;decoder=zlib.decompressobj(31)
        if not decoder.eof:raise ValueError('compressed_body_incomplete')

    async def _keyset(self, spec, request, deadline):
        rows=[]; cursor=None
        while True:
            params=dict(spec.base_params or {},limit=100)
            if cursor is not None:params[spec.cursor_param]=cursor
            payload=spec.parse_page(await self._json(spec.service,spec.path,params,request,deadline))
            _budget.get().take_items(len(payload.items))
            rows.extend(payload.items)
            if payload.server_next_cursor is None:return tuple(rows)
            cursor=payload.server_next_cursor
            await asyncio.sleep(0)

    async def _rewards(self, request, deadline, sponsored):
        from polymarket._internal.actions import rewards
        rows=[];cursor=None
        while True:
            path,params=rewards.build_list_market_rewards_request(condition_id=request.condition_id,
                sponsored=sponsored,cursor=cursor)
            payload=await self._json('clob',path,params,request,deadline)
            page=rewards.parse_market_rewards_page(payload)
            _budget.get().take_items(len(page.items))
            # SDK MarketRewardConfig omits the optional venue configuration ID.
            # Retain that identity after SDK validation, so repeated IDs dedupe;
            # equal anonymous configs retain their original multiplicity.
            from .polymarket_trading import _model_dict
            for parsed, raw in zip(page.items, payload['data'], strict=True):
                value=_model_dict(parsed)
                configs=[]
                for config, original in zip(value['rewards_config'],raw.get('rewards_config',()),strict=True):
                    configs.append(dict(config, **({'id':original['id']} if original.get('id') is not None else {})))
                value['rewards_config']=configs
                rows.append(value)
            if not page.has_more:
                if not rows:raise ValueError('reward_row_missing')
                return tuple(rows)
            if not page.next_cursor:raise ValueError('missing_cursor')
            cursor=page.next_cursor
            await asyncio.sleep(0)

    async def _metadata(self, request, deadline):
        from polymarket._internal.actions import gamma
        from .polymarket_trading import project_lp_market_metadata, _model_dict, _collect
        rows=await self._keyset(gamma.list_markets_spec(condition_ids=(request.condition_id,)),request,deadline)
        if not rows:
            rows=await self._keyset(gamma.list_markets_spec(condition_ids=(request.condition_id,),closed=True),request,deadline)
        events={}
        for row in rows:
            value=_model_dict(row) or {}
            references=_collect(value.get('events'))
            if len(references)!=1:continue
            reference=_model_dict(references[0]) or {}
            key=str(reference.get('id') or '')
            if not key:continue
            if key.isascii() and key.isdecimal() and str(int(key))==key:
                found=()
                for closed in (False,True):
                    found=await self._keyset(gamma.list_events_spec(ids=(int(key),),closed=closed),request,deadline)
                    if found:break
                for event in found:
                    value=_model_dict(event) or {}
                    if str(value.get('id'))==key:events[key]=value
            else:
                spec=gamma.get_event_spec(id=key)
                event=spec.parse(await self._json(spec.service,spec.path,spec.params,request,deadline))
                value=_model_dict(event) or {}
                if str(value.get('id'))==key:events[key]=value
        failed={}
        projected=project_lp_market_metadata(rows,(request.condition_id,),events,{},failed,self.utc_now())
        value=projected.get(request.condition_id)
        if not value or failed:raise ValueError('metadata_missing')
        return value

    async def read(self, request, deadline):
        identity=request.request_id or uuid.uuid4().hex
        token=_budget.set(_Budget(*self._limits))
        try:
            if request.source=='market':
                results=await asyncio.gather(*(self.read(replace(request,source=source,
                    request_id=identity+':'+source),deadline) for source in
                    ('book','metadata','native_rewards','sponsored_rewards')))
                if not all(r.complete and not r.error for r in results):raise ValueError('market_incomplete')
                value=market_value(request.condition_id,dict(zip(
                    ('book','metadata','native_rewards','sponsored_rewards'),results)))
                if value['rewards']['complete'] is not True:raise ValueError('rewards_unknown')
            elif request.source=='metadata':value=await self._metadata(request,deadline)
            elif request.source in ('native_rewards','sponsored_rewards'):
                value=await self._rewards(request,deadline,request.source=='sponsored_rewards')
                from .polymarket_trading import _model_dict
                value=tuple(_model_dict(row) for row in value)
            elif request.source=='book':
                from polymarket._internal.actions import clob
                from .polymarket_trading import _lp_book
                path,params=clob.build_order_book_request(token_id=request.token_id)
                value=_lp_book(clob.parse_order_book(await self._json('clob',path,params,request,deadline)))
                if value is None or value.get('condition_id')!=request.condition_id or value.get('token_id')!=request.token_id:
                    raise ValueError('book_identity_unknown')
                value.update(checked_at=self.utc_now(),received_at=self.utc_now())
            elif request.source=='positions':
                from polymarket._internal.actions import data
                from .polymarket_trading import _lp_position
                spec=data.list_positions_spec(user=request.account,size_threshold=0)
                rows=[];offset=0
                while True:
                    page=spec.parse_items(await self._json(spec.service,spec.path,
                        dict(spec.base_params or {},limit=100,offset=offset),request,deadline))
                    _budget.get().take_items(len(page))
                    from .polymarket_trading import _model_dict
                    for model in page:
                        row=_lp_position(model) or {};raw=_model_dict(model) or {}
                        row['account_id']=str(raw.get('proxy_wallet') or raw.get('user') or request.account).casefold()
                        rows.append(row)
                    if len(page)<100:break
                    offset+=100
                    await asyncio.sleep(0)
                value=tuple(rows)
            elif request.source=='scoring':
                from polymarket._internal.actions import rewards
                path,params=rewards.build_get_order_scoring_request(order_id=request.order_id)
                value=rewards.parse_order_scoring(await self._json('clob',path,params,request,deadline))
            elif request.source=='fills':
                from polymarket._internal.actions import account
                from .polymarket_trading import _lp_trade,_model_dict
                rows=[];cursor=None
                while True:
                    path,params=account.build_list_account_trades_request(market=request.condition_id,cursor=cursor)
                    page=account.parse_account_trades_page(await self._json('clob',path,params,request,deadline))
                    _budget.get().take_items(len(page.items))
                    for model in page.items:
                        row,raw=_lp_trade(model),_model_dict(model) or {}
                        if row is None or row['condition_id']!=request.condition_id:raise ValueError('protection_fill_identity')
                        row['maker_address']=raw.get('maker_address');rows.append(row)
                    if not page.has_more:break
                    if not page.next_cursor:raise ValueError('missing_cursor')
                    cursor=page.next_cursor
                    await asyncio.sleep(0)
                value=tuple(rows)
            elif request.source in ('open_orders','order','balance'):
                from polymarket._internal.actions import account
                from .polymarket_trading import _lp_order, _model_dict
                if request.source=='balance':
                    from decimal import Decimal
                    path,params=account.build_balance_allowance_request(asset_type='COLLATERAL',token_id=None,signature_type=self.signature_type)
                    model=account.parse_balance_allowance(await self._json('clob',path,params,request,deadline))
                    row=_model_dict(model)
                    allowances=row.get('allowances')
                    allowance=next((Decimal(v) for k,v in (allowances or {}).items()
                        if isinstance(k,str) and k.casefold()==self.spender.casefold()),Decimal(0))
                    value=(Decimal(row['balance'])/1000000,allowance/1000000)
                elif request.source=='order':
                    path,params=account.build_get_order_request(order_id=request.order_id)
                    model=account.parse_open_order(await self._json('clob',path,params,request,deadline))
                    value=_lp_order(model)
                    raw=_model_dict(model) or {}
                    size,matched=value['original_size'],value['size_matched']
                    if (str(raw.get('maker_address') or '').casefold()!=request.account.casefold()
                            or not 0<=matched<=size or size<=0 or value['side'] not in ('BUY','SELL')
                            or value['price'] is None or not 0<value['price']<1
                            or value['status'] not in ('LIVE','OPEN','ACCEPTED','PARTIALLY_FILLED','CANCELED','CANCELLED',
                                'FILLED','MATCHED','EXPIRED','REJECTED','FAILED')):raise ValueError('protection_order_unknown')
                    if not value or (value['order_id'],value['condition_id'],value['token_id'])!=(request.order_id,request.condition_id,request.token_id):
                        raise ValueError('exact_order_identity')
                else:
                    rows=[];cursor=None
                    while True:
                        path,params=account.build_list_open_orders_request(cursor=cursor)
                        page=account.parse_open_orders_page(await self._json('clob',path,params,request,deadline))
                        _budget.get().take_items(len(page.items))
                        for model in page.items:
                            original=_model_dict(model) or {}
                            row=_lp_order(model) or {}
                            row.update(account_id=str(original.get('maker_address') or request.account).casefold())
                            rows.append(row)
                        if not page.has_more:break
                        if not page.next_cursor:raise ValueError('missing_cursor')
                        cursor=page.next_cursor
                        await asyncio.sleep(0)
                    value=tuple(rows)
            else:raise ValueError('unsupported_source')
            return SourceResult(identity,freeze(value),checked_at=self.utc_now())
        except Exception as exc:
            return SourceResult(identity,complete=False,error=type(exc).__name__,failed_at=self.utc_now(),
                retry_at=getattr(exc,'retry_at',None),error_facts=freeze(getattr(exc,'facts',{})),
                diagnostics=freeze(dict(exception_type=type(exc).__name__)))
        finally:_budget.reset(token)

    async def aclose(self):
        for client in dict.fromkeys(self.clients.values()):
            await client.aclose()

def closed_position_value(condition,token,results):
    """A book 404 alone is never evidence that a position is non-tradable."""
    from collections.abc import Mapping
    metadata=results.get('metadata');book=results.get('book')
    value=metadata.value if metadata and metadata.complete and not metadata.error else None
    if (not isinstance(value,Mapping) or value.get('condition_id')!=condition or not value.get('market_id')
            or value.get('closed') is not True or value.get('accepting_orders') is not False
            or not any(isinstance(v,Mapping) and v.get('token_id')==token for v in value.get('outcomes',{}).values())
            or book is None or book.error_facts.get('status')!=404):return None
    return freeze(dict(state='non_tradable',metadata=value,book=None,rewards=None,book_absent=True,
        book_absence_status=404,book_checked_at=book.failed_at))


def market_value(condition, results):
    from .polymarket_trading import project_lp_selected_rewards
    native,sponsored=(results[k] for k in ('native_rewards','sponsored_rewards'))
    catalog=project_lp_selected_rewards((condition,),{
        (condition,False):(native.value,None,None),
        (condition,True):(sponsored.value,None,None)},min(native.checked_at,sponsored.checked_at))
    return freeze(dict(book=results['book'].value,metadata=results['metadata'].value,rewards=catalog))
