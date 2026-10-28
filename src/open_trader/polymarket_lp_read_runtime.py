"""Owned asynchronous LP reads. Business decisions remain with the live owner."""
from dataclasses import dataclass, replace
from collections.abc import Mapping
import time
import asyncio
import threading
import json
import gc
from collections import OrderedDict, deque
from datetime import UTC, datetime
from types import MappingProxyType, CoroutineType, GeneratorType, AsyncGeneratorType
from .polymarket_lp_market_reader import freeze, SourceResult

BUY_SCOPE_CAP = 64
REFERENCE_CACHE_ENTRIES = 256
REFERENCE_CACHE_BYTES = 8*1024*1024
FINANCIAL_WAIT_BYTES = 16*1024
FINANCIAL_WAIT_FRAMES = 16
FINANCIAL_WAIT_LINKS = 64
CLEANUP_FAULT_ITEMS = 16
_FINANCIAL_SOURCES = ('balance', 'open_orders', 'positions')


def _native_asend_type():
    # Establish the native type without trusting an arbitrary object's name.
    async def sentinel():
        yield None
    generator = sentinel()  # Inert, never entered; owns no external resources.
    driver = generator.__anext__()
    driver_type = type(driver)
    driver.close()  # Only our local sentinel, never a captured live driver.
    return driver_type


_NATIVE_ASEND_TYPE = _native_asend_type()


def _location_text(value, limit=64):
    # Code metadata / opaque observation identity only; never stringify objects.
    if type(value) is not str:
        return 'unknown'
    return ''.join(c if c.isascii() and (c.isalnum() or c in '_.:-<>') else '_'
                   for c in value[:limit])


def _await_locations(task):
    """Inspect native await links only, retaining the deepest safe locations.

    Unknown awaiters are terminal: no repr, arbitrary attributes, locals, task
    enumeration or client/pool introspection. Returned values own no frames.
    """
    frames = deque(maxlen=FINANCIAL_WAIT_FRAMES)
    current = task.get_coro()
    walked = 0
    while current is not None and walked < FINANCIAL_WAIT_LINKS:
        if type(current) is CoroutineType:
            code, frame, following = current.cr_code, current.cr_frame, current.cr_await
        elif type(current) is GeneratorType:
            code, frame, following = current.gi_code, current.gi_frame, current.gi_yieldfrom
        elif type(current) is AsyncGeneratorType:
            code, frame, following = current.ag_code, current.ag_frame, current.ag_await
        elif type(current) is _NATIVE_ASEND_TYPE:
            # CPython exposes no public next-link on this native driver. Only
            # this exact type may bridge to its single native async generator.
            generators = [item for item in gc.get_referents(current)
                          if type(item) is AsyncGeneratorType]
            walked += 1
            if len(generators) == 1:
                current = generators[0]
                continue
            return dict(frames=list(frames), walked=walked, end='unknown',
                        truncated=walked > FINANCIAL_WAIT_FRAMES)
        else:
            return dict(frames=list(frames), walked=walked, end='unknown',
                        truncated=walked > FINANCIAL_WAIT_FRAMES)
        walked += 1
        if frame is not None:
            basename = code.co_filename.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
            frames.append(dict(file=_location_text(basename), function=_location_text(code.co_name),
                               line=min(999999999, max(0, frame.f_lineno)),
                               text_truncated=len(basename)>64 or len(code.co_name)>64))
        current = following
    return dict(frames=list(frames), walked=walked,
                end='complete' if current is None else 'unknown',
                truncated=walked > FINANCIAL_WAIT_FRAMES or current is not None)


@dataclass(frozen=True)
class ReadDemand:
    account: str
    process_epoch: str
    generation: str
    order_id: str
    terms_version: int
    condition_id: str = ''
    token_id: str = ''
    kind: str = 'buy'
    sources: tuple = ('book','metadata','native_rewards','sponsored_rewards')
    observation_id: str = ''
    target_order_id: str = ''
    reward_pool_fresh: bool = False
    @property
    def key(self):
        return (self.account,self.process_epoch,self.generation,self.kind,self.order_id,self.terms_version)

@dataclass(frozen=True)
class MarketAttempt:
    demand: ReadDemand
    attempt: int
    started: float
    deadline: float
    completed: float
    sources: Mapping
    complete: bool
    reason: str | None = None
    cleanup_fault: bool = False
    ws_invalidation: bool = False

class _Loop:
    def __init__(self,name):
        self.loop=asyncio.new_event_loop()
        self.thread=threading.Thread(target=self.loop.run_forever,name=name,daemon=True)
        self.thread.start()
    def submit(self,coro):return asyncio.run_coroutine_threadsafe(coro,self.loop)
    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()

class _Scope:
    def __init__(self,demand,lane):
        self.demand,self.lane=demand,lane
        self.port=None;self.tasks=();self.completed_sources={};self.active=False;self.closing=False;self.closed=False
        self.retiring=False;self.attempt=0;self.next_start=0;self.result=None
        self.started=self.deadline=0;self.actual_end=None;self.expired=False;self.cancel_sent=False;self.fault=False;self.fault_pending=False
        self.financial_cleanup_delayed=False
        self.financial_wait_capture=None
        self.fault_detail=None
        self.last_sources=None
        self.delivered_ws=None

class LPReadRuntime:
    def __init__(self, source_factory, *, clock=time.monotonic, utc_now=lambda:datetime.now(UTC), buy_cap=BUY_SCOPE_CAP,
                 cache_entries=REFERENCE_CACHE_ENTRIES, cache_bytes=REFERENCE_CACHE_BYTES, orderbook_url=None):
        self.source_factory=source_factory;self.clock=clock;self.utc_now=utc_now
        self.buy_cap=buy_cap;self.cache_entries=cache_entries;self.cache_bytes=cache_bytes;self._cache=OrderedDict();self._cache_size=0;self._lock=threading.RLock();self._scopes={};self._demands={}
        self._fixed_ports={};self._fixed_closing=set();self._fixed_closed=set();self._fixed_faults=set()
        self._fixed_kinds={};self._fixed_fault_details={}
        self._ages={};self._age_sequence=0
        # Runtime-lifetime identity, independent of scope retirement. One
        # scalar avoids retaining an unbounded ledger of departed order keys.
        self._attempt_sequence=0
        self._financial_cleanup_faults=0;self._last_financial_cleanup_fault=None
        self._last_financial_timeout=None
        self._financial_request_cleanup=None
        self._close_lock=threading.Lock()
        self._last_notice=None
        self._observed=-1;self._closed=False;self._started=False;self._signal=threading.Event();self._wakeup=lambda:None
        from .polymarket_lp_orderbook import OrderBooks
        self._books=OrderBooks(clock=clock,utc_now=utc_now);self._orderbook_url=orderbook_url
        self._orderbook_mode='off';self._ws=None;self._ws_future=None;self._ws_close=None;self._ws_eligible=set()
        self._audit_at={}
        self._ws_pending=set()
        self._ws_wake_requests=0
        self._shadow=self._books.diagnostics
        self._ws_woken_at={}
        self._ws_delivered={};self._ws_cutoffs={};self._ws_available={}
        self._shadow_contexts={};self._shadow_books=OrderedDict();self._shadow_tick_scheduled=False
        self._shadow_seen=OrderedDict()
        self._shadow_states={}
        self._shadow_logged={}
        self._configured_book_tokens={};self._configured_buy_prices={}

    def configure_orderbook(self, mode, tokens, *, eligible_keys=(), buy_prices=None, comparison_contexts=(), accepted_cutoffs=None):
        if mode not in ('off','shadow','use'):raise ValueError('orderbook_mode_invalid')
        with self._lock:
            if mode!='shadow':self._drop_shadow_work('mode_changed')
            self._orderbook_mode=mode;self._ws_eligible=set(eligible_keys)
            cutoffs={k:v for k,v in (accepted_cutoffs or {}).items() if k in self._ws_eligible}
            changed={k for k in self._ws_eligible if self._ws_cutoffs.get(k)!=cutoffs.get(k)}
            self._ws_cutoffs=cutoffs
            self._ws_available={k:v for k,v in self._ws_available.items() if k in self._ws_eligible and k not in changed}
            for key in changed:self._ws_delivered.pop(key,None)
            self._shadow.set_mode(mode)
            self._configured_book_tokens=dict(tokens) if mode!='off' else {}
            self._configured_buy_prices=dict(buy_prices or {})
            self._shadow_contexts={}
            for context in comparison_contexts:
                if (mode!='shadow' or context['demand_key'] not in self._ws_eligible
                        or tokens.get(context['token_id'])!=context['condition_id']):continue
                if (type(context['bid_level']) is not int or not 1<=context['bid_level']<=64
                        or len(self._shadow_contexts)>=64 and context['key'] not in self._shadow_contexts):
                    self._shadow.abandon(book_pending=False,cancel_pending=False,reason='context_capacity');continue
                self._shadow_contexts[context['key']]=freeze(context)
            for key,state in self._shadow_states.items():
                if key not in self._shadow_contexts and state['retire_at'] is None:
                    state['retire_at']=self.clock()+2
                    state['retire_continuity']=self._books.continuity_ticket(state['context']['token_id'])
            for key,context in self._shadow_contexts.items():
                state=self._shadow_states.setdefault(key,dict(context=context,triggered_at=None,
                    signature=None,ws=dict(state='unknown'),retire_at=None,receipt_cursor=self._books.receipt_cursor(),
                    qualification_identity=None,qualified_at=None))
                state['retire_at']=None
            retired=[key for key,state in self._shadow_states.items() if state['retire_at'] is not None]
            for key in retired[:-64]:self._discard_shadow_context(key,'context_capacity')
            self._refresh_shadow_subscriptions()
            self._ws_pending.intersection_update(tokens if mode!='off' else ())
            self._ws_delivered={k:v for k,v in self._ws_delivered.items() if k in self._ws_eligible}
            if mode=='off':
                for scope in self._scopes.values():scope.last_sources=None
        self.wake()

    def _refresh_shadow_subscriptions(self):
        tokens=dict(self._configured_book_tokens);prices=dict(self._configured_buy_prices)
        if self._orderbook_mode=='shadow':
            for state in self._shadow_states.values():
                context=state['context'];token=context['token_id']
                if token not in tokens:
                    tokens[token]=context['condition_id'];prices[token]=(context['price'],)
        self._books.subscribe(tokens,buy_prices=prices)

    def _drop_shadow_work(self, reason):
        for sample in self._shadow_books.values():
            self._shadow.abandon(book_pending=not sample['book_done'],cancel_pending=not sample['cancel_done'],reason=reason)
        self._shadow_books.clear();self._shadow_contexts.clear();self._shadow_states.clear()
        self._shadow_seen.clear();self._shadow_logged.clear()

    def _discard_shadow_context(self, key, reason):
        samples=tuple(k for k,s in self._shadow_books.items() if s['context']['key']==key)
        for sample_key in samples:
            sample=self._shadow_books.pop(sample_key)
            self._shadow.abandon(book_pending=not sample['book_done'],cancel_pending=not sample['cancel_done'],reason=reason)
        if not samples:self._shadow.abandon(book_pending=False,cancel_pending=False,reason=reason)
        self._shadow_states.pop(key,None);self._shadow_seen.pop(key,None);self._shadow_logged.pop(key,None)

    def _usable_ws_book(self, demand):
        from .polymarket_lp_orderbook import book_after_buy_acceptance
        book=self._books.market_book(demand.token_id)
        if book_after_buy_acceptance(book,condition_id=demand.condition_id,token_id=demand.token_id,
                accepted_at=self._ws_cutoffs.get(demand.key)) is not True:return None
        return book

    def orderbook_snapshot(self, token):
        with self._lock:return freeze(self._books.market_book(token))

    def complete_orderbook_check(self):
        with self._lock:
            changed={t for t in self._ws_pending if any(s.demand.token_id==t and s.demand.key in self._ws_eligible
                and self._ws_delivered.get(s.demand.key)!=self._books.seed_ticket(t)
                for s in self._scopes.values())}
            self._ws_pending.clear();self._ws_woken_at.clear()
        if changed:self._ws_changed(changed)

    def drain_orderbook_results(self):
        """Complete local reads before protection, through the original identity path."""
        results=[]
        with self._lock:
            if self._orderbook_mode!='use':return ()
            for scope in self._scopes.values():
                token=scope.demand.token_id
                book=self._usable_ws_book(scope.demand)
                if book is None and scope.delivered_ws is not None:
                    results.append(replace(scope.delivered_ws,complete=False,reason='ws_invalidated',ws_invalidation=True))
                    scope.delivered_ws=None
                if ((token not in self._ws_pending and book is not None) or scope.demand.kind!='buy'
                        or scope.demand.key not in self._ws_eligible or scope.active
                        or scope.retiring or scope.fault or scope.last_sources is None):continue
                if scope.result is not None and not _ws_source(scope.result.sources.get('book')):continue
                if book is None:continue
                ticket=self._books.seed_ticket(token)
                if self._ws_delivered.get(scope.demand.key)==ticket:continue
                self._attempt_sequence+=1;now=self.clock();sources=dict(scope.last_sources)
                sources['book']=SourceResult(str(scope.demand.key)+':ws:'+str(self._attempt_sequence),
                    freeze(book) if book else None,complete=book is not None,checked_at=book['received_at'] if book else None,
                    error=None if book else 'ws_invalidated')
                result=MarketAttempt(scope.demand,self._attempt_sequence,now,now+5,now,
                    freeze_sources(sources),True)
                results.append(result);scope.delivered_ws=result
                self._ws_delivered[scope.demand.key]=ticket
                scope.result=None
        return tuple(results)

    def protection_started(self):
        with self._lock:
            for stamp in self._ws_woken_at.values():self._shadow.decision_latency(self.clock()-stamp)

    def compare_shadow(self, token, rest, *, price, own_remaining, bid_level, baseline_levels, condition_id,
                       accepted_at=None, source_id=None, context_key=None):
        from .polymarket_lp_risk import evaluate_lp_bid_levels
        from .polymarket_lp_orderbook import book_after_buy_acceptance
        with self._lock:
            if self._orderbook_mode!='shadow':return None
            if source_id is not None:
                previous=self._shadow_seen.get(context_key)
                if previous is None or previous[0]!=source_id:return None
                row=dict(previous[1])
                if self._shadow_logged.get(context_key)==source_id:return freeze(row)
                self._shadow_logged[context_key]=source_id
                row=dict(row,observation_reason=row['reason'],reason='owner_projection')
            else:
                # An explicit value-only query has no observation/counter power.
                book=self._books.market_book(token)
                if book_after_buy_acceptance(book,condition_id=condition_id,token_id=token,accepted_at=accepted_at) is not True:
                    book=None
                ws=evaluate_lp_bid_levels(book,price=price,own_remaining=own_remaining,
                    bid_level=bid_level,baseline_levels=baseline_levels,condition_id=condition_id,token_id=token)
                row=self._shadow.comparison(rest,ws)
        import logging
        from .polymarket_trading import _lp_capture_read_log
        _lp_capture_read_log(lambda:logging.getLogger(__name__).info(
            'prediction_lp_orderbook_shadow %s',json.dumps(row,sort_keys=True)))
        return freeze(row)

    def _ws_changed(self, tokens):
        notify=False
        with self._lock:
            if self._orderbook_mode=='shadow':self._advance_shadow_books();return
            if self._orderbook_mode!='use':return
            tokens=set(tokens)
            for scope in self._scopes.values():
                if scope.demand.key not in self._ws_eligible or scope.demand.kind!='buy':continue
                available=self._usable_ws_book(scope.demand) is not None
                if available and not self._ws_available.get(scope.demand.key,False):
                    tokens.add(scope.demand.token_id);self._ws_delivered.pop(scope.demand.key,None)
                self._ws_available[scope.demand.key]=available
            for token in tokens:
                first=token not in self._ws_pending;self._ws_pending.add(token)
                if first:self._ws_woken_at[token]=self.clock()
                for scope in self._scopes.values():
                    if (scope.demand.token_id!=token or scope.demand.kind!='buy'
                            or scope.demand.key not in self._ws_eligible or scope.retiring):continue
                    book=self._usable_ws_book(scope.demand)
                    if book is None:
                        notify=True
                        continue
                    if scope.result is not None:
                        if not _ws_source(scope.result.sources.get('book')):continue
                        sources=dict(scope.result.sources)
                        sources['book']=SourceResult(str(scope.demand.key)+':ws:'+str(scope.result.attempt),book,
                            complete=book is not None,checked_at=book['received_at'] if book else None,error=None if book else 'ws_invalidated')
                        scope.result=replace(scope.result,sources=freeze_sources(sources),complete=scope.result.complete and book is not None)
                    elif first and not scope.active and not scope.fault and scope.last_sources is not None:
                        self._attempt_sequence+=1
                        sources=dict(scope.last_sources)
                        sources['book']=SourceResult(str(scope.demand.key)+':ws:'+str(self._attempt_sequence),book,
                            complete=book is not None,checked_at=book['received_at'] if book else None,error=None if book else 'ws_invalidated')
                        now=self.clock()
                        scope.result=MarketAttempt(scope.demand,self._attempt_sequence,now,now+5,now,freeze_sources(sources),book is not None)
                        notify=True
        if notify:
            self._ws_wake_requests+=1;self._notify()

    @staticmethod
    def _public_bids(book, n):
        bids=sorted(book['bids'],key=lambda row:row['price'],reverse=True)
        return tuple((row['price'],row['size']) for row in bids[:n])

    def _observe_shadow_book(self, scope, result):
        """Runs at actual REST completion on market I/O, before owner collection."""
        if self._orderbook_mode!='shadow' or scope.demand.kind!='buy':return
        self._advance_shadow_books()
        contexts=tuple(context for context in self._shadow_contexts.values()
            if scope.demand.key in context.get('demand_keys',(context['demand_key'],)))
        self._shadow.eligible_read(has_context=bool(contexts))
        if not contexts:
            import logging
            from .polymarket_trading import _lp_capture_read_log
            row=dict(reason='missing_comparison_context',token_prefix=scope.demand.token_id[:8],
                rest_exchange_at=result.value['timestamp'].isoformat(),
                rest_read_at=result.checked_at.isoformat() if result.checked_at else None)
            _lp_capture_read_log(lambda:logging.getLogger(__name__).info(
                'prediction_lp_orderbook_shadow %s',json.dumps(row,sort_keys=True)))
        for context in contexts:
            key=(context['key'],result.source_id)
            if key in self._shadow_books:continue
            rest=self._shadow_evaluate(result.value,context)
            state=self._shadow_states[context['key']]
            row=self._shadow.sample(rest,state['ws'])
            state['rest_state']=rest['state']
            if rest['state']=='triggered':state['early_unmatched']=False
            row.update(observed_at=self.utc_now().isoformat())
            self._shadow_seen[context['key']]=(result.source_id,row)
            cancel=rest['state']=='triggered'
            if cancel:self._shadow.cancel_started(observation_id=result.source_id,obligation=True)
            self._shadow_books[key]=dict(context=context,rest=result.value,completed=self.clock(),
                deadline=self.clock()+2,generation=self._books.generation,book_done=False,
                cancel_done=not cancel,row=row,rest_decision=rest,
                continuity=self._books.continuity_ticket(context['token_id']),rest_read_at=result.checked_at)
            self._shadow.book_started()
            while len(self._shadow_books)>256:
                _,dropped=self._shadow_books.popitem(last=False)
                self._shadow.abandon(book_pending=not dropped['book_done'],cancel_pending=not dropped['cancel_done'],reason='pending_capacity')
        self._advance_shadow_books()

    @staticmethod
    def _shadow_evaluate(book, context):
        from .polymarket_lp_risk import evaluate_lp_bid_levels
        from .polymarket_lp_orderbook import book_after_buy_acceptance
        if book_after_buy_acceptance(book,condition_id=context['condition_id'],token_id=context['token_id'],
                accepted_at=context['accepted_at']) is not True:book=None
        return evaluate_lp_bid_levels(book,price=context['price'],own_remaining=context['own_remaining'],
            bid_level=context['bid_level'],baseline_levels=context['baseline_levels'],
            condition_id=context['condition_id'],token_id=context['token_id'])

    def _advance_shadow_books(self):
        with self._lock:
            self._shadow_tick_scheduled=False
            for key,state in self._shadow_states.items():
                context=state['context'];book=self._books.market_book(context['token_id'])
                signature=(book['book_version'],book['confirmation_at']) if book else None
                # Freshness can hide a book without changing its actual continuity.
                # Check real identity even across consecutive unavailable snapshots.
                identity=self._books.continuity_ticket(context['token_id'])
                if state['qualification_identity']!=identity:
                    state['qualification_identity']=identity;state['qualified_at']=None
                if state['signature']==signature:continue
                if book is not None:
                    cutoff=context['accepted_at']
                    for stamp,at in ((book.get('confirmation_at'),book.get('confirmation_monotonic')),
                            (book.get('message_confirmed_at'),book.get('message_confirmed_monotonic'))):
                        if stamp is not None and at is not None and (cutoff is None or stamp>cutoff):
                            state['qualified_at']=at if state['qualified_at'] is None else min(state['qualified_at'],at)
                previous=state['ws']['state']
                state['signature']=signature;state['ws']=self._shadow_evaluate(book,context)
                if state['ws']['state']=='triggered' and previous!='triggered' and state.get('rest_state')!='triggered':
                    self._shadow.ws_excursion();state['early_unmatched']=True
                elif state['ws']['state']=='monitoring' and previous=='triggered' and state.get('early_unmatched'):
                    self._shadow.ws_excursion(transient=True);state['early_unmatched']=False
                if state['ws']['state']=='triggered' and state['triggered_at'] is None and state['qualified_at'] is not None:
                    state['triggered_at']=max(book['book_received_monotonic'],state['qualified_at'])
                    state['triggered_book']=freeze(book)
            for key,sample in tuple(self._shadow_books.items()):
                context=sample['context'];token=context['token_id']
                settled=self._books.comparison_settled(token,until=sample['deadline'],
                    continuity=sample['continuity'],after=self._shadow_states[context['key']]['receipt_cursor'])
                if not sample['book_done']:
                    target=self._public_bids(sample['rest'],context['bid_level'])
                    window=self._books.comparison_versions(token,since=sample['completed']-2,
                        until=min(self.clock(),sample['deadline']),continuity=sample['continuity'])
                    matched_book=next((version['book'] for version in window['versions']
                        if self._public_bids(version['book'],context['bid_level'])==target),None)
                    matched=matched_book is not None
                    if matched or settled:
                        self._shadow.book_result(matched if matched or window['reason'] is None else None,
                            reason=window['reason']);sample['book_done']=True
                        self._publish_shadow_result(sample,'book_match' if matched else window['reason'] or 'book_mismatch',
                            matched_book or self._books.market_book(token),kind='book')
                if not sample['cancel_done']:
                    state=self._shadow_states.get(context['key'])
                    trigger=state['triggered_at'] if state else None
                    if trigger is not None and trigger<=sample['deadline']:
                        self._shadow.cancel_result(trigger-sample['completed']);sample['cancel_done']=True
                        sample['row'].update(self._shadow.comparison(sample['rest_decision'],dict(state='triggered')))
                        self._publish_shadow_result(sample,'cancel_match',state.get('triggered_book'),kind='cancel',
                            seconds=trigger-sample['completed'])
                    elif settled:
                        self._shadow.cancel_result(None);sample['cancel_done']=True
                        self._publish_shadow_result(sample,'ws_missed_cancel',self._books.market_book(token),kind='cancel')
                if sample['book_done'] and sample['cancel_done']:self._shadow_books.pop(key)
            retired=tuple(key for key,state in self._shadow_states.items()
                if state['retire_at'] is not None and self.clock()>=state['retire_at']
                and not self._books.pending_receipt(state['context']['token_id'],until=state['retire_at'],
                    continuity=state['retire_continuity'],after=state['receipt_cursor']))
            for key in retired:
                self._shadow_states.pop(key);self._shadow_seen.pop(key,None);self._shadow_logged.pop(key,None)
            if retired:self._refresh_shadow_subscriptions()
            if self._orderbook_mode=='shadow':
                states=tuple(self._shadow_states.values())
                unknown=next((s['ws'] for s in states if s['ws']['state']=='unknown'),None)
                self._shadow.availability(bool(states) and unknown is None,
                    reason=(unknown.get('reason_codes') or ['book_unknown'])[0] if unknown else
                        'missing_comparison_context' if self._books.tokens else 'no_subscriptions')

    def _publish_shadow_result(self, sample, reason, book, *, kind, seconds=None):
        """Whitelist public values before handing a detached record to Queue32."""
        import logging
        from .polymarket_trading import _lp_capture_read_log
        context=sample['context'];rest=sample['rest'];n=context['bid_level']
        def stamp(value):return value.isoformat() if isinstance(value,datetime) else None
        def bids(value):
            return tuple((str(price),str(size)) for price,size in self._public_bids(value,n)) if value else ()
        row=dict(sample['row'],token_prefix=context['token_id'][:8],comparison_kind=kind,reason=reason,
            outcome='match' if reason in ('book_match','cancel_match') else 'different' if reason in (
                'book_mismatch','ws_missed_cancel') else 'not_comparable',signed_seconds=seconds,
            rest_exchange_at=stamp(rest.get('timestamp')),rest_read_at=stamp(sample['rest_read_at']),
            rest_confirmation_at=None,ws_exchange_at=stamp(book.get('timestamp')) if book else None,
            ws_confirmation_at=stamp(book.get('confirmation_at')) if book else None,
            ws_read_at=stamp(book.get('book_received_at')) if book else None,
            ws_version=book.get('book_version') if book else None,
            rest_bids=bids(rest),ws_bids=bids(book),observed_at=self.utc_now().isoformat())
        sample['row'].update(row);self._shadow.publish(row)
        encoded=json.dumps(row,sort_keys=True)
        _lp_capture_read_log(lambda:logging.getLogger(__name__).info('prediction_lp_orderbook_shadow %s',encoded))

    def _maintain_orderbook(self):
        if (self._orderbook_mode=='shadow' or self._shadow_books or any(s['retire_at'] is not None for s in self._shadow_states.values())) and self._started and not self._shadow_tick_scheduled:
            self._shadow_tick_scheduled=True
            self._market.loop.call_soon_threadsafe(self._advance_shadow_books)
        valid={(self._books.generation,t) for t in self._books.tokens}
        self._audit_at={k:v for k,v in self._audit_at.items() if k in valid}
        for key in valid:
            if key not in self._audit_at and self._books.snapshot(key[1]) is not None:self._audit_at[key]=self._books.synced_since(key[1])+60
        if self._ws_future is not None and self._ws_future.done() and (self._ws_close is None or self._ws_close.done()):
            self._ws=None;self._ws_future=None;self._ws_close=None
        wanted=not self._closed and self._orderbook_mode!='off' and bool(self._books.tokens)
        if not wanted and self._ws is not None and self._ws_close is None:
            self._ws_close=self._market.submit(self._ws.aclose())
        if wanted and self._ws is None:
            from .polymarket_lp_orderbook import OrderBookWebSocket
            self._ws=OrderBookWebSocket(self._books,on_update=self._ws_changed,**({'url':self._orderbook_url} if self._orderbook_url else {}))
            self._ws_future=self._market.submit(self._ws.run())

    def start(self,wakeup=lambda:None):
        with self._lock:
            self._wakeup=wakeup
            if self._started:return
            self._market=_Loop('lp-market-io');self._control=_Loop('lp-account-exact-io')
            self._started=True
            self._supervisor=threading.Thread(target=self._supervise,name='lp-read-deadlines',daemon=True)
            self._supervisor.start()

    def reconcile(self,demands):
        with self._lock:
            incoming={} if self._closed else {d.key:d for d in demands}
            share_accounts=set()
            for key,demand in tuple(incoming.items()):
                if demand.kind=='reward_share':
                    account=demand.account.casefold()
                    if account in share_accounts:incoming.pop(key)
                    share_accounts.add(account)
            for key in incoming:
                if key not in self._ages:
                    self._age_sequence+=1;self._ages[key]=self._age_sequence
            self._ages={k:v for k,v in self._ages.items() if k in incoming or k in self._scopes}
            self._demands=incoming
        self.wake()

    def wake(self):self._signal.set()

    def _notify(self):
        try:self._wakeup()
        except Exception:pass

    def _supervise(self):
        while True:
            self._signal.wait(.01);self._signal.clear()
            with self._lock:
                self._observed=self.clock()
                self._maintain_orderbook()
                for key,scope in tuple(self._scopes.items()):
                    # The owning loop stages only copied values. Publish on
                    # the existing supervisor tick, under its existing lock;
                    # diagnostic work never adds a lock wait before cancel.
                    capture=scope.financial_wait_capture
                    if capture is not None:
                        original,snapshot=capture
                        scope.financial_wait_capture=None
                        if self._last_financial_timeout is original:
                            self._last_financial_timeout=snapshot
                    if scope.demand.kind=='financial' and scope.port is not None:
                        try:
                            observe=getattr(scope.port,'financial_cleanup_records',None)
                            records=observe(scope.attempt) if observe is not None else None
                            if records is not None and records['attempt']==scope.attempt:
                                self._financial_request_cleanup=records
                        except Exception:
                            pass
                    if self._closed:
                        scope.result=None;scope.fault_pending=False;scope.delivered_ws=None
                    if key not in self._demands:
                        scope.retiring=True
                    if scope.active and (scope.retiring or self.clock()>=scope.deadline):
                        if not scope.retiring and not scope.expired:
                            self._expire(scope)
                        if not scope.cancel_sent:
                            scope.cancel_sent=True
                            scope.lane.loop.call_soon_threadsafe(self._cancel_children,scope)
                    if scope.active and scope.cancel_sent and self.clock()>=scope.deadline+1 and not scope.fault:
                        scope.fault=True;scope.fault_pending=True
                        scope.fault_detail=self._fault_detail(_fixed_lane(scope.demand.kind) or 'buy',
                            scope.demand.kind,'delayed_read_cleanup')
                        if scope.result is not None:
                            scope.result=replace(scope.result,cleanup_fault=True)
                        if scope.demand.kind=='financial':
                            scope.financial_cleanup_delayed=True
                            self._financial_cleanup_faults+=1
                            self._last_financial_cleanup_fault=dict(attempt=scope.attempt,
                                sources=tuple(name for name,task in zip(scope.demand.sources,scope.tasks)
                                    if name in ('balance','open_orders','positions') and not task.done())[:3],
                                fault_monotonic=self.clock(),ended_monotonic=None)
                    if scope.retiring and not scope.active and not scope.closing and not scope.closed:
                        scope.closing=True;scope.lane.submit(self._close_scope(scope))
                    if scope.closed and scope.result is None and not scope.fault_pending:
                        self._scopes.pop(key,None)
                for key,demand in sorted(self._demands.items(),key=lambda item:self._ages[item[0]]):
                    if key not in self._scopes:
                        if demand.kind in ('buy','income') and (sum(s.demand.kind in ('buy','income') for s in self._scopes.values())>=self.buy_cap
                                or any(s.demand.kind in ('buy','income') and (s.demand.account,s.demand.order_id)==(demand.account,demand.order_id)
                                    for s in self._scopes.values())):continue
                        lane=self._control if demand.kind in ('financial','exact') else self._market
                        self._scopes[key]=_Scope(demand,lane)
                for scope in sorted(self._scopes.values(),key=lambda s:(s.next_start,
                        0 if s.demand.kind=='position' else 1,self._ages.get(s.demand.key,0))):
                    updated=self._demands.get(scope.demand.key)
                    if (updated is not None and not scope.active and scope.result is None
                            and not (_fixed_lane(scope.demand.kind) in ('financial','low') and scope.fault_pending)):
                        scope.demand=updated
                    if (not scope.retiring and not scope.fault and not scope.active and scope.result is None
                            and not (_fixed_lane(scope.demand.kind) in ('financial','low') and scope.fault_pending)
                            and self.clock()>=scope.next_start):
                        kind=scope.demand.kind
                        lane=_fixed_lane(kind)
                        if lane is not None:
                            peers=[s for s in self._scopes.values() if _fixed_lane(s.demand.kind)==lane and s is not scope]
                            if lane in self._fixed_faults or any(s.fault for s in peers):continue
                            if lane=='exact':
                                fills=scope.demand.sources==('fills',)
                                if any(s.active and (s.demand.sources==('fills',))==fills for s in peers):continue
                            elif any(s.active for s in peers):continue
                        wait_owner=scope.port if scope.port is not None else self.source_factory
                        if callable(getattr(wait_owner,'not_before',None)):
                            providers={'gamma' if n=='metadata' else 'data' if n=='positions' else 'clob' for n in scope.demand.sources}
                            not_before=max((wait_owner.not_before(p,scope.demand.account) for p in providers),default=0)
                            if self.clock()<not_before:
                                scope.next_start=max(scope.next_start,not_before);continue
                        scope.completed_sources={}
                        self._attempt_sequence+=1;scope.attempt=self._attempt_sequence
                        scope.started=self.clock();scope.deadline=scope.started+(10 if kind=='reward_share' else 5)
                        scope.active=True;scope.actual_end=None;scope.expired=False;scope.cancel_sent=False
                        scope.lane.submit(self._attempt(scope))
                needed={(d.account,d.process_epoch,d.generation,d.condition_id,d.token_id,n)
                    for d in self._demands.values() for n in ('metadata','native_rewards','sponsored_rewards')}
                for key in tuple(self._cache):
                    if key not in needed:self._cache_size-=self._cache.pop(key)[1]
                if self._closed and not self._scopes:
                    for name,(port,lane) in self._fixed_ports.items():
                        if name not in self._fixed_closing:
                            self._fixed_closing.add(name);lane.submit(self._close_fixed(name,port))
                done=self._closed and not self._scopes and len(self._fixed_closed)==len(self._fixed_ports) and self._ws_future is None
                # A completion/deadline/coverage change wakes the business
                # owner. Polling the clock alone must not create a 100 Hz loop.
                notice=tuple((key,s.attempt,s.active,s.fault,s.retiring,s.closed,
                    s.result is not None,s.fault_pending) for key,s in self._scopes.items())
                changed=notice!=self._last_notice
                self._last_notice=notice
            if changed:self._notify()
            if done:return

    def _cancel_children(self,scope):
        # Observation failure must never suppress or postpone original cleanup.
        try:
            if scope.demand.kind=='financial' and scope.expired:
                self._capture_financial_timeout(scope)
        finally:
            for task in scope.tasks:
                if not task.done():task.cancel()

    def _capture_financial_timeout(self,scope):
        original=self._last_financial_timeout
        if (original is None or original['attempt']!=scope.attempt
                or original['capture_state']!='pending' or scope.financial_wait_capture is not None):
            return
        captured=None
        try:
            captured=self.clock()
            sources=[]
            for name,task in zip(scope.demand.sources[:3],scope.tasks[:3]):
                if name not in _FINANCIAL_SOURCES:continue
                done=task.done()
                sources.append(dict(source=name,state='done' if done else 'pending',
                    **(dict(frames=[],walked=0,end='complete',truncated=False) if done
                       else _await_locations(task))))
            snapshot=dict(original,capture_state='captured',captured_monotonic=captured,
                late=captured>original['deadline_monotonic'],sources=sources)
            # A second hard bound covers future schema additions as well as the
            # fixed 3 x 16 x bounded ASCII location schema above.
            if len(json.dumps(snapshot,allow_nan=False).encode('utf-8'))>FINANCIAL_WAIT_BYTES:
                raise ValueError('financial_wait_budget')
        except Exception:
            snapshot=dict(original,capture_state='unavailable',captured_monotonic=captured,
                late=None if captured is None else captured>original['deadline_monotonic'])
        # No lock, event, await or I/O on the cancellation path. The supervisor
        # checks immutable record identity before accepting this staged value;
        # an older callback cannot replace a more recent attempt's snapshot.
        scope.financial_wait_capture=(original,snapshot)

    def _expire(self,scope):
        scope.expired=True
        scope.last_sources=None
        if scope.demand.kind=='financial':
            self._last_financial_timeout=dict(attempt=scope.attempt,
                observation_id=_location_text(scope.demand.observation_id,96),
                observation_id_truncated=type(scope.demand.observation_id) is str and len(scope.demand.observation_id)>96,
                started_monotonic=scope.started,deadline_monotonic=scope.deadline,
                timeout_monotonic=self.clock(),captured_monotonic=None,late=None,
                capture_state='pending',sources=[dict(source=name,
                    state='done' if name in scope.completed_sources else 'pending',frames=[],
                    walked=0,end='unavailable',truncated=False)
                    for name in scope.demand.sources[:3] if name in _FINANCIAL_SOURCES])
        scope.result=MarketAttempt(scope.demand,scope.attempt,scope.started,scope.deadline,self.clock(),
            freeze_sources({name:scope.completed_sources.get(name,SourceResult('',complete=False,error='timeout')) for name in scope.demand.sources}),
            False,'timeout')

    async def _read_source(self,scope,source,request):
        key=(scope.demand.account,scope.demand.process_epoch,scope.demand.generation,
            scope.demand.condition_id,scope.demand.token_id,source)
        reference=source in ('metadata','native_rewards','sponsored_rewards')
        seed_ticket=None;audit=False;seed_unsynced=False
        with self._lock:
            eligible=source=='book' and self._orderbook_mode!='off' and scope.demand.key in self._ws_eligible
            if eligible:
                seed_ticket=self._books.seed_ticket(scope.demand.token_id)
                book=self._books.market_book(scope.demand.token_id)
                seed_unsynced=book is None
                audit_key=(self._books.generation,scope.demand.token_id)
                if book is not None and self.clock()>=self._audit_at.get(audit_key,self.clock()+60):
                    audit=True;self._audit_at[audit_key]=self.clock()+60
                    self._books.audit_started(scope.demand.token_id)
                if self._orderbook_mode=='use' and scope.demand.kind=='buy':
                    book=self._usable_ws_book(scope.demand)
                    self._ws_available[scope.demand.key]=book is not None
                if book is not None and self._orderbook_mode=='use' and scope.demand.kind=='buy' and not audit:
                    return self._completed(scope,source,SourceResult(request.request_id,book,checked_at=book['received_at']))
            cached=self._cache.get(key) if reference else None
            if cached is not None and cached[0].checked_at is not None:
                age=(self.utc_now()-cached[0].checked_at).total_seconds()
                if 0<=age<(5 if source in ('native_rewards','sponsored_rewards') and scope.demand.reward_pool_fresh else 30):
                    self._cache.move_to_end(key);return self._completed(scope,source,cached[0])
        result=await scope.port.read(request,scope.deadline)
        if eligible:
            from .polymarket_lp_orderbook import rest_event
            with self._lock:
                if (scope.demand.key in self._ws_eligible and self._orderbook_mode!='off'
                        and seed_ticket[:4]==self._books.seed_ticket(scope.demand.token_id)[:4]):
                    if result.complete and not result.error and isinstance(result.value,Mapping):
                        if not scope.retiring and not scope.expired and self.clock()<scope.deadline:
                            self._observe_shadow_book(scope,result)
                        if audit:self._books.reconcile(scope.demand.token_id,rest_event(result.value))
                        elif seed_unsynced:self._books.seed(scope.demand.token_id,rest_event(result.value),seed_ticket)
                        if audit and self._orderbook_mode=='use' and scope.demand.kind=='buy':
                            latest=self._usable_ws_book(scope.demand)
                            if latest is not None:result=replace(result,value=freeze(latest),checked_at=latest['received_at'])
        if reference and result.complete and not result.error and result.checked_at is not None:
            with self._lock:
                if not scope.retiring and not scope.expired and self.clock()<scope.deadline:
                    encoded=len(json.dumps(_json_value(result.value),separators=(',',':'),default=str).encode())
                    if encoded<=self.cache_bytes:
                        previous=self._cache.get(key)
                        if previous is None or previous[0].checked_at<=result.checked_at:
                            if previous is not None:self._cache_size-=previous[1]
                            self._cache[key]=(result,encoded);self._cache_size+=encoded
                            self._cache.move_to_end(key)
                            while len(self._cache)>self.cache_entries or self._cache_size>self.cache_bytes:
                                _,(_,size)=self._cache.popitem(last=False);self._cache_size-=size
        return self._completed(scope,source,result)

    def _completed(self,scope,source,result):
        with self._lock:
            if not scope.retiring and not scope.expired and self.clock()<scope.deadline:
                scope.completed_sources[source]=result
        return result

    async def _attempt(self,scope):
        from .polymarket_lp_async_source import SourceRequest, OwnedReadTask
        sources={}
        try:
            if scope.port is None:
                fixed=_fixed_lane(scope.demand.kind)
                if fixed is not None:
                    with self._lock:
                        owned=self._fixed_ports.get(fixed)
                        if owned is None:
                            scope.port=self.source_factory(scope.demand)
                            self._fixed_ports[fixed]=(scope.port,scope.lane)
                        else:scope.port=owned[0]
                        self._fixed_kinds[fixed]=scope.demand.kind
                else:scope.port=self.source_factory(scope.demand)
            d=scope.demand
            scope.tasks=tuple(OwnedReadTask(self._read_source(scope,source,SourceRequest(source,d.account,
                d.condition_id,d.token_id,d.target_order_id or d.order_id,':'.join(map(str,(*d.key,scope.attempt,source))),
                diagnostic_attempt=scope.attempt if d.kind=='financial' else 0)))
                for source in d.sources)
            if scope.cancel_sent:self._cancel_children(scope)
            values=await asyncio.gather(*scope.tasks,return_exceptions=True)
            sources={name:value if isinstance(value,SourceResult) else SourceResult('',complete=False,error='read_failed')
                for name,value in zip(d.sources,values)}
            with self._lock:
                if not scope.retiring and not scope.expired and self.clock()>=scope.deadline:
                    self._expire(scope)
                from .polymarket_lp_async_source import closed_position_value
                closed=(d.kind=='position' and closed_position_value(d.condition_id,d.token_id,sources) is not None)
                complete=closed or all(s.complete and not s.error for s in sources.values())
                if complete and not closed and set(('book','metadata','native_rewards','sponsored_rewards'))<=sources.keys():
                    from .polymarket_lp_async_source import market_value
                    try:complete=market_value(d.condition_id,sources)['rewards']['complete'] is True
                    except (ValueError,TypeError,KeyError):complete=False
                if not scope.retiring and not scope.expired:
                    scope.result=MarketAttempt(d,scope.attempt,scope.started,scope.deadline,self.clock(),
                        freeze_sources(sources),complete)
                # Publish a normal result and actual exit in one critical
                # section. The owner can advance its clock immediately on ACK.
                self._ended(scope,sources)
        except Exception as exc:
            with self._lock:
                if not scope.retiring and not scope.expired:
                    scope.result=MarketAttempt(scope.demand,scope.attempt,scope.started,scope.deadline,self.clock(),
                        freeze_sources({n:SourceResult('',complete=False,error=type(exc).__name__) for n in scope.demand.sources}),False,'source_unavailable')
        finally:
            with self._lock:
                if scope.active:self._ended(scope,sources)
            self.wake();self._notify()

    def _ended(self,scope,sources):
        scope.active=False;scope.actual_end=self.clock()
        # A book-only completion cannot heal a failed required refresh, an
        # expired attempt, or an incomplete reward catalog using older facts.
        scope.last_sources=None
        if (self._orderbook_mode!='off' and scope.demand.key in self._ws_eligible and not scope.expired
                and not scope.retiring and scope.result is not None and scope.result.complete
                and sources and all(s.complete and not s.error for s in sources.values())):
            scope.last_sources=freeze_sources(sources)
        # gather has collected the actual children, including response.aclose.
        # Financial and low delay faults block only while that ownership is
        # live. Their original result mailbox survives until owner collection.
        # BUY, exact and port-close faults retain their existing semantics.
        if _fixed_lane(scope.demand.kind) in ('low','reward_share'):
            scope.fault=False;scope.fault_detail=None
        if scope.financial_cleanup_delayed:
            scope.fault=False;scope.financial_cleanup_delayed=False;scope.fault_detail=None
            if self._last_financial_cleanup_fault['attempt']==scope.attempt:
                self._last_financial_cleanup_fault=dict(self._last_financial_cleanup_fault,
                    ended_monotonic=scope.actual_end)
        scope.next_start=max(scope.started+({'sell':30,'position':60,'reward_share':30}.get(scope.demand.kind,5)),
            scope.actual_end,*(s.retry_at or 0 for s in sources.values()))

    async def _close_scope(self,scope):
        try:
            if scope.port is not None and scope.demand.kind in ('buy','income'):await scope.port.aclose()
        except BaseException:
            with self._lock:
                scope.fault=True;scope.fault_pending=True
                scope.fault_detail=self._fault_detail('buy',scope.demand.kind,'port_close_failure')
        else:
            with self._lock:scope.closed=True
        finally:self.wake()

    async def _close_fixed(self,name,port):
        try:await port.aclose()
        except BaseException:
            with self._lock:
                self._fixed_faults.add(name)
                self._fixed_fault_details[name]=self._fault_detail(name,
                    self._fixed_kinds.get(name),'port_close_failure')
        else:
            with self._lock:self._fixed_closed.add(name)
        finally:self.wake()

    def _fault_detail(self,lane,kind,reason):
        # Only enum values and a captured UTC timestamp leave this boundary.
        # Diagnostic failure must not alter cleanup ownership or its gate.
        since=None
        try:
            stamp=self.utc_now()
            if isinstance(stamp,datetime) and stamp.utcoffset() is not None:
                since=stamp.astimezone(UTC).isoformat()
        except Exception:pass
        return dict(lane=lane,kind=kind if kind in
            ('buy','financial','exact','sell','position','holding','income','reward_share') else 'unknown',
            since=since,active=True,blocks_new_buy=lane not in ('buy','reward_share'),reason=reason)

    def drain_results(self):
        with self._lock:
            result=[]
            for scope in self._scopes.values():
                if scope.result is not None:
                    source=scope.result.sources.get('book')
                    if source is not None and isinstance(source.value,Mapping) and source.value.get('transport')=='ws':
                        book=self._usable_ws_book(scope.demand)
                        if (book is None or book['connection_generation']!=source.value.get('connection_generation')
                                or scope.demand.key not in self._ws_eligible or self._orderbook_mode!='use'):
                            # Loss of a WS fact is confidence loss, not another HTTP attempt.
                            if scope.result.complete:
                                scope.result=replace(scope.result,complete=False,reason='ws_invalidated',ws_invalidation=True)
                        elif self.clock()<scope.result.deadline:
                            sources=dict(scope.result.sources)
                            sources['book']=replace(source,value=freeze(book),checked_at=book['received_at'])
                            scope.result=replace(scope.result,sources=freeze_sources(sources))
                    result.append(replace(scope.result,cleanup_fault=scope.result.cleanup_fault or scope.fault))
                    if source is not None and isinstance(source.value,Mapping) and source.value.get('transport')=='ws':
                        self._ws_delivered[scope.demand.key]=self._books.seed_ticket(scope.demand.token_id)
                    delivered=result[-1]
                    scope.delivered_ws=delivered if delivered.complete and _ws_source(delivered.sources.get('book')) else None
                    scope.result=None;scope.fault_pending=False
                elif scope.fault_pending:
                    result.append(MarketAttempt(scope.demand,scope.attempt,scope.started,scope.deadline,
                        self.clock(),freeze_sources({}),False,'cleanup_fault',True))
                    scope.fault_pending=False
            result=tuple(result)
        self.wake();return result

    def status(self):
        with self._lock:
            now=self.clock();dependencies=[]
            for scope in self._scopes.values():
                if scope.result is not None or scope.fault_pending:
                    dependencies.append(now)
                elif scope.active and not scope.fault:
                    dependencies.append(scope.deadline+(1 if scope.expired or scope.retiring else 0))
                elif not (scope.active or scope.retiring or scope.fault or scope.closed):
                    lane=_fixed_lane(scope.demand.kind)
                    peers=[s for s in self._scopes.values() if s is not scope and _fixed_lane(s.demand.kind)==lane]
                    if lane is not None and (lane in self._fixed_faults or any(s.fault for s in peers)
                            or any(s.active and (lane!='exact' or (s.demand.sources==('fills',))==
                                (scope.demand.sources==('fills',))) for s in peers)):
                        continue  # actual peer completion/deadline owns admission
                    dependencies.append(scope.next_start)
            # Newly requested fixed scopes need supervisor admission first.
            if any(key not in self._scopes and d.kind not in ('buy','income') for key,d in self._demands.items()):
                dependencies.append(now)
            deferred=[d for key,d in self._demands.items() if d.kind in ('buy','income') and key not in self._scopes]
            owned=[s for s in self._scopes.values() if s.demand.kind in ('buy','income')]
            ports=[s.port for s in owned if s.port is not None and not s.closed]
            ports.extend(p for key,(p,_) in self._fixed_ports.items() if key not in self._fixed_closed)
            pools=sum(len(set(port.clients.values())) for port in ports)
            ws_tasks=(self._ws.managed_tasks() if self._ws is not None else 0)+int(self._ws_close is not None and not self._ws_close.done())
            managed=ws_tasks+sum(s.active+sum(not t.done() for t in s.tasks)+(s.closing and not s.closed)
                for s in self._scopes.values())+len(self._fixed_closing-self._fixed_closed)
            faults=sum(s.fault for s in self._scopes.values())+len(self._fixed_faults)
            fixed_faults=bool(self._fixed_faults-{'reward_share'}) or any(s.fault and s.demand.kind not in ('buy','income','reward_share') for s in self._scopes.values())
            # Put blockers first so a full BUY lane cannot hide a fixed fault.
            details=[self._fixed_fault_details[name] for name in sorted(self._fixed_faults)]
            details.extend(s.fault_detail for buy in (False,True) for s in self._scopes.values()
                if s.fault and (s.demand.kind in ('buy','income'))==buy)
            block_reasons=tuple(reason for reason,active in (
                ('cleanup_fault',fixed_faults),('capacity',len(owned)>=self.buy_cap),
                ('deferred',bool(deferred))) if active)
            return freeze(dict(cache_entries=len(self._cache),cache_bytes=self._cache_size,
                orderbook=dict(mode=self._orderbook_mode,shadow=dict(self._shadow.snapshot(),
                    retired_contexts=sum(s['retire_at'] is not None for s in self._shadow_states.values()),
                    active_contexts=len(self._shadow_contexts),pending_observations=len(self._shadow_books)),
                    pending_checks=len(self._ws_pending),wake_requests=self._ws_wake_requests,owned_tasks=ws_tasks,**self._books.status(mode=self._orderbook_mode)),
                next_dependency_monotonic=min(dependencies) if dependencies else None,
                waiting_for_event=not dependencies and (bool(self._fixed_faults)
                    or any(s.fault or s.retiring or s.closing for s in self._scopes.values())),
                policy=dict(buy_cap=self.buy_cap,cache_entries=self.cache_entries,cache_bytes=self.cache_bytes),
                observed_monotonic=self._observed,cleanup_faults=faults,owned_buy_scopes=len(owned),
                cleanup_fault_details=dict(total=faults,omitted=max(0,faults-CLEANUP_FAULT_ITEMS),
                    items=tuple(details[:CLEANUP_FAULT_ITEMS])),new_buy_block_reasons=block_reasons,
                financial_cleanup_faults=self._financial_cleanup_faults,
                last_financial_cleanup_fault=self._last_financial_cleanup_fault,
                last_financial_timeout=self._last_financial_timeout,
                financial_request_cleanup=self._financial_request_cleanup,
                retiring_buy_scopes=sum(s.retiring for s in owned),deferred_count=len(deferred),
                deferred_order_ids=tuple(d.order_id for d in deferred[:64]),new_buy_allowed=len(owned)<self.buy_cap and not deferred and not fixed_faults,
                ready_buy_count=sum(s.result is not None or s.fault_pending for s in owned),
                ready_count=sum(s.result is not None or s.fault_pending for s in self._scopes.values()),
                pool_count=pools,pool_limit=2*self.buy_cap+6,connection_limit=4*self.buy_cap+10+int(self._ws_future is not None),managed_tasks=managed,
                scopes=tuple(dict(key=s.demand.key,kind=s.demand.kind,attempt=s.attempt,started=s.started,next_start=s.next_start,
                    active=s.active,retiring=s.retiring,cleanup_fault=s.fault) for s in self._scopes.values())))

    def close(self,timeout=0):
        if not self._close_lock.acquire(blocking=False):return False
        try:return self._close_owned(timeout)
        finally:self._close_lock.release()

    def _close_owned(self,timeout):
        with self._lock:
            self._closed=True;self._demands={}
            self._shadow.set_mode('off')
            self._drop_shadow_work('closed')
            self._configured_book_tokens={};self._configured_buy_prices={};self._books.subscribe({})
            for scope in self._scopes.values():
                scope.result=None;scope.fault_pending=False
            if not self._started:return True
        self.wake();self._supervisor.join(max(0,timeout))
        if self._supervisor.is_alive():return False
        if self._started:
            self._market.stop();self._control.stop();self._started=False
        return True

def freeze_sources(sources):
    return MappingProxyType(dict(sources))


def _ws_source(source):
    return source is not None and isinstance(source.value,Mapping) and source.value.get('transport')=='ws'


def _json_value(value):
    if isinstance(value,Mapping):return {k:_json_value(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [_json_value(v) for v in value]
    return value


def _fixed_lane(kind):
    if kind in ('buy','income'):return None
    return kind if kind in ('financial','exact','reward_share') else 'low'
