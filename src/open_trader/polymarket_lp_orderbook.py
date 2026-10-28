"""LP-owned, value-only order books. No execution or financial authority."""
import time
import random
import threading
from functools import wraps
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime, timedelta
from types import MappingProxyType


def number(value, *, price=False):
    value=Decimal(str(value))
    if not value.is_finite() or value<0 or (price and not 0<value<1):
        raise ValueError('invalid_level')
    return value


def exchange_datetime(stamp):
    """Export venue milliseconds without float rounding across an acceptance cutoff."""
    if not 0 <= stamp < Decimal('253402300800000'):
        raise ValueError('invalid_timestamp')
    return datetime(1970,1,1,tzinfo=UTC)+timedelta(microseconds=int(stamp // Decimal('.001')))


def book_after_buy_acceptance(book, *, condition_id, token_id, accepted_at):
    """One value-only acceptance predicate for live and shadow books."""
    if accepted_at is None:return True  # Recovered orders have no proven local cutoff.
    from collections.abc import Mapping
    if (not isinstance(book,Mapping) or book.get('condition_id')!=condition_id
            or book.get('token_id')!=token_id):return None
    stamp=book.get('confirmation_at') if book.get('transport')=='ws' else book.get('timestamp')
    if not isinstance(stamp,datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:return None
    if book.get('transport')=='ws':
        if (book.get('confirmation_identity') is None
                or book.get('confirmation_identity')!=book.get('sync_identity')):return None
        return stamp>accepted_at
    return stamp>=accepted_at


def rest_event(value):
    stamp=value.get('source_timestamp',value.get('timestamp'))
    if isinstance(stamp,datetime):stamp=str(int(stamp.timestamp()*1000))
    return dict(value,event_type='book',market=value.get('condition_id'),
        asset_id=value.get('token_id'),timestamp=stamp)


def book_levels(event):
    return {side:tuple(sorted({number(row['price'],price=True):number(row['size'])
        for row in event[side] if number(row['size'])!=0}.items(),reverse=side=='bids'))
        for side in ('bids','asks')}


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
        self._sync_sequence=0;self._sync_identity={};self._confirmations={}
        self._history={};self._history_gaps={}
        self._version_at={}
        self._receipts={};self._receipt_sequence=0;self._receipt_limit=257;self._receive_drained_at=None
        self.history_overflows=0
        self.buy_prices={};self.diagnostics=ShadowDiagnostics(clock=clock)
        self._fallbacks={};self._mismatches={}
        self._members={};self._member_sequence=0
        self.failure_streak=0;self.next_retry=None;self.retry_reason=None
        from collections import deque
        self.connections=0;self.disconnects=0;self.connection_reason_counts={};self.connection_events=deque(maxlen=16)
        self.audit_counts=dict(attempts=0,compared=0,equal=0,different=0,rest_older_skipped=0,unavailable=0)

    def _audit_event(self, token, reason):
        import json,logging
        from .polymarket_trading import _lp_capture_read_log
        if reason=='attempt':self.audit_counts['attempts']+=1
        else:
            self.audit_counts[reason]+=1
            if reason in ('equal','different'):self.audit_counts['compared']+=1
            self.diagnostics.audit_result(reason)
        row=dict(token_prefix=token[:8],reason=reason,at=self.utc_now().isoformat())
        _lp_capture_read_log(lambda:logging.getLogger(__name__).info(
            'prediction_lp_orderbook_audit %s',json.dumps(row,sort_keys=True)))

    @synchronized
    def audit_started(self, token):self._audit_event(token,'attempt')

    def _connection_event(self, event, reason=None, *, code=None, exception_class=None):
        import json,logging
        from .polymarket_trading import _lp_capture_read_log
        reason=reason if reason in ('server_close','invalid_json','silence','queue_overflow','send_failure',
            'other_exception','closed') else 'other_exception' if reason is not None else None
        row=dict(event=event,reason=reason,at=self.utc_now().isoformat(),
            code=code if type(code) is int and 0<=code<=4999 else None,
            exception_class=''.join(c for c in (exception_class or '')[:64] if c.isascii() and (c.isalnum() or c=='_')) or None)
        if event=='connected':self.connections+=1
        else:
            self.disconnects+=1;self.connection_reason_counts[reason]=self.connection_reason_counts.get(reason,0)+1
            self.diagnostics.disconnect(reason)
        self.connection_events.append(row)
        _lp_capture_read_log(lambda:logging.getLogger(__name__).info(
            'prediction_lp_orderbook_connection %s',json.dumps(row,sort_keys=True)))

    @synchronized
    def subscribe(self, tokens, *, buy_prices=None):
        """Replace exact token -> condition subscriptions."""
        self._books={t:b for t,b in self._books.items() if tokens.get(t)==self.tokens.get(t)}
        if tokens!=self.tokens:self._subscription+=1
        self._buffer={t:b for t,b in self._buffer.items() if tokens.get(t)==self.tokens.get(t)}
        self._revision={t:r for t,r in self._revision.items() if t in tokens}
        self._synced_at={t:v for t,v in self._synced_at.items() if tokens.get(t)==self.tokens.get(t)}
        self._sync_identity={t:v for t,v in self._sync_identity.items() if tokens.get(t)==self.tokens.get(t)}
        self._confirmations={t:v for t,v in self._confirmations.items() if tokens.get(t)==self.tokens.get(t)}
        self._history={t:v for t,v in self._history.items() if tokens.get(t)==self.tokens.get(t)}
        self._history_gaps={t:v for t,v in self._history_gaps.items() if tokens.get(t)==self.tokens.get(t)}
        self._version_at={t:v for t,v in self._version_at.items() if tokens.get(t)==self.tokens.get(t)}
        self._fallbacks={t:v for t,v in self._fallbacks.items() if tokens.get(t)==self.tokens.get(t)}
        self._mismatches={t:v for t,v in self._mismatches.items() if tokens.get(t)==self.tokens.get(t)}
        self.buy_prices={t:tuple(v) for t,v in (buy_prices if buy_prices is not None else self.buy_prices).items() if t in tokens}
        members={}
        for token,condition in tokens.items():
            if self.tokens.get(token)==condition:members[token]=self._members[token]
            else:self._member_sequence+=1;members[token]=self._member_sequence
        self._members=members
        self.tokens=dict(tokens)
        self._prune_receipts()

    @synchronized
    def delivery_ticket(self):
        return dict(self._members)

    @synchronized
    def connected(self):
        """Start a new connection generation, initially unsynchronized."""
        self.generation+=1;self._books={};self._buffer={};self._synced_at={}
        self._sync_identity={};self._confirmations={}
        self._history={};self._history_gaps={}
        self._version_at={}
        self._receipts.clear();self._receive_drained_at=None
        self.online=True;self.last_proof=self.clock();self.proof_at=None
        self._connection_event('connected')

    @synchronized
    def proof(self, *, received_at=None, received_monotonic=None, identities=None):
        if self.online:
            received=self.clock() if received_monotonic is None else received_monotonic
            stamp=self.utc_now() if received_at is None else received_at
            if self.proof_at is None or received>=self.last_proof:
                self.last_proof=received;self.proof_at=stamp
            for token,identity in (self._sync_identity if identities is None else identities).items():
                previous=self._confirmations.get(token)
                if (self._sync_identity.get(token)==identity and token in self._books
                        and (previous is None or received>=previous[2])):
                    self._confirmations[token]=(stamp,identity,received)
            for token in self._history:self._prune_history(token)

    @synchronized
    def confirmation_ticket(self):
        """Capture token continuity at actual receive, before queued application."""
        return dict(self._sync_identity)

    def _prune_receipts(self):
        for key,(sequence,received,identities) in tuple(self._receipts.items()):
            current={t:identity for t,identity in identities.items()
                if identity==self.continuity_ticket(t) and t in self.tokens and self.online}
            if current:self._receipts[key]=(sequence,received,current)
            else:self._receipts.pop(key,None)

    @synchronized
    def queue_receipt(self, queue, event, received, received_at, identities):
        """Admit metadata atomically with the existing payload queue; no replay copy."""
        import asyncio
        self._receipt_limit=queue.maxsize+1
        if len(self._receipts)>=self._receipt_limit:raise asyncio.QueueFull
        rows=event.get('price_changes',()) if event.get('event_type')=='price_change' else (event,)
        tokens={row.get('asset_id') for row in rows if isinstance(row,dict)
            and isinstance(row.get('asset_id'),str)} if isinstance(rows,(tuple,list)) else set()
        continuity={t:(self.generation,self._members[t],identities.get(t))
            for t in tokens if t in self.tokens}
        queue.put_nowait((event,received,self.delivery_ticket(),received_at,identities))
        self._receipt_sequence+=1
        if continuity:self._receipts[id(event)]=(self._receipt_sequence,received,continuity)

    @synchronized
    def receipt_applied(self, event):
        self._receipts.pop(id(event),None);self._prune_receipts()

    @synchronized
    def receive_drained(self, generation):
        # An empty real consumer poll accounts for equal-time receipts before
        # equality can close a window. No extra timer or grace period.
        if self.online and self.generation==generation:self._receive_drained_at=self.clock()

    @synchronized
    def receipt_cursor(self):return self._receipt_sequence

    @synchronized
    def pending_receipt(self, token, *, until, continuity, after=0):
        current=self.continuity_ticket(token)
        if not self.online or token not in self.tokens or current!=continuity:return False
        return any(sequence>after and received<=until and identities.get(token)==current
            for sequence,received,identities in self._receipts.values())

    @synchronized
    def comparison_settled(self, token, *, until, continuity, after=0):
        if self.pending_receipt(token,until=until,continuity=continuity,after=after):return False
        return self.clock()>until or (self.clock()==until and
            self._receive_drained_at is not None and self._receive_drained_at>=until)

    @synchronized
    def disconnected(self, reason='disconnect', *, code=None, exception_class=None):
        if not self.online:return
        if reason=='queue_overflow':self.queue_overflows+=1
        self._connection_event('disconnected',reason,code=code,exception_class=exception_class)
        self._receipts.clear();self._receive_drained_at=None
        self.online=False;self.generation+=1;self._books={};self._buffer={};self.last_reason=reason
        self._synced_at={}
        self._sync_identity={};self._confirmations={}
        self._history={};self._history_gaps={}
        self._version_at={}
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
            failure_streak=self.failure_streak,next_retry_monotonic=self.next_retry,retry_reason=self.retry_reason,
            tokens=dict(total=len(self.tokens),omitted=max(0,len(self.tokens)-16),items=items),
            reconciliation_mismatches=self.reconciliation_mismatches,
            last_reconciliation=dict(self.last_reconciliation) if self.last_reconciliation else None,
            last_proof_monotonic=self.last_proof,
            retained_tokens=len(set(self._books)|set(self._synced_at)|set(self._buffer)|set(self._revision)),
            history_versions=sum(len(rows) for rows in self._history.values()),history_overflows=self.history_overflows,
            connections=self.connections,disconnects=self.disconnects,
            **{'audit_'+name:value for name,value in self.audit_counts.items()},
            connection_reason_counts=dict(self.connection_reason_counts),connection_events=tuple(dict(r) for r in self.connection_events),
            pending_receipts=len(self._receipts),receipt_capacity=self._receipt_limit,
            queue_overflows=self.queue_overflows,subscribed=len(self.tokens),synced=sum(self.snapshot(t) is not None for t in self.tokens))

    @synchronized
    def retry_state(self, streak, deadline, reason):
        self.failure_streak=streak;self.next_retry=deadline;self.retry_reason=reason

    @synchronized
    def market_book(self, token):
        value=self.snapshot(token)
        if value is None:return None
        return dict(value,timestamp=exchange_datetime(value['timestamp']),source_timestamp=value['timestamp'],
            transport='ws',connection_generation=self.generation,
            sync_identity=self._sync_identity.get(token),
            confirmation_at=self._confirmations.get(token,(None,None))[0],
            confirmation_identity=self._confirmations.get(token,(None,None))[1],
            confirmation_monotonic=self._confirmations.get(token,(None,None,None))[2],
            book_received_monotonic=self._version_at.get(token),
            book_received_at=self._books[token]['received_at'],
            book_version=(self.generation,self._members[token],self._revision.get(token,0)),
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
            if old is None or event.get('asset_id')!=token:
                self._audit_event(token,'unavailable');return False
            if number(event['timestamp'])<old['timestamp']:
                self._audit_event(token,'rest_older_skipped');return False
            new=self._validate(token,event,book_levels(event),event)
            mismatch=any(new[side]!=old[side] for side in ('bids','asks'))
            self._audit_event(token,'different' if mismatch else 'equal')
            if mismatch:
                self.reconciliation_mismatches+=1
                self._mismatches[token]=self._mismatches.get(token,0)+1
                self.last_reconciliation=dict(reason='full_book_mismatch',at=self.utc_now().isoformat())
            self._books[token]=new;self._revision[token]=self._revision.get(token,0)+1
            return mismatch
        except (ValueError,InvalidOperation,KeyError,TypeError):
            self._audit_event(token,'unavailable');return False

    def _validate(self, token, event, value, row=None):
        if event.get('market')!=self.tokens[token]:raise ValueError('identity')
        stamp=number(event['timestamp'])
        exchange_datetime(stamp)  # Reject unrepresentable exchange times at message admission.
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
    def handle(self, event, *, connection_data=True, membership=None, receipt=None):
        """Apply a venue message, failing closed on invalid data."""
        if not self.online:return set()
        kind=event.get('event_type')
        rows=event.get('price_changes',()) if kind=='price_change' else (event,)
        changed=set();confirmed=dict(self._sync_identity if receipt is None else receipt[2]);valid_tokens=set()
        for row in rows:
            token=row.get('asset_id')
            if token not in self.tokens:continue
            if membership is not None and membership.get(token)!=self._members[token]:continue
            old_book=self._books.get(token)
            try:
                if kind=='tick_size_change':raise ValueError('tick_size_change')
                if kind=='book':
                    value=book_levels(event)
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
                self._books[token]=self._validate(token,event,value,row)
                if receipt is not None:self._books[token]['received_at']=receipt[1]
                if token not in self._sync_identity:
                    self._synced_at[token]=self.clock();self._sync_sequence+=1
                    self._sync_identity[token]=(self.generation,self._members[token],self._sync_sequence)
                self._revision[token]=self._revision.get(token,0)+1
                self._version_at[token]=receipt[0] if receipt is not None else self.clock()
                valid_tokens.add(token)
                confirmed[token]=self._sync_identity[token]
                prices=self.buy_prices.get(token,())
                if prices and old_book is not None:
                    threshold=min(prices)
                    if tuple(r for r in old_book['bids'] if r[0]>=threshold)!=tuple(r for r in value['bids'] if r[0]>=threshold):
                        changed.add(token)
            except (ValueError,InvalidOperation,KeyError,TypeError) as exc:
                self._books.pop(token,None)
                self._sync_identity.pop(token,None);self._confirmations.pop(token,None);self._synced_at.pop(token,None)
                self._history.pop(token,None)
                self._version_at.pop(token,None)
                self._prune_receipts()
                self._revision[token]=self._revision.get(token,0)+1;changed.add(token)
                reason=str(exc) if type(exc) is ValueError and str(exc) in (
                    'identity','timestamp_regression','crossed_book','best_bid_mismatch','best_ask_mismatch',
                    'invalid_level','invalid_side','tick_size_change') else 'invalid_message'
                self._fallbacks[token]=dict(reason=reason,at=self.utc_now().isoformat())
        if connection_data and valid_tokens:
            self.proof(**(dict(received_monotonic=receipt[0],received_at=receipt[1],identities=confirmed)
                if receipt is not None else dict(identities=confirmed)))
        for token in valid_tokens:self._remember(token,receipt[0] if receipt is not None else self.clock(),independent=connection_data)
        return changed

    def _prune_history(self, token):
        history=self._history[token]
        while len(history)>1 and history[1]['at']<=self.clock()-2:history.popleft()

    def _remember(self, token, at, *, independent):
        from collections import deque
        book=self.market_book(token)
        if book is None:return
        history=self._history.setdefault(token,deque())
        history.append(dict(at=at,book=MappingProxyType(book),independent=independent))
        self._prune_history(token)
        while len(history)>64:
            history.popleft();self._history_gaps[token]=history[0]['at'];self.history_overflows+=1

    @synchronized
    def continuity_ticket(self, token):
        return (self.generation,self._members.get(token),self._sync_identity.get(token))

    @synchronized
    def comparison_versions(self, token, *, since, until, continuity=None):
        current=self.continuity_ticket(token);reason=None
        if token not in self.tokens:reason='not_subscribed'
        elif continuity is not None and continuity[:2]!=current[:2]:reason='connection_gap'
        elif continuity is not None and continuity[2] is not None and continuity[2]!=current[2]:reason='token_sync_gap'
        elif not self.online or token not in self._books:reason='ws_unsynced'
        elif self._history_gaps.get(token,float('-inf'))>since:reason='history_capacity'
        rows=self.recent_versions(token,since=since,until=until)
        eligible=tuple(row for row in rows if row['independent'] and
            row['book']['sync_identity']==current[2]) if reason not in (
                'not_subscribed','connection_gap','token_sync_gap','ws_unsynced') else ()
        if not eligible and reason is None:reason='not_independent'
        return MappingProxyType(dict(versions=eligible,reason=reason))

    @synchronized
    def recent_versions(self, token, *, since, until):
        """Detached versions active in the interval, including its quiet anchor."""
        history=tuple(self._history.get(token,()))
        result=[]
        for index,row in enumerate(history):
            end=history[index+1]['at'] if index+1<len(history) else self.clock()
            if row['at']<=until and end>=since:
                result.append(MappingProxyType(dict(row)))
        return tuple(result)

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
        self._failure_streak=0;self._stop_event=None

    async def run(self):
        import asyncio
        import json
        from websockets.asyncio.client import connect
        from .prediction_websocket_compat import install_proxy_cleanup
        install_proxy_cleanup()
        self._runner=asyncio.current_task();self._stop_event=asyncio.Event()
        try:
            while not self.stopping:
                if not self.books.tokens:
                    await asyncio.sleep(.02);continue
                try:
                    async with connect(self.url,ping_interval=None,max_size=4*1024*1024) as socket:
                        self.socket=socket;self.books.connected()
                        subscribed=set(self.books.tokens)
                        await self._send(socket,json.dumps(dict(type='market',assets_ids=sorted(subscribed))))
                        queue=asyncio.Queue(maxsize=self.queue_limit)
                        generation=self.books.generation
                        reader=self._reader=asyncio.create_task(self._receive(socket,queue,generation))
                        last_ping=self.books.clock()
                        try:
                            while not self.stopping:
                                wanted=set(self.books.tokens)
                                for operation,tokens in (('unsubscribe',subscribed-wanted),('subscribe',wanted-subscribed)):
                                    if tokens:await self._send(socket,json.dumps(dict(operation=operation,assets_ids=sorted(tokens))))
                                subscribed=wanted
                                if self.books.clock()-last_ping>=5:
                                    await self._send(socket,'PING');last_ping=self.books.clock()
                                self.books.check_liveness()
                                if not self.books.online or self.books.generation!=generation:break
                                if self._waiter is None:self._waiter=asyncio.create_task(queue.get())
                                ready,_=await asyncio.wait((self._waiter,),timeout=.02)
                                if not ready:
                                    self.books.receive_drained(generation)
                                    if reader.done():break
                                    continue
                                event,received,membership,received_at,identities=self._waiter.result()
                                self._waiter=None
                                if self.books.online and self.books.generation==generation:
                                    rows=event.get('price_changes',()) if event.get('event_type')=='price_change' else (event,)
                                    before={row.get('asset_id'):self.books.seed_ticket(row.get('asset_id')) for row in rows}
                                    try:
                                        changed=self.books.handle(event,membership=membership,
                                            receipt=(received,received_at,identities))
                                    finally:self.books.receipt_applied(event)
                                    if any(self.books.seed_ticket(token)!=ticket and self.books.snapshot(token) is not None
                                            for token,ticket in before.items()):
                                        self._valid_data()
                                    self.on_update(changed)
                                    self.books.diagnostics.update_latency(self.books.clock()-received)
                        finally:
                            if self._waiter is not None:
                                if not self._waiter.done():self._waiter.cancel()
                                await asyncio.gather(self._waiter,return_exceptions=True)
                                self._waiter=None
                            if not reader.done():reader.cancel()
                            await asyncio.gather(reader,return_exceptions=True)
                except asyncio.CancelledError:raise
                except Exception as exc:self.books.disconnected('other_exception',exception_class=type(exc).__name__)
                finally:
                    self.socket=None
                    if self.books.online:self.books.disconnected('closed' if self.stopping else 'server_close')
                if not self.stopping:
                    self._failure_streak+=1
                    base=min(30,.25*2**min(7,self._failure_streak-1))
                    deadline=self.books.clock()+min(30,base*(1+random.uniform(0,.2)))
                    self.books.retry_state(self._failure_streak,deadline,self.books.last_reason or 'disconnect')
                    while not self.stopping and self.books.tokens and self.books.clock()<deadline:
                        try:
                            # Await in the accounted resident runner; no hidden Event.wait task.
                            async with asyncio.timeout(min(.02,deadline-self.books.clock())):
                                await self._stop_event.wait()
                        except asyncio.TimeoutError:pass
        finally:
            self.books.disconnected('closed');self.books.retry_state(self._failure_streak,None,'closed');self.closed=True

    async def _receive(self, socket, queue, generation):
        import asyncio
        import json
        from websockets.exceptions import ConnectionClosed
        try:
            async for raw in socket:
                if self.books.generation!=generation:return
                if raw=='PONG':
                    self.books.proof();self._valid_data();self.on_update(set());continue
                parsed=json.loads(raw)
                received,received_at=self.books.clock(),self.books.utc_now()
                identities=self.books.confirmation_ticket()
                for event in parsed if isinstance(parsed,list) else (parsed,):
                    if not isinstance(event,dict):raise ValueError('invalid_message')
                    self.books.queue_receipt(queue,event,received,received_at,identities)
        except asyncio.CancelledError:raise
        except asyncio.QueueFull:
            self.books.disconnected('queue_overflow')
            await socket.close()
        except json.JSONDecodeError:self.books.disconnected('invalid_json')
        except ConnectionClosed:self.books.disconnected('server_close',code=socket.close_code)
        except Exception as exc:self.books.disconnected('other_exception',exception_class=type(exc).__name__)
        finally:
            if self.books.generation==generation and self.books.online:
                self.books.disconnected('closed' if self.stopping else 'server_close',code=socket.close_code)

    async def _send(self, socket, message):
        try:await socket.send(message)
        except Exception as exc:
            self.books.disconnected('send_failure',exception_class=type(exc).__name__)
            raise

    def _valid_data(self):
        self._failure_streak=0
        self.books.retry_state(0,None,None)

    def managed_tasks(self):
        return sum(task is not None and not task.done() for task in (self._runner,self._reader,self._waiter))

    async def aclose(self):
        self.stopping=True
        if self._stop_event is not None:self._stop_event.set()
        if self.socket is not None:await self.socket.close()


class ShadowDiagnostics:
    """Bounded measurements; a report grants no trading or switch authority."""
    def __init__(self, *, clock=time.monotonic):
        from collections import deque, OrderedDict
        self.clock=clock;self._lock=threading.RLock()
        self.samples=0;self.mismatches=0;self.not_comparable=0;self.last=None
        self.reasons={};self._start=None;self._previous=None;self.coverage=0
        self._updates=deque(maxlen=256);self._decisions=deque(maxlen=256)
        self.book_matches=0;self.book_mismatches=0;self.book_pending=0
        self.rest_cancel_observations=0;self.cancel_pending=0;self.cancel_matches=0;self.ws_missed_cancels=0
        self._cancel_timing=deque(maxlen=256)
        self.eligible_rest_reads=0;self.context_missing_reads=0
        self.incomplete_observations=0
        self.ws_early_events=0;self.ws_transient_events=0
        self._disconnect_window=deque(maxlen=7);self.disconnect_hour_exceeded=False
        self.audit_mismatches=0
        self._mode=None;self._continuous_start=None;self._last_availability=None;self.max_coverage=0
        self._cancel_ids=OrderedDict();self.cancel_obligations=0
        self.comparison_unknown_samples=0;self.unexplained_ws_unknown=0;self.unknown_reason_counts={}
        self._unknown_started=None;self.max_unknown_segment=0;self._availability_reason=None

    @staticmethod
    def _unknown_reason(reason):
        return reason if reason in ('book_unknown','book_identity_mismatch','book_level_missing','data_inconsistent',
            'remaining_unknown','awaiting_acceptance_confirmation','missing_comparison_context','no_subscriptions',
            'ws_unsynced','ws_stale','connection_gap','token_sync_gap','not_independent','not_subscribed',
            'history_capacity','observation_gap','audit_unavailable') else 'unexplained_unknown'

    def _note_unknown(self, reason):
        reason=self._unknown_reason(reason)
        self.unknown_reason_counts[reason]=self.unknown_reason_counts.get(reason,0)+1
        if reason=='unexplained_unknown':self.unexplained_ws_unknown+=1
        return reason

    @synchronized
    def set_mode(self, mode):
        if mode==self._mode:return
        if self._unknown_started is not None:
            self.max_unknown_segment=max(self.max_unknown_segment,self.clock()-self._unknown_started)
        self._unknown_started=None;self._availability_reason=None
        self._mode=mode;self._continuous_start=None;self._last_availability=None;self.coverage=0

    @synchronized
    def availability(self, available, *, reason=None):
        if self._mode is None:self.set_mode('shadow')
        if self._mode!='shadow':return
        now=self.clock()
        if self._last_availability is None or not 0<=now-self._last_availability<=10:
            self._continuous_start=now
        self._last_availability=now;self.coverage=now-self._continuous_start
        self.max_coverage=max(self.max_coverage,self.coverage)
        if available:
            if self._unknown_started is not None:self.max_unknown_segment=max(self.max_unknown_segment,now-self._unknown_started)
            self._unknown_started=None;self._availability_reason=None
        else:
            if self._unknown_started is None:self._unknown_started=now
            classified=self._unknown_reason(reason)
            if classified!=self._availability_reason:self._note_unknown(reason)
            self._availability_reason=classified
            self.max_unknown_segment=max(self.max_unknown_segment,now-self._unknown_started)

    @synchronized
    def audit_result(self, reason):
        if reason=='different':self.audit_mismatches+=1
        elif reason=='unavailable':
            self.not_comparable+=1;self.reasons['audit_unavailable']=self.reasons.get('audit_unavailable',0)+1

    @synchronized
    def disconnect(self, reason):
        if reason!='closed':
            self._disconnect_window.append(self.clock())
            if len(self._disconnect_window)==7 and self._disconnect_window[-1]-self._disconnect_window[0]<=3600:
                self.disconnect_hour_exceeded=True

    @synchronized
    def ws_excursion(self, *, transient=False):
        if transient:self.ws_transient_events+=1
        else:self.ws_early_events+=1

    @synchronized
    def abandon(self, *, book_pending, cancel_pending, reason):
        self.book_pending=max(0,self.book_pending-int(book_pending))
        self.cancel_pending=max(0,self.cancel_pending-int(cancel_pending))
        self.incomplete_observations+=1;self.not_comparable+=1
        reason=reason if reason in ('closed','mode_changed','context_capacity','pending_capacity') else 'observation_gap'
        self.reasons[reason]=self.reasons.get(reason,0)+1

    @synchronized
    def eligible_read(self, *, has_context):
        self.eligible_rest_reads+=1
        if not has_context:
            self.context_missing_reads+=1;self.not_comparable+=1
            self._note_unknown('missing_comparison_context')
            reason='missing_comparison_context'
            self.reasons[reason]=self.reasons.get(reason,0)+1

    @synchronized
    def sample(self, rest, ws):
        """One actual REST observation; unresolved differences have no verdict yet."""
        row=self.comparison(rest,ws)
        if ws['state']=='unknown':
            self.comparison_unknown_samples+=1
            self._note_unknown((ws.get('reason_codes') or [None])[0])
        self.samples+=1;self.not_comparable+=int(not row['comparable']);self.last=row
        self.reasons[row['reason']]=self.reasons.get(row['reason'],0)+1
        return row

    @synchronized
    def publish(self, row):self.last=dict(row)

    @synchronized
    def cancel_started(self, observation_id=None, *, obligation=False):
        fresh=observation_id is None or observation_id not in self._cancel_ids
        if not fresh and not obligation:return False
        if observation_id is not None:
            self._cancel_ids[observation_id]=None;self._cancel_ids.move_to_end(observation_id)
            while len(self._cancel_ids)>512:self._cancel_ids.popitem(last=False)
        self.rest_cancel_observations+=int(fresh);self.cancel_pending+=1;self.cancel_obligations+=1
        return fresh

    @synchronized
    def cancel_result(self, seconds, *, comparable=True):
        self.cancel_pending=max(0,self.cancel_pending-1)
        matched=seconds is not None and seconds<=2
        if not matched:self.ws_missed_cancels+=1;self.mismatches+=int(comparable)
        else:self.cancel_matches+=1;self._cancel_timing.append(float(seconds))
        return matched

    @synchronized
    def book_started(self):self.book_pending+=1

    @synchronized
    def book_result(self, matched, *, reason=None):
        self.book_pending=max(0,self.book_pending-1)
        if matched is None:
            self.not_comparable+=1;self.incomplete_observations+=1
            reason=reason if reason in ('not_subscribed','connection_gap','token_sync_gap','ws_unsynced',
                'history_capacity','not_independent') else 'observation_gap'
            self.reasons[reason]=self.reasons.get(reason,0)+1
        else:self.book_matches+=int(matched);self.book_mismatches+=int(not matched)

    @staticmethod
    def comparison(rest, ws):
        comparable=rest['state']!='unknown' and ws['state']!='unknown'
        consistent=comparable and rest['state']==ws['state']
        reason=('consistent' if consistent else 'decision_mismatch' if comparable else
            'ws_unknown' if ws['state']=='unknown' else 'rest_unknown')
        return dict(comparable=comparable,rest_can_cancel=rest['state']=='triggered',
            ws_can_cancel=ws['state']=='triggered',consistent=consistent,reason=reason)

    @synchronized
    def record(self, rest, ws):
        """Aggregate one completed comparison; runtime starts windows with sample()."""
        self.eligible_read(has_context=True);row=self.sample(rest,ws)
        self.availability(ws['state']!='unknown',reason=(ws.get('reason_codes') or [None])[0])
        if row['rest_can_cancel']:
            self.cancel_started();self.cancel_result(0 if row['ws_can_cancel'] else None,comparable=row['comparable'])
        elif row['ws_can_cancel']:self.ws_excursion()
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
        denominator=self.samples+self.context_missing_reads
        fraction=(self.comparison_unknown_samples+self.context_missing_reads)/denominator if denominator else None
        segment=max(self.max_unknown_segment,self.clock()-self._unknown_started if self._unknown_started is not None else 0)
        qualified=self.coverage>=7200 and self.rest_cancel_observations>=30 and self.samples>0
        hard_failure=bool(self.book_mismatches or self.audit_mismatches or self.ws_missed_cancels
            or self.unexplained_ws_unknown or segment>15 or self.disconnect_hour_exceeded)
        assessment=('FAIL' if hard_failure or qualified and fraction is not None and fraction>.02 else
            'UNKNOWN' if self._mode is None else 'PASS' if qualified and self._mode=='shadow' and
            not self.book_pending and not self.cancel_pending and not self.incomplete_observations else 'PENDING')
        return dict(samples=self.samples,mismatches=self.mismatches,not_comparable=self.not_comparable,
            book_matches=self.book_matches,book_mismatches=self.book_mismatches,book_pending=self.book_pending,
            rest_cancel_observations=self.rest_cancel_observations,cancel_pending=self.cancel_pending,
            cancel_obligations=self.cancel_obligations,
            eligible_rest_reads=self.eligible_rest_reads,context_missing_reads=self.context_missing_reads,
            incomplete_observations=self.incomplete_observations,
            ws_early_events=self.ws_early_events,ws_transient_events=self.ws_transient_events,
            audit_mismatches=self.audit_mismatches,
            cancel_matches=self.cancel_matches,ws_missed_cancels=self.ws_missed_cancels,
            cancel_timing=dict(self._distribution(self._cancel_timing),last_seconds=self._cancel_timing[-1] if self._cancel_timing else None),
            last=dict(self.last) if self.last else None,reasons=dict(self.reasons),coverage_seconds=self.coverage,
            max_continuous_coverage_seconds=self.max_coverage,
            comparison_unknown_samples=self.comparison_unknown_samples,unknown_fraction=fraction,
            unknown_sample_denominator=denominator,max_unknown_segment_seconds=segment,
            unexplained_ws_unknown=self.unexplained_ws_unknown,unknown_reason_counts=dict(self.unknown_reason_counts),
            disconnect_hour_exceeded=self.disconnect_hour_exceeded,assessment=assessment,
            rest_can_cancel_implies_ws_can_cancel=(None if not self.rest_cancel_observations else
                self.ws_missed_cancels==0 and self.cancel_pending==0),
            automatic_enable=False,arrival_to_update=self._distribution(self._updates),
            wake_to_decision=self._distribution(self._decisions))
