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

class LPReadRuntime:
    def __init__(self, source_factory, *, clock=time.monotonic, utc_now=lambda:datetime.now(UTC), buy_cap=BUY_SCOPE_CAP,
                 cache_entries=REFERENCE_CACHE_ENTRIES, cache_bytes=REFERENCE_CACHE_BYTES):
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
                        scope.result=None;scope.fault_pending=False
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
                done=self._closed and not self._scopes and len(self._fixed_closed)==len(self._fixed_ports)
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
        with self._lock:
            cached=self._cache.get(key) if reference else None
            if cached is not None and cached[0].checked_at is not None:
                age=(self.utc_now()-cached[0].checked_at).total_seconds()
                if 0<=age<(5 if source in ('native_rewards','sponsored_rewards') and scope.demand.reward_pool_fresh else 30):
                    self._cache.move_to_end(key);return self._completed(scope,source,cached[0])
        result=await scope.port.read(request,scope.deadline)
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
                    result.append(replace(scope.result,cleanup_fault=scope.result.cleanup_fault or scope.fault))
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
            managed=sum(s.active+sum(not t.done() for t in s.tasks)+(s.closing and not s.closed)
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
                pool_count=pools,pool_limit=2*self.buy_cap+6,connection_limit=4*self.buy_cap+10,managed_tasks=managed,
                scopes=tuple(dict(key=s.demand.key,kind=s.demand.kind,attempt=s.attempt,started=s.started,next_start=s.next_start,
                    active=s.active,retiring=s.retiring,cleanup_fault=s.fault) for s in self._scopes.values())))

    def close(self,timeout=0):
        if not self._close_lock.acquire(blocking=False):return False
        try:return self._close_owned(timeout)
        finally:self._close_lock.release()

    def _close_owned(self,timeout):
        with self._lock:
            self._closed=True;self._demands={}
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


def _json_value(value):
    if isinstance(value,Mapping):return {k:_json_value(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [_json_value(v) for v in value]
    return value


def _fixed_lane(kind):
    if kind in ('buy','income'):return None
    return kind if kind in ('financial','exact','reward_share') else 'low'
