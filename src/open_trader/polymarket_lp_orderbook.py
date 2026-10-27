"""LP-owned, value-only order books. No execution or financial authority."""
import time
import threading
from functools import wraps
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from types import MappingProxyType


def number(value, *, price=False):
    value=Decimal(str(value))
    if not value.is_finite() or value<0 or (price and not 0<value<1):
        raise ValueError('invalid_level')
    return value


def rest_event(value):
    stamp=value.get('source_timestamp',value.get('timestamp'))
    if isinstance(stamp,datetime):stamp=str(int(stamp.timestamp()*1000))
    return dict(value,event_type='book',market=value.get('condition_id'),
        asset_id=value.get('token_id'),timestamp=stamp)


def synchronized(method):
    @wraps(method)
    def call(self,*args,**kwargs):
        with self._lock:return method(self,*args,**kwargs)
    return call


class OrderBooks:
    def __init__(self, *, clock=time.monotonic, utc_now=lambda:datetime.now(UTC)):
        self._lock=threading.RLock()
        self.clock, self.utc_now = clock, utc_now
        self.tokens={};self._books={};self.generation=0;self._buffer={};self._subscription=0;self._revision={}
        self.online=False;self.last_proof=None;self.proof_at=None;self.last_reason=None;self.queue_overflows=0
        self.reconciliation_mismatches=0;self.last_reconciliation=None
        self._synced_at={}
        self.buy_prices={};self.diagnostics=ShadowDiagnostics(clock=clock)
        self._fallbacks={};self._mismatches={}
        self._members={};self._member_sequence=0

    @synchronized
    def subscribe(self, tokens, *, buy_prices=None):
        """Replace exact token -> condition subscriptions."""
        self._books={t:b for t,b in self._books.items() if tokens.get(t)==self.tokens.get(t)}
        if tokens!=self.tokens:self._subscription+=1
        self._buffer={t:b for t,b in self._buffer.items() if tokens.get(t)==self.tokens.get(t)}
        self._revision={t:r for t,r in self._revision.items() if t in tokens}
        self._synced_at={t:v for t,v in self._synced_at.items() if tokens.get(t)==self.tokens.get(t)}
        self._fallbacks={t:v for t,v in self._fallbacks.items() if tokens.get(t)==self.tokens.get(t)}
        self._mismatches={t:v for t,v in self._mismatches.items() if tokens.get(t)==self.tokens.get(t)}
        self.buy_prices={t:tuple(v) for t,v in (buy_prices if buy_prices is not None else self.buy_prices).items() if t in tokens}
        members={}
        for token,condition in tokens.items():
            if self.tokens.get(token)==condition:members[token]=self._members[token]
            else:self._member_sequence+=1;members[token]=self._member_sequence
        self._members=members
        self.tokens=dict(tokens)

    @synchronized
    def delivery_ticket(self):
        return dict(self._members)

    @synchronized
    def connected(self):
        """Start a new connection generation, initially unsynchronized."""
        self.generation+=1;self._books={};self._buffer={};self._synced_at={}
        self.online=True;self.proof()

    @synchronized
    def proof(self):
        if self.online:self.last_proof=self.clock();self.proof_at=self.utc_now()

    @synchronized
    def disconnected(self, reason='disconnect'):
        if reason=='queue_overflow':self.queue_overflows+=1
        self.online=False;self.generation+=1;self._books={};self._buffer={};self.last_reason=reason
        self._synced_at={}
        for token in self.tokens:self._fallbacks[token]=dict(reason=reason,at=self.utc_now().isoformat())

    @synchronized
    def check_liveness(self):
        if self.online and self.clock()-self.last_proof>=15:self.disconnected('silence')

    @synchronized
    def status(self, *, mode='use'):
        items=[]
        for token in sorted(self.tokens)[:16]:
            fallback=self._fallbacks.get(token,{})
            items.append(dict(token_id=token[:256],route='ws' if mode=='use' and self.snapshot(token) is not None else 'rest',
                ws_available=self.snapshot(token) is not None,
                fallback_reason=fallback.get('reason'),fallback_at=fallback.get('at'),
                reconciliation_mismatches=self._mismatches.get(token,0)))
        return dict(connected=self.online,generation=self.generation,reason=self.last_reason,
            tokens=dict(total=len(self.tokens),omitted=max(0,len(self.tokens)-16),items=items),
            reconciliation_mismatches=self.reconciliation_mismatches,
            last_reconciliation=dict(self.last_reconciliation) if self.last_reconciliation else None,
            last_proof_monotonic=self.last_proof,
            retained_tokens=len(set(self._books)|set(self._synced_at)|set(self._buffer)|set(self._revision)),
            queue_overflows=self.queue_overflows,subscribed=len(self.tokens),synced=sum(self.snapshot(t) is not None for t in self.tokens))

    @synchronized
    def market_book(self, token):
        value=self.snapshot(token)
        if value is None:return None
        return dict(value,transport='ws',connection_generation=self.generation,
            **{side:tuple(MappingProxyType(dict(price=p,size=s)) for p,s in value[side])
            for side in ('bids','asks')})

    @synchronized
    def synced_since(self, token):
        return self._synced_at.get(token,self.clock())

    @synchronized
    def seed_ticket(self, token):
        return (self.generation,self._subscription,token,self.tokens.get(token),self._revision.get(token,0))

    @synchronized
    def seed(self, token, event, ticket):
        if ticket!=self.seed_ticket(token) or event.get('asset_id')!=token:return False
        buffered=self._buffer.pop(token,())
        self.handle(dict(event,event_type='book'),connection_data=False)
        value=self._books.get(token)
        if value is None:return False
        for update in buffered:
            try:
                if number(update['timestamp'])>=value['timestamp']:self.handle(update,connection_data=False)
            except (ValueError,InvalidOperation,KeyError,TypeError):
                self._books.pop(token,None);return False
        return token in self._books

    @synchronized
    def reconcile(self, token, event):
        old=self._books.get(token)
        try:
            if old is None or number(event['timestamp'])<old['timestamp']:return False
            if event.get('asset_id')!=token:return False
            candidate=OrderBooks(clock=self.clock,utc_now=self.utc_now)
            candidate.subscribe({token:self.tokens[token]});candidate.connected();candidate.handle(event)
            new=candidate._books.get(token)
            if new is None:
                self._books.pop(token,None);return False
            mismatch=any(new[side]!=old[side] for side in ('bids','asks'))
            if mismatch:
                self.reconciliation_mismatches+=1
                self._mismatches[token]=self._mismatches.get(token,0)+1
                self.last_reconciliation=dict(reason='full_book_mismatch',at=self.utc_now().isoformat())
            self._books[token]=new;self._revision[token]=self._revision.get(token,0)+1
            return mismatch
        except (ValueError,InvalidOperation,KeyError,TypeError):return False

    def _validate(self, token, event, value, row=None):
        if event.get('market')!=self.tokens[token]:raise ValueError('identity')
        stamp=number(event['timestamp'])
        old=self._books.get(token)
        if old and stamp<old['timestamp']:raise ValueError('timestamp_regression')
        if value['bids'] and value['asks'] and value['bids'][0][0]>=value['asks'][0][0]:
            raise ValueError('crossed_book')
        for field,side in (('best_bid','bids'),('best_ask','asks')):
            if row is not None and field in row:
                best=value[side][0][0] if value[side] else Decimal(0 if side=='bids' else 1)
                if number(row[field])!=best:raise ValueError(field+'_mismatch')
        return dict(value,condition_id=self.tokens[token],token_id=token,timestamp=stamp,received_at=self.utc_now())

    @synchronized
    def handle(self, event, *, connection_data=True, membership=None):
        """Apply a venue message, failing closed on invalid data."""
        if not self.online:return set()
        if connection_data:self.proof()
        kind=event.get('event_type')
        rows=event.get('price_changes',()) if kind=='price_change' else (event,)
        changed=set()
        for row in rows:
            token=row.get('asset_id')
            if token not in self.tokens:continue
            if membership is not None and membership.get(token)!=self._members[token]:continue
            old_book=self._books.get(token)
            try:
                if kind=='tick_size_change':raise ValueError('tick_size_change')
                if kind=='book':
                    value={side:tuple(sorted({number(r['price'],price=True):number(r['size'])
                        for r in event[side] if number(r['size'])!=0}.items(),reverse=side=='bids'))
                        for side in ('bids','asks')}
                elif kind=='price_change':
                    old=self._books.get(token)
                    if old is None:
                        buffer=self._buffer.setdefault(token,[])
                        if len(buffer)<256:
                            buffer.append(dict(event,price_changes=[dict(row)]))
                        else:self.disconnected('queue_overflow');return changed
                        continue
                    if row['side'] not in ('BUY','SELL'):raise ValueError('invalid_side')
                    side='bids' if row['side']=='BUY' else 'asks'
                    levels=dict(old[side]);price=number(row['price'],price=True);size=number(row['size'])
                    if size:levels[price]=size
                    else:levels.pop(price,None)
                    value=dict(old,**{side:tuple(sorted(levels.items(),reverse=side=='bids'))})
                else:continue
                if token not in self._books:self._synced_at[token]=self.clock()
                self._books[token]=self._validate(token,event,value,row)
                self._revision[token]=self._revision.get(token,0)+1
                prices=self.buy_prices.get(token,())
                if prices and old_book is not None:
                    threshold=min(prices)
                    if tuple(r for r in old_book['bids'] if r[0]>=threshold)!=tuple(r for r in value['bids'] if r[0]>=threshold):
                        changed.add(token)
            except (ValueError,InvalidOperation,KeyError,TypeError) as exc:
                self._books.pop(token,None)
                self._revision[token]=self._revision.get(token,0)+1;changed.add(token)
                reason=str(exc) if type(exc) is ValueError and str(exc) in (
                    'identity','timestamp_regression','crossed_book','best_bid_mismatch','best_ask_mismatch',
                    'invalid_level','invalid_side','tick_size_change') else 'invalid_message'
                self._fallbacks[token]=dict(reason=reason,at=self.utc_now().isoformat())
        return changed

    @synchronized
    def snapshot(self, token):
        """Return a detached immutable snapshot or None when unavailable."""
        value=self._books.get(token)
        return (MappingProxyType(dict(value,received_at=self.proof_at,checked_at=self.proof_at))
            if value is not None and self.online and 0<=self.clock()-self.last_proof<=10 else None)


class OrderBookWebSocket:
    """One explicitly owned connection on the existing market I/O loop."""
    def __init__(self, books, *, url='wss://ws-subscriptions-clob.polymarket.com/ws/market', queue_limit=256, on_update=lambda tokens:None):
        self.books=books;self.url=url;self.queue_limit=queue_limit;self.stopping=False;self.closed=False;self.socket=None
        self.on_update=on_update;self._runner=None;self._reader=None;self._waiter=None

    async def run(self):
        import asyncio
        import json
        from websockets.asyncio.client import connect
        from .prediction_websocket_compat import install_proxy_cleanup
        install_proxy_cleanup()
        self._runner=asyncio.current_task()
        try:
            while not self.stopping:
                if not self.books.tokens:
                    await asyncio.sleep(.02);continue
                try:
                    async with connect(self.url,ping_interval=None,max_size=4*1024*1024) as socket:
                        self.socket=socket;self.books.connected()
                        subscribed=set(self.books.tokens)
                        await socket.send(json.dumps(dict(type='market',assets_ids=sorted(subscribed))))
                        queue=asyncio.Queue(maxsize=self.queue_limit)
                        generation=self.books.generation
                        reader=self._reader=asyncio.create_task(self._receive(socket,queue,generation))
                        last_ping=self.books.clock()
                        try:
                            while not self.stopping:
                                wanted=set(self.books.tokens)
                                for operation,tokens in (('unsubscribe',subscribed-wanted),('subscribe',wanted-subscribed)):
                                    if tokens:await socket.send(json.dumps(dict(operation=operation,assets_ids=sorted(tokens))))
                                subscribed=wanted
                                if self.books.clock()-last_ping>=10:
                                    await socket.send('PING');last_ping=self.books.clock()
                                self.books.check_liveness()
                                if not self.books.online or self.books.generation!=generation:break
                                self._waiter=asyncio.create_task(queue.get())
                                try:event,received,membership=await asyncio.wait_for(self._waiter,.02)
                                except TimeoutError:
                                    if reader.done():break
                                    continue
                                finally:self._waiter=None
                                if self.books.online and self.books.generation==generation:
                                    self.on_update(self.books.handle(event,connection_data=False,membership=membership))
                                    self.books.diagnostics.update_latency(self.books.clock()-received)
                        finally:
                            if not reader.done():reader.cancel()
                            await asyncio.gather(reader,return_exceptions=True)
                except asyncio.CancelledError:raise
                except Exception:self.books.disconnected('disconnect')
                finally:
                    self.socket=None
                    if self.books.online:self.books.disconnected('disconnect')
                if not self.stopping:await asyncio.sleep(.1)
        finally:
            self.books.disconnected('closed');self.closed=True

    async def _receive(self, socket, queue, generation):
        import asyncio
        import json
        try:
            async for raw in socket:
                if self.books.generation!=generation:return
                self.books.proof()
                if raw=='PONG':continue
                parsed=json.loads(raw)
                for event in parsed if isinstance(parsed,list) else (parsed,):
                    if not isinstance(event,dict):raise ValueError('invalid_message')
                    queue.put_nowait((event,self.books.clock(),self.books.delivery_ticket()))
        except asyncio.CancelledError:raise
        except asyncio.QueueFull:
            self.books.disconnected('queue_overflow')
            await socket.close()
        except Exception:
            self.books.disconnected('disconnect')
        finally:
            if self.books.generation==generation and self.books.online:self.books.disconnected('disconnect')

    def managed_tasks(self):
        return sum(task is not None and not task.done() for task in (self._runner,self._reader,self._waiter))

    async def aclose(self):
        self.stopping=True
        if self.socket is not None:await self.socket.close()


class ShadowDiagnostics:
    """Bounded measurements; a report grants no trading or switch authority."""
    def __init__(self, *, clock=time.monotonic):
        from collections import deque
        self.clock=clock;self._lock=threading.RLock()
        self.samples=0;self.mismatches=0;self.not_comparable=0;self.last=None
        self.reasons={};self._start=None;self._previous=None;self.coverage=0
        self._updates=deque(maxlen=256);self._decisions=deque(maxlen=256)

    @synchronized
    def record(self, rest, ws):
        comparable=rest['state']!='unknown' and ws['state']!='unknown'
        consistent=comparable and rest['state']==ws['state']
        reason=('consistent' if consistent else 'decision_mismatch' if comparable else
            'ws_unknown' if ws['state']=='unknown' else 'rest_unknown')
        row=dict(comparable=comparable,rest_can_cancel=rest['state']=='triggered',
            ws_can_cancel=ws['state']=='triggered',consistent=consistent,reason=reason)
        self.samples+=1;self.mismatches+=int(comparable and not consistent);self.not_comparable+=int(not comparable)
        self.reasons[reason]=self.reasons.get(reason,0)+1;self.last=row
        now=self.clock()
        if consistent:
            if self._previous is None or not 0<=now-self._previous<=10:self._start=now
            self.coverage=max(self.coverage,now-self._start)
            self._previous=now
        else:self._start=None;self._previous=None
        return dict(row)

    @synchronized
    def update_latency(self, seconds):self._updates.append(max(0,float(seconds)))

    @synchronized
    def decision_latency(self, seconds):self._decisions.append(max(0,float(seconds)))

    @staticmethod
    def _distribution(samples):
        values=sorted(samples)
        return dict(retained=len(values),p50_seconds=values[int((len(values)-1)*.5)] if values else None,
            p95_seconds=values[int((len(values)-1)*.95)] if values else None,max_seconds=max(values) if values else None)

    @synchronized
    def snapshot(self):
        return dict(samples=self.samples,mismatches=self.mismatches,not_comparable=self.not_comparable,
            last=dict(self.last) if self.last else None,reasons=dict(self.reasons),coverage_seconds=self.coverage,
            assessment='UNKNOWN' if not self.samples else 'FAIL' if self.mismatches or self.not_comparable else
                'PASS' if self.coverage>=7200 else 'PENDING',
            rest_can_cancel_implies_ws_can_cancel=(None if not self.samples else self.mismatches==0 and self.not_comparable==0),
            automatic_enable=False,arrival_to_update=self._distribution(self._updates),
            wake_to_decision=self._distribution(self._decisions))
