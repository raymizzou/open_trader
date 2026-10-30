"""LP-owned cancellable reads, using pinned SDK builders and parsers only.

Clients and authentication are capabilities supplied by the existing owner.
This module never creates a SecureClient or changes authentication state.
"""
import asyncio
import sys
from collections import deque
from contextvars import ContextVar
from contextlib import aclosing
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
    diagnostic_attempt: int = 0

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

class OwnedReadTask(asyncio.Task):
    """The actual source task owns request unwind, including library cleanup.

    Use only the public Task cancellation API. The first network cancellation
    is delivered normally. Once unwinding/closing starts, further cancellation
    requests are retained until the request has actually exited. No extra close
    task, private HTTPX state, or pool introspection is involved.
    """
    def __init__(self, coro):
        self.in_read = False
        self.cleanup = False
        self.cancel_requests = 0
        self.cancel_message = None
        self.record = None
        self.request_marker = None
        self.cleanup_result = 'not_started'
        self._cancel_balance_pending = False
        super().__init__(coro, loop=asyncio.get_running_loop())

    def cancel(self, msg=None):
        if self.done():
            return False
        if not self.in_read:
            return super().cancel(msg)
        self.cancel_requests += 1
        self.cancel_message = msg
        if self.record is not None:
            self.record.note('cancel')
        if self.cleanup or self.cancel_requests > 1:
            return True
        return super().cancel(msg)

    def cancelling(self):
        return max(self.cancel_requests, super().cancelling())

    def uncancel(self):
        # AnyIO uses balanced cancel/uncancel for its own network scopes.
        # Those internal cancellations must not become caller cancellation.
        self.cancel_requests = max(0, self.cancel_requests - 1)
        super().uncancel()
        if not self.cancel_requests and self.record is not None:
            self.record.note('uncancel')
        if self.in_read and not self.cleanup and not self._cancel_balance_pending:
            # A scope can balance several deliveries synchronously. Wait until
            # that whole boundary exits: Python 3.12 uncancel() does not rescind
            # an already queued injection if a later balance reduces it to zero.
            self._cancel_balance_pending = True
            self.get_loop().call_soon(self._deliver_unbalanced_cancel)
        return self.cancelling()

    def _deliver_unbalanced_cancel(self):
        self._cancel_balance_pending = False
        if self.in_read and self.cancel_requests and not self.cleanup and not self.done():
            # Deliver the remaining caller request, not a new counted request.
            # No library-scope message: an enclosing scope must not mistake it
            # for its own internal cancellation and swallow it again.
            super().cancel()


class _RequestRecord:
    """Fixed-schema values only. Never retain HTTP objects or trace info."""
    def __init__(self, port, request):
        self.port = port
        self.value = dict(source=request.source, attempt=request.diagnostic_attempt,
            sequence=port._request_sequence, phase='request', trace_state='started', phase_monotonic=None,
            started_monotonic=None, cleanup_started_monotonic=None,
            cleanup_completed_monotonic=None, cancel_requested_monotonic=None,
            ended_monotonic=None, cleanup_state='not_started', outcome='running')

    def note(self, event):
        try:
            now = float(self.port.clock())
            if not math.isfinite(now):
                return
            value = self.value
            if event == 'request':
                value['started_monotonic'] = value['phase_monotonic'] = now
            elif event == 'cancel':
                if value['cancel_requested_monotonic'] is None:
                    value['cancel_requested_monotonic'] = now
            elif event == 'uncancel':
                value['cancel_requested_monotonic'] = None
            elif event.startswith('cleanup.'):
                state = event.split('.')[1]
                value['cleanup_state'] = state
                if state == 'started' and value['cleanup_started_monotonic'] is None:
                    value['cleanup_started_monotonic'] = now
                elif state == 'completed':
                    value['cleanup_completed_monotonic'] = now
            elif event in ('complete', 'failed', 'cancelled'):
                value['outcome'] = event
                value['ended_monotonic'] = now
            elif event in ('headers.started', 'headers.complete', 'headers.failed',
                           'body.started', 'body.complete', 'body.failed'):
                value['phase'], value['trace_state'] = event.split('.')
                value['phase_monotonic'] = now
            self.port._stage_cleanup_records()
        except Exception:
            # Diagnostic failure cannot suppress cancellation or cleanup.
            pass


_request_owner = ContextVar('lp_request_owner', default=None)
_HTTP_PHASE_EVENTS = {f'http{version}.{phase}.{state}': f'{name}.{state}'
    for version in ('11', '2')
    for phase, name in (('receive_response_headers', 'headers'), ('receive_response_body', 'body'))
    for state in ('started', 'complete', 'failed')}
_HTTP_CLEANUP_EVENTS = {f'http{version}.response_closed.{state}': result
    for version in ('11', '2')
    for state, result in (('started', 'started'), ('complete', 'completed'), ('failed', 'failed'))}


async def _install_request_trace(request):
    owner = _request_owner.get()
    if owner is None:
        return
    previous = request.extensions.get('trace')
    marker, record = owner.request_marker, owner.record

    async def trace(event, info):
        # Bind each public callback to its actual request, including pagination.
        if owner.request_marker is marker:
            cleanup = _HTTP_CLEANUP_EVENTS.get(event)
            if cleanup is not None:
                owner.cleanup = True
                owner.cleanup_result = cleanup
                if record is not None:
                    record.note('cleanup.' + cleanup)
            elif record is not None and event in _HTTP_PHASE_EVENTS:
                record.note(_HTTP_PHASE_EVENTS[event])
        # Existing externally supplied traces retain their public behavior.
        # Our observations never inspect or serialize raw info.
        if previous is not None:
            await previous(event, info)
    request.extensions['trace'] = trace


class _OwnedCloseStream(httpx.AsyncByteStream):
    """Also cover external transports without a public httpcore trace."""
    def __init__(self, stream, owner):
        self._stream, self._owner = stream, owner
        self._iterator = None

    async def __aiter__(self):
        self._iterator = self._stream.__aiter__()
        async for chunk in self._iterator:
            yield chunk

    async def aclose(self):
        self._owner.cleanup = True
        self._owner.cleanup_result = 'started'
        record = self._owner.record
        if record is not None:
            record.note('cleanup.started')
        try:
            await self._close()
        except BaseException:
            self._owner.cleanup_result = 'failed'
            if record is not None:
                record.note('cleanup.failed')
            raise
        else:
            self._owner.cleanup_result = 'completed'
            if record is not None:
                record.note('cleanup.completed')

    async def _close(self):
        try:
            close = getattr(self._iterator, 'aclose', None)
            if close is not None:
                await close()
        finally:
            await self._stream.aclose()

class LPAsyncSourcePort:
    def __init__(self, clients, *, clock=time.monotonic, utc_now=lambda: datetime.now(UTC), provider_limits=None,auth_resolver=None,signature_type=2,spender="spender",response_bytes=4*1024*1024,branch_bytes=16*1024*1024,page_limit=128,item_limit=20000):
        self.clients, self.clock, self.utc_now = clients, clock, utc_now
        self.auth_resolver,self.signature_type,self.spender=auth_resolver,signature_type,spender
        self._limits=(response_bytes,branch_bytes,page_limit,item_limit)
        self.provider_limits=provider_limits if provider_limits is not None else ProviderLimits()
        self._request_sequence = 0
        self._record_attempt = 0
        self._current_records = {}
        self._record_history = deque(maxlen=32)
        self._cleanup_snapshot = None
        for client in dict.fromkeys(clients.values()):
            if _install_request_trace not in client.event_hooks['request']:
                client.event_hooks['request'].append(_install_request_trace)

    def not_before(self, provider, account):
        return self.provider_limits.not_before(provider,account)

    def financial_pool_status(self):
        """Bounded scalars from the pinned HTTPX/HTTPCore pool, never URLs/requests."""
        rows = []
        for name, limit in (('clob', 2), ('data', 1)):
            row = dict(service=name, limit=limit, connections=None, active=None, idle=None, closed=None)
            try:
                client = self.clients[name]
                # HTTPX has no public pool accessor; isolate the pinned transport seam.
                transport = client._transport_for_url(client.base_url)
                if type(transport) is not httpx.AsyncHTTPTransport:
                    raise ValueError('pool_unavailable')
                connections = transport._pool.connections
                if len(connections) > limit:
                    raise ValueError('pool_limit')
                row.update(connections=len(connections),
                    active=sum(not c.is_idle() and not c.is_closed() for c in connections),
                    idle=sum(c.is_idle() for c in connections), closed=client.is_closed)
            except Exception:
                pass
            rows.append(row)
        return rows

    def _stage_cleanup_records(self):
        snapshot = dict(attempt=self._record_attempt,
            current=[dict(record.value) for record in self._current_records.values()],
            history=[dict(row) for row in self._record_history])
        while len(json.dumps(snapshot, allow_nan=False).encode()) > 16 * 1024:
            if not snapshot['history']:
                return
            self._record_history.popleft()
            snapshot['history'].pop(0)
        # Atomic copied staging. Only the existing supervisor publishes status.
        self._cleanup_snapshot = snapshot

    def financial_cleanup_records(self, attempt):
        snapshot = self._cleanup_snapshot
        return snapshot if snapshot is not None and snapshot['attempt'] == attempt else None

    def _start_record(self, request):
        if (request.source not in ('balance', 'open_orders', 'positions')
                or type(request.diagnostic_attempt) is not int or request.diagnostic_attempt <= 0
                or request.diagnostic_attempt < self._record_attempt
                or request.source in self._current_records):
            return None
        self._request_sequence += 1
        self._record_attempt = request.diagnostic_attempt
        record = _RequestRecord(self, request)
        self._current_records[request.source] = record
        record.note('request')
        return record

    def _finish_record(self, record, outcome):
        if record is None:
            return
        record.note(outcome)
        if self._current_records.get(record.value['source']) is record:
            self._current_records.pop(record.value['source'])
            self._record_history.append(dict(record.value))
            self._stage_cleanup_records()

    async def _json(self, service, path, params, request, deadline):
        if self.clock() >= deadline:
            raise TimeoutError('attempt_deadline')
        budget=_budget.get()
        budget.pages+=1
        if budget.pages>budget.page_limit:raise ReadLimitError('branch_pages')
        params={k:('true' if v else 'false') if type(v) is bool else v
            for k,v in (params or {}).items() if v is not None}
        headers={'x-lp-order':request.order_id,'x-lp-source':request.source,'accept-encoding':'identity'}
        if request.source in ('balance','open_orders','order','fills','scoring','reward_share') and self.auth_resolver is not None:
            headers.update(self.auth_resolver('GET',path,None))
        owner = asyncio.current_task()
        owner.cleanup = False
        owner.cleanup_result = 'not_started'
        owner.request_marker = object()
        record = owner.record = self._start_record(request)
        try:
            async with self.clients[service].stream('GET',path,params=params,headers=headers) as response:
                response.stream=_OwnedCloseStream(response.stream, owner)
                if record is not None:
                    record.note('headers.complete')
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
                if record is not None:
                    record.note('body.started')
                async with aclosing(self._decoded_chunks(response,budget)) as chunks:
                    async for chunk in chunks:
                        if self.clock()>=deadline:raise TimeoutError('attempt_deadline')
                        if len(data)+len(chunk)>budget.response_limit:raise ReadLimitError('response_bytes')
                        if budget.bytes+len(chunk)>budget.byte_limit:raise ReadLimitError('branch_bytes')
                        budget.bytes+=len(chunk);data.extend(chunk)
                return json.loads(data)
        except BaseException as exc:
            if owner.cancel_requests and not isinstance(exc, asyncio.CancelledError):
                raise asyncio.CancelledError(owner.cancel_message) from exc
            raise
        finally:
            error = sys.exception()
            if record is not None:
                if owner.cleanup_result in ('completed', 'failed'):
                    record.note('cleanup.' + owner.cleanup_result)
                outcome = ('cancelled' if owner.cancel_requests or isinstance(error, asyncio.CancelledError)
                    else 'failed' if error is not None else 'complete')
                try:
                    self._finish_record(record, outcome)
                except Exception:
                    pass
            owner.record = None
            owner.request_marker = None
            owner.cleanup = False
            if owner.cancel_requests and error is None:
                # All response/library cleanup has exited; no subsequent page
                # may turn a cancelled read into complete financial facts.
                raise asyncio.CancelledError(owner.cancel_message)

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

    async def _reward_share(self, request, deadline):
        from decimal import Decimal
        markets = {}
        checked_at = self.utc_now()
        for sponsored in (False, True):
            source = 'sponsored' if sponsored else 'native'
            cursor = None
            seen = set()
            while True:
                payload = await self._json('clob', '/rewards/user/markets', dict(
                    signature_type=self.signature_type, maker_address=request.account,
                    sponsored=sponsored, only_open_orders=True, only_open_positions=False,
                    page_size=500, next_cursor=cursor), request, deadline)
                rows = payload['data']
                if not isinstance(rows, list):raise ValueError('reward_share_rows')
                _budget.get().take_items(len(rows))
                for row in rows:
                    cid = row['condition_id']
                    raw = row['earning_percentage']
                    if not isinstance(cid, str) or not cid or isinstance(raw, bool):
                        raise ValueError('reward_share_identity')
                    percentage = Decimal(str(raw))
                    if not percentage.is_finite() or not 0 <= percentage <= 100:
                        raise ValueError('reward_share_percentage')
                    if row.get('checked_at') is not None:
                        checked = datetime.fromisoformat(row['checked_at'].replace('Z', '+00:00'))
                        if checked.utcoffset() is None or checked > self.utc_now():
                            raise ValueError('reward_share_time')
                        checked_at=min(checked_at,checked)
                    values = markets.setdefault(cid, {})
                    if source in values and values[source] != percentage:
                        raise ValueError('reward_share_conflict')
                    values[source] = percentage
                cursor = payload['next_cursor']
                if cursor == 'LTE=':break
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ValueError('reward_share_pagination')
                seen.add(cursor)
                await asyncio.sleep(0)
        return dict(account_id=request.account, markets=markets, checked_at=checked_at)

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
        current = asyncio.current_task()
        if isinstance(current, OwnedReadTask):
            return await self._owned_read(request, deadline)
        # A native external caller keeps its ordinary Task. Shield only the
        # ownership join, forwarding cancellation to the actual source owner.
        owner = OwnedReadTask(self._owned_read(request, deadline))
        cancellation = None
        while not owner.done():
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError as exc:
                cancellation = exc
                owner.cancel(exc.args[0] if exc.args else None)
            except Exception:
                break
        try:
            result = owner.result()
        except BaseException as exc:
            if cancellation is not None and not isinstance(exc, asyncio.CancelledError):
                raise cancellation from exc
            raise
        if cancellation is not None:
            raise cancellation
        return result

    async def _owned_read(self, request, deadline):
        owner = asyncio.current_task()
        owner.in_read = True
        token = _request_owner.set(owner)
        try:
            result = await self._read(request, deadline)
        except BaseException as exc:
            if owner.cancel_requests and not isinstance(exc, asyncio.CancelledError):
                raise asyncio.CancelledError(owner.cancel_message) from exc
            raise
        else:
            if owner.cancel_requests:
                raise asyncio.CancelledError(owner.cancel_message)
            return result
        finally:
            owner.in_read = False
            _request_owner.reset(token)

    async def _read(self, request, deadline):
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
            elif request.source=='reward_share':value=await self._reward_share(request,deadline)
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
            return SourceResult(identity,freeze(value),checked_at=value['checked_at'] if request.source=='reward_share' else self.utc_now())
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
