"""Explicit inactive manual routes composed over real current owners and SQLite."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import fcntl
import hashlib
import sqlite3
import threading
import time
from threading import Event
from types import SimpleNamespace

import pytest
from polymarket.models.clob.order_book import OrderBookLevel
from polymarket.models.clob.order_response import AcceptedOrder
from polymarket.models.clob.rewards import CurrentReward, CurrentRewardConfig

from open_trader import polymarket_lp_market_reader, polymarket_trading, prediction_arbitrage_store
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
from open_trader.polymarket_lp_market_calculation import CalculationPolicy
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_lp_order_registration_contract import _adapter, _SDKPublicClient, _Notifier, _open_order, WALLET, CONDITION_ID, TOKEN_ID

NOW = datetime(2026,10,16,1,tzinfo=UTC)
OTHER_CID='0x'+'3'*64
OTHER_TOKEN='0x'+'4'*64


def manual_runtime(tmp_path,monkeypatch,*,process='manual-P1'):
    clock=[NOW]
    class ClockMeta(type):
        def __instancecheck__(cls,value):return isinstance(value,datetime)
    class BusinessDateTime(datetime,metaclass=ClockMeta):
        @classmethod
        def now(cls,tz=None):return clock[0] if tz is not None else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(polymarket_lp_market_reader,'datetime',BusinessDateTime)
    monkeypatch.setattr(polymarket_trading,'datetime',BusinessDateTime)
    monkeypatch.setattr(prediction_arbitrage_store,'datetime',BusinessDateTime)
    class Public(_SDKPublicClient):
        bid,ask,book_reads=Decimal('.30'),Decimal('.32'),0
        lower_bids=()
        def get_market(self,*,id):
            base=super().get_market(id='market-1')
            if id=='market-1':return base
            assert id=='market-2'
            return base.model_copy(update={'id':id,'condition_id':OTHER_CID,
                'outcomes':base.outcomes.model_copy(update={'yes':base.outcomes.yes.model_copy(update={'token_id':OTHER_TOKEN})})})
        def list_markets(self,**kw):
            return [self.get_market(id=v) for v in ('market-1','market-2')]
        def get_order_book(self,*,token_id):
            self.book_reads+=1
            base=super().get_order_book(token_id=TOKEN_ID if token_id==OTHER_TOKEN else token_id)
            return base.model_copy(update={'condition_id':OTHER_CID if token_id==OTHER_TOKEN else CONDITION_ID,'token_id':token_id,'timestamp':clock[0],
                'bids':(OrderBookLevel(price=self.bid,size=Decimal('1000')),)+self.lower_bids,
                'asks':(OrderBookLevel(price=self.ask,size=Decimal('1000')),)})
        def list_market_rewards(self,*,condition_id,sponsored):
            assert condition_id in (CONDITION_ID,OTHER_CID)
            return (CurrentReward(condition_id=condition_id,rewards_max_spread=10,rewards_min_size=Decimal('1'),
                rewards_config=(CurrentRewardConfig(asset_address='0x2791bca1f2de4661ed88a30c99a7a9449aa84174',
                    start_date=NOW-timedelta(days=1),end_date=NOW+timedelta(days=1),
                    rate_per_day=Decimal('24')),)),)
    public=Public(NOW);adapter,sdk=_adapter(tmp_path,public_client=public)
    sdk.create_limit_order=lambda **kw:dict(kw,order_type='GTD' if kw['expiration'] is not None else 'GTC')
    def post(signed):
        number=max((int(o.id.removeprefix('manual-order-')) for o in sdk.orders if o.id.startswith('manual-order-')),default=0)+1
        sdk.posts.append(signed);oid='manual-order-'+str(max(number,len(sdk.posts)))
        sdk.orders+=(_open_order(oid,'BUY',price=str(signed['price']),original=str(signed['size'])).model_copy(
            update={'order_type':'GTD','expires_at':datetime.fromtimestamp(signed['expiration'],UTC),'created_at':clock[0],'token_id':signed['token_id'],
                    'condition_id':OTHER_CID if signed['token_id']==OTHER_TOKEN else CONDITION_ID}),)
        return AcceptedOrder(order_id=oid,status='live',making_amount=Decimal('0'),taking_amount=Decimal('0'),
            trade_ids=(),transactions_hashes=())
    sdk.post_order=post
    store=PredictionArbitrageStore(tmp_path)
    account=hashlib.sha256(WALLET.strip().casefold().encode()).hexdigest()
    if store.lp_load_current_control(account) is None:
        store.lp_save_current_control(dict(account_id=account,process_epoch='seed',request_id='paused',request_version=1,
            desired_running=False,requested_at=NOW,actor=None))
    store.lp_save_price_history(CONDITION_ID,TOKEN_ID,[],dict(state='known',amplitude=Decimal('.005'),
        checked_at=NOW,valid_until=NOW+timedelta(hours=24)))
    store.lp_save_price_history(OTHER_CID,OTHER_TOKEN,[],dict(state='known',amplitude=Decimal('.005'),
        checked_at=NOW,valid_until=NOW+timedelta(hours=24)))
    lp=PolymarketLPService(store,adapter,clock=lambda:clock[0])
    live=LPD2LiveExecution(store,adapter,account_id=WALLET,venue='polymarket',process_epoch=process,
        policy=CalculationPolicy(None),utc_now=lambda:clock[0])
    live.initialize(ValidatedStartAdmission(process,'offline-start-permit'))
    execution=PredictionExecutionService(store=store,monitor=object(),trading=adapter,notifier=_Notifier(),
        lock_path=tmp_path/'execution.lock',lp=lp,lp_d2=live)
    execution._breaker_open=False  # existing startup-clear fixture boundary
    return execution,live,sdk,public,clock


def entry(clock,**changes):
    return dict(market_id='market-1',condition_id=CONDITION_ID,token_id=TOKEN_ID,outcome='YES',
        price='.29',quantity='20',review_at=(clock[0]+timedelta(minutes=10)).isoformat(),
        idempotency_key='manual-key',estimated_target_quantity='999')|changes


def test_d2_manual_entry_uses_current_frame_and_explicit_terms(tmp_path,monkeypatch):
    execution,live,sdk,public,clock=manual_runtime(tmp_path,monkeypatch)
    try:
        initial=live.state.snapshot().current
        assert initial.effective_running is False and live.rounds.snapshot().round_number==0 and live.policy.budget is None
        with pytest.raises(ValueError,match='quantity_or_price_invalid'):
            execution.lp_submit_entry(entry(clock,idempotency_key='bad-price',price='0'))
        assert live.store.lp_manual_command(WALLET,'bad-price') is None and sdk.posts==[]
        result=execution.lp_submit_entry(entry(clock,idempotency_key='bad-outcome',outcome='NO'))
        assert result['state']=='rejected'
        assert live.store.lp_manual_command(WALLET,'bad-outcome') is None and sdk.posts==[]
        sdk.orders=(_open_order('foreign-account','BUY',price='.30',original='10').model_copy(
            update={'maker_address':'0x'+'f'*40}),)
        assert execution.lp_submit_entry(entry(clock,idempotency_key='foreign-account'))['state']=='rejected'
        assert live.store.lp_manual_command(WALLET,'foreign-account') is None and sdk.posts==[]
        sdk.orders=();sdk.positions=None
        assert execution.lp_submit_entry(entry(clock,idempotency_key='positions-unknown'))['state']=='rejected'
        assert live.store.lp_manual_command(WALLET,'positions-unknown') is None and sdk.posts==[]
        sdk.positions=()
        incident_execution=live.store.create_recovery_execution(dict(reason='real existing incident'),idempotency_key='incident')
        incident=live.store.open_incident(incident_execution['execution_id'],dict(reason='real existing incident'))
        result=execution.lp_submit_entry(entry(clock,idempotency_key='breaker'))
        assert result['state']=='locked' and result['reason']=='circuit_breaker_open'
        live.store.acknowledge_incident(incident,dict(note='cleared'))
        active=live.store.create_recovery_execution(dict(reason='real nonLP'),idempotency_key='nonLP')
        live.store.transition_execution(active['execution_id'],state='submitting',evidence={})
        assert execution.lp_submit_entry(entry(clock,idempotency_key='nonLP'))['reason']=='active_execution'
        live.store.transition_execution(active['execution_id'],state='complete',evidence={})
        with (tmp_path/'execution.lock').open('a+') as foreign:
            fcntl.flock(foreign.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            assert execution.lp_submit_entry(entry(clock,idempotency_key='locked'))['reason']=='execution_lock'
            fcntl.flock(foreign.fileno(),fcntl.LOCK_UN)
        sdk.orders=(_open_order('unrelated-buy','BUY',price='.40',original='10').model_copy(
            update={'condition_id':'0x'+'3'*64,'token_id':'0x'+'4'*64}),)
        balance=sdk.get_balance_allowance
        def exact_funds(**kw):
            balance(**kw)
            return SimpleNamespace(balance='9800000',allowances={sdk.environment.standard_exchange:'9800000'})
        sdk.get_balance_allowance=exact_funds
        before=(sdk.balance_reads,sdk.order_reads,sdk.position_reads,public.book_reads)
        request=entry(clock)
        result=execution.lp_submit_entry(request)
        if result['state'] != 'entry_open':
            observation = live.state.snapshot().observation
            print('actual current market evidence:', repr(observation.source_frame.sources.get(f'market:{CONDITION_ID}:{TOKEN_ID}')) if observation else 'no accepted frame')
        assert result['state']=='entry_open',result
        assert len(sdk.posts)==1
        sent,=sdk.posts
        assert sent['price']==Decimal('.29') and sent['size']==20 and sent['post_only'] is True
        assert sent['order_type']=='GTD' and sent['expiration']==int((clock[0]+timedelta(minutes=11)).timestamp())
        assert tuple(a-b for a,b in zip((sdk.balance_reads,sdk.order_reads,sdk.position_reads,public.book_reads),before))==(1,1,1,1)
        snapshot=live.state.snapshot()
        assert snapshot.current==initial and live.rounds.snapshot().round_number==0
        observation=snapshot.observation
        assert observation is not None, 'manual entry must accept its actual D2 frame'
        assert observation.source_frame.buy_reserved==4
        assert all(observation.source_frame.sources[k].checked_at==clock[0] for k in ('balance','open_orders','positions',f'market:{CONDITION_ID}:{TOKEN_ID}'))
        order=next(o for o in snapshot.orders if o.key[3]=='manual-order-1')
        assert order.original_size.value==20 and order.size_matched.value==0 and order.remaining.value==20
        group=live.store.lp_manual_group(WALLET,result['session_id'])
        assert group['entry_order_ids']==['manual-order-1'] and group['identity']['review_at']==request['review_at']
        opening,=snapshot.openings.values()
        assert opening.entry_order_ids==('manual-order-1',) and opening.review_at.value==clock[0]+timedelta(minutes=10)
        assert opening.economics.opening_loss_usd.state=='unknown' and opening.stop_loss_latched.value is False
        assert live.store.lp_active_sessions()==[] and live.store.lp_sessions()==[]
        reads=(sdk.balance_reads,sdk.order_reads,sdk.position_reads,public.book_reads)
        replay=execution.lp_submit_entry(entry(clock,price='.28',quantity='99'))
        assert replay==result and len(sdk.posts)==1
        assert reads==(sdk.balance_reads,sdk.order_reads,sdk.position_reads,public.book_reads)
    finally:
        assert live.close(timeout=3)
        live.trading.close()
    rebuilt,other,other_sdk,_,other_clock=manual_runtime(tmp_path,monkeypatch,process='manual-P2')
    try:
        assert rebuilt.lp_submit_entry(entry(other_clock,quantity='999'))==result
        assert other_sdk.posts==[] and other.state.snapshot().openings=={} and other.state.snapshot().orders==()
        assert other.rounds.snapshot().round_number==0
    finally:
        assert other.close(timeout=3)
        other.trading.close()


def test_d2_manual_preview_start_preserves_credential_and_replay_order(tmp_path,monkeypatch):
    execution,live,sdk,public,clock=manual_runtime(tmp_path/'custom',monkeypatch)
    try:
        preview=execution.lp_preview(entry(clock))
        assert preview['state']=='previewed',preview
        assert live.state.snapshot().observation is not None, 'preview uses accepted current frame, not legacy financial read'
        assert datetime.fromisoformat(preview['expires_at'])==clock[0]+timedelta(seconds=10)
        assert live.rounds.snapshot().round_number==0 and sdk.posts==[]
        public.bid=Decimal('.29');clock[0]+=timedelta(seconds=1)
        rejected=execution.lp_start(preview['preview_id'],'changed-bid')
        assert rejected['state']=='rejected' and rejected['reason']=='best_bid_changed'
        assert live.store.lp_preview(preview['preview_id'])['consumed_at'] is None and sdk.posts==[]
        public.bid=Decimal('.30')
        preview=execution.lp_preview(entry(clock))
        accepted=execution.lp_start(preview['preview_id'],'start-key')
        assert accepted['state']=='entry_open' and len(sdk.posts)==1
        assert sdk.posts[0]['price']==Decimal('.29') and sdk.posts[0]['size']==20
        assert live.store.lp_preview(preview['preview_id'])['consumed_at'] is not None
        clock[0]=datetime.fromisoformat(preview['expires_at'])
        before=(sdk.order_reads,public.book_reads)
        assert execution.lp_start(preview['preview_id'],'start-key')==accepted
        assert before==(sdk.order_reads,public.book_reads) and len(sdk.posts)==1
        expired=execution.lp_start(preview['preview_id'],'new-expired-key')
        assert expired['state']=='rejected' and expired['reason']=='preview_expired'
        assert execution.lp_start('missing','missing-key')['reason']=='preview_not_found'
        assert live.store.lp_manual_command(WALLET,'new-expired-key') is None
    finally:
        assert live.close(timeout=3);live.trading.close()
    execution,live,sdk,public,clock=manual_runtime(tmp_path/'candidate',monkeypatch)
    public.lower_bids=(OrderBookLevel(price=Decimal('.29'),size=Decimal('1000')),)
    try:
        candidate={k:v for k,v in entry(clock).items() if k in ('market_id','condition_id','token_id','outcome')}
        preview=execution.lp_candidate_preview(candidate)
        assert preview['state']=='previewed',preview
        assert Decimal(str(preview['request']['price']))==Decimal('.30')
        assert preview['request']['candidate_policy']=='best_bid_minimum'
        public.bid=Decimal('.29');clock[0]+=timedelta(seconds=1)
        rejected=execution.lp_start(preview['preview_id'],'candidate-changed')
        assert rejected['state']=='rejected' and rejected['reason']=='candidate_best_bid_changed'
        assert sdk.posts==[] and live.store.lp_preview(preview['preview_id'])['consumed_at'] is None
    finally:
        assert live.close(timeout=3);live.trading.close()
    execution,live,sdk,public,clock=manual_runtime(tmp_path/'race',monkeypatch)
    entered,release=Event(),Event();results=[];errors=[];worker=None
    try:
        preview=execution.lp_preview(entry(clock));post=sdk.post_order
        def held(signed):
            entered.set();assert release.wait(5),'competing starts SDK completion watchdog'
            return post(signed)
        sdk.post_order=held
        def start():
            try:results.append(execution.lp_start(preview['preview_id'],'race-first'))
            except BaseException as exc:errors.append(exc)
        worker=threading.Thread(target=start);worker.start();assert entered.wait(3)
        second=execution.lp_start(preview['preview_id'],'race-second')
        assert second['state'] in ('busy','rejected') and live.store.lp_manual_command(WALLET,'race-second') is None
        release.set();worker.join(3);assert not worker.is_alive() and errors==[]
        assert results[0]['state']=='entry_open' and len(sdk.posts)==1
        assert live.store.lp_preview(preview['preview_id'])['consumed_at'] is not None
    finally:
        release.set()
        if worker:worker.join(3)
        assert live.close(timeout=3);live.trading.close()
    execution,live,sdk,public,clock=manual_runtime(tmp_path/'claim-failure',monkeypatch)
    try:
        preview=execution.lp_preview(entry(clock));real_connect=sqlite3.connect;armed=[True]
        class FailedClaim(sqlite3.Connection):
            def execute(self,sql,*a,**kw):
                if sql=='COMMIT' and armed[0]:
                    armed[0]=False;raise sqlite3.OperationalError('actual preview claim commit failure')
                return super().execute(sql,*a,**kw)
        monkeypatch.setattr(sqlite3,'connect',lambda *a,**kw:real_connect(*a,**(kw|dict(factory=FailedClaim))))
        result=execution.lp_start(preview['preview_id'],'failed-claim')
        assert result['state']=='needs_attention' and sdk.posts==[]
        assert live.store.lp_preview(preview['preview_id'])['consumed_at'] is None
        assert live.store.lp_manual_command(WALLET,'failed-claim') is None and live.control.status().fatal is None
    finally:
        assert live.close(timeout=3);live.trading.close()


def test_d2_manual_named_augment_keeps_current_group_and_original_deadline(tmp_path,monkeypatch):
    execution,live,sdk,public,clock=manual_runtime(tmp_path,monkeypatch)
    try:
        first=execution.lp_submit_entry(entry(clock))
        second=execution.lp_submit_entry(entry(clock,market_id='market-2',condition_id=OTHER_CID,
            token_id=OTHER_TOKEN,idempotency_key='other-entry'))
        assert first['state']==second['state']=='entry_open',(first,second)
        group=first['session_id'];other=second['session_id']
        other_opening=next(v for v in live.state.snapshot().openings.values() if v.scope[2]==OTHER_TOKEN)
        original=live.store.lp_manual_group(WALLET,group)['identity']
        payload=dict(session_id=group,quantity='20',price='.28',idempotency_key='augment-one')
        result=execution.lp_submit_augment(payload)
        assert result['state']=='entry_open',result
        assert result['entry_order_id']==first['entry_order_id'] and result['augment_order_id']=='manual-order-3'
        assert len(sdk.posts)==3 and sdk.posts[-1]['price']==Decimal('.28') and sdk.posts[-1]['size']==20
        assert sdk.posts[-1]['expiration']==original['expiration']
        current=live.store.lp_manual_group(WALLET,group)
        assert current['augment_order_ids']==['manual-order-3'] and current['identity']==original
        opening=next(v for v in live.state.snapshot().openings.values() if v.scope[2]==TOKEN_ID)
        assert opening.entry_order_ids==('manual-order-1','manual-order-3')
        assert opening.review_at.value==datetime.fromisoformat(original['review_at'])
        assert live.state.snapshot().openings[other_opening.scope]==other_opening
        before=(sdk.order_reads,len(sdk.posts))
        assert execution.lp_submit_augment(payload|dict(price='.27',quantity='999'))==result
        assert (sdk.order_reads,len(sdk.posts))==before
        assert execution.lp_submit_augment(payload|dict(session_id='missing',idempotency_key='missing'))['reason']=='session_not_found'
        assert execution.lp_submit_augment(payload|dict(price='.28',idempotency_key='same-level'))['reason']=='price_level_active'
        assert execution.lp_submit_augment(payload|dict(price='.31',idempotency_key='above-bid'))['reason']=='price_above_best_bid'
        preview=execution.lp_augment_preview(dict(session_id=group,quantity='20',price='.27'))
        assert preview['state']=='previewed',preview
        assert execution.lp_augment(other,preview['preview_id'],'wrong-group')['state']=='rejected'
        added=execution.lp_augment(group,preview['preview_id'],'two-phase')
        assert added['state']=='entry_open' and len(sdk.posts)==4,added
        assert added['entry_order_id']==first['entry_order_id'] and added['augment_order_id']=='manual-order-4'
        assert sdk.posts[-1]['price']==Decimal('.27') and sdk.posts[-1]['expiration']==original['expiration']
        # Default price is the original entry price; its live level rejects.
        assert execution.lp_submit_augment(dict(session_id=group,quantity='20',idempotency_key='default-live'))['reason']=='price_level_active'
        sdk.orders=tuple(o for o in sdk.orders if o.id!='manual-order-1')
        default=execution.lp_submit_augment(dict(session_id=group,quantity='20',idempotency_key='default-empty'))
        assert default['state']=='entry_open' and sdk.posts[-1]['price']==Decimal('.29'),default
        # Named gates precede replay, unlike entry/start.
        incident_execution=live.store.create_recovery_execution(dict(reason='incident'),idempotency_key='incident')
        incident=live.store.open_incident(incident_execution['execution_id'],dict(reason='incident'))
        assert execution.lp_submit_augment(payload)['reason']=='circuit_breaker_open'
        live.store.acknowledge_incident(incident,dict(note='clear'))
        active=live.store.create_recovery_execution(dict(reason='nonLP'),idempotency_key='nonLP')
        live.store.transition_execution(active['execution_id'],state='submitting',evidence={})
        assert execution.lp_submit_augment(payload)['reason']=='active_execution'
        live.store.transition_execution(active['execution_id'],state='complete',evidence={})
        assert live.rounds.snapshot().round_number==0 and sdk.market_orders==[]
    finally:
        assert live.close(timeout=3);live.trading.close()


def test_d2_manual_restart_binding_uses_current_api_not_saved_economics(tmp_path,monkeypatch):
    execution,live,sdk,public,clock=manual_runtime(tmp_path,monkeypatch)
    first=execution.lp_submit_entry(entry(clock))
    other=execution.lp_submit_entry(entry(clock,market_id='market-2',condition_id=OTHER_CID,
        token_id=OTHER_TOKEN,idempotency_key='other-entry'))
    assert first['state']==other['state']=='entry_open'
    saved_orders=sdk.orders;group=first['session_id']
    with sqlite3.connect(live.store.path) as con:
        con.execute("UPDATE lp_sessions SET payload=json_set(payload,'$.response.buy_cost','900',"
            "'$.response.fees','99','$.response.stop_loss_latched',json('true')) WHERE session_id=?",(group,))
    assert live.close(timeout=3);live.trading.close()
    execution,live,sdk,public,clock=manual_runtime(tmp_path,monkeypatch,process='manual-restart')
    try:
        # Response replay grants no current ownership or historical money.
        replay=execution.lp_submit_entry(entry(clock))
        assert replay['buy_cost']=='900' and replay['stop_loss_latched'] is True
        assert sdk.posts==[] and live.state.snapshot().openings=={}
        sdk.orders=()
        absent=execution.lp_status(group)
        assert absent['state']=='needs_attention' and live.state.snapshot().openings=={},absent
        sdk.orders=tuple(o.model_copy(update={'maker_address':'0x'+'f'*40}) for o in saved_orders)
        assert execution.lp_status(group)['state']=='needs_attention' and live.state.snapshot().openings=={}
        sdk.orders=saved_orders;sdk.positions=None
        assert execution.lp_status(group)['state']=='needs_attention' and live.state.snapshot().openings=={}
        sdk.positions=()
        sdk.orders=tuple(o.model_copy(update={'price':Decimal('.26')}) if o.id=='manual-order-1' else o for o in saved_orders)
        assert execution.lp_status(group)['state']=='needs_attention' and live.state.snapshot().openings=={}
        sdk.orders=saved_orders
        status=execution.lp_status(group)
        if status['state']!='entry_open':
            print('actual canonical terms',[(o.key,o.side,o.order_type.value,o.expiration.value,o.price.value,o.original_size.value,o.source==live.state.snapshot().observation.order_view.orders_source) for o in live.state.snapshot().orders])
        assert status['state']=='entry_open',status
        snapshot=live.state.snapshot();opening,=snapshot.openings.values()
        assert opening.scope[3].startswith('manual-restart:manual:')
        assert opening.entry_order_ids==('manual-order-1',)
        assert opening.review_at.value==NOW+timedelta(minutes=10)
        assert opening.economics.opening_loss_usd.state=='unknown' and opening.stop_loss_latched.value is None
        assert opening.buy_filled_quantity.state=='unknown'
        assert snapshot.orders and all(o.source.source_id for o in snapshot.orders)
        assert snapshot.own_fills=={} and sdk.posts==[]
        assert status.get('buy_cost') != '900' and status.get('fees') != '99'
        assert execution.lp_submit_augment(dict(session_id=group,quantity='20',price='.28',idempotency_key='restart-add'))['state']=='entry_open'
        assert len(sdk.posts)==1 and sdk.posts[0]['expiration']==int((NOW+timedelta(minutes=11)).timestamp())
        assert live.rounds.snapshot().round_number==0
        # Named current status binds only requested identities, never the other group implicitly.
        assert len(live.state.snapshot().openings)==1
        clock[0]=NOW+timedelta(minutes=10)
        live.run_once()
        live.run_protection_once()
        current=live.state.snapshot().openings[opening.scope]
        assert current.stop_requested and current.phase=='review'
        assert current.review_at.value==NOW+timedelta(minutes=10) and current.stop_loss_latched.value is not True
        assert sdk.market_orders==[]
    finally:
        assert live.close(timeout=3);live.trading.close()


def test_d2_manual_post_uncertainty_and_final_admission_keep_real_identity(tmp_path,monkeypatch):
    from tests.test_lp_d2_integration import settle_control
    from dataclasses import fields
    from open_trader.polymarket_lp_current_state import BuyExecutionUncertainty
    for variant in ('timeout','error','missing_quantity','signing','generation','closed','observation'):
        execution,live,sdk,public,clock=manual_runtime(tmp_path/variant,monkeypatch,process='manual-'+variant)
        entered,release,signed=Event(),Event(),Event();worker=None;results=[];errors=[]
        try:
            assert live.request_running(True).status=='accepted';settle_control(live)
            live.run_once()
            assert live.rounds.snapshot().round_number==1
            assert live.request_running(False).status=='accepted';settle_control(live)
            sampled=live.rounds.snapshot();original=entry(clock,idempotency_key=variant)
            sign=sdk.create_limit_order
            def create(**kw):
                if variant=='signing':raise RuntimeError('actual signing failure before POST')
                result=sign(**kw);signed.set();return result
            sdk.create_limit_order=create
            post=sdk.post_order;posted=[]
            def uncertain(order):
                posted.append(order)
                if variant=='timeout':raise TimeoutError('actual SDK POST timeout')
                if variant=='error':raise RuntimeError('actual SDK POST error')
                if variant=='missing_quantity':return dict(order_id='possible-manual-order',status='LIVE',accepted=True)
                return post(order)
            sdk.post_order=uncertain
            real_connect=sqlite3.connect
            class FinalSQL(sqlite3.Connection):
                def execute(self,sql,*a,**kw):
                    cursor=super().execute(sql,*a,**kw)
                    if (variant in ('generation','closed','observation') and signed.is_set()
                            and threading.current_thread().name=='lp-d2-manual-command'
                            and sql.startswith('SELECT * FROM executions WHERE state NOT IN') and not entered.is_set()):
                        entered.set();assert release.wait(6),'last actual manual safety SQL watchdog'
                    return cursor
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3,'connect',lambda *a,**kw:real_connect(*a,**(kw|dict(factory=FinalSQL))))
                def submit():
                    try:results.append(execution.lp_submit_entry(original))
                    except BaseException as exc:errors.append(exc)
                worker=threading.Thread(target=submit);worker.start()
                if variant in ('generation','closed','observation'):
                    assert entered.wait(3)
                    before=live.state.snapshot().observation
                    if variant=='generation':
                        live.request_running(True);settle_control(live)
                        assert live.state.snapshot().current.cycle_generation!=sampled.cycle_generation
                    elif variant=='closed':assert live.close(timeout=.01) is False
                    else:
                        live.run_once()
                        assert live.state.snapshot().observation.observation_id!=before.observation_id
                    release.set()
                worker.join(4);assert not worker.is_alive() and errors==[]
            result,=results
            uncertain_event=live.status()['last_buy_uncertainty']
            if variant in ('signing','generation','closed','observation'):
                assert result['submit_status']=='not_sent' and posted==[] and uncertain_event is None
                continue
            assert result['state']=='needs_attention' and result['submit_status']=='unknown'
            assert len(posted)==1 and len(fields(BuyExecutionUncertainty))==21
            claim=live.store.lp_manual_command(WALLET,variant);command=claim['command']
            assert uncertain_event.request_id==command['request_id']
            assert uncertain_event.batch_id=='manual:'+command['request_id']
            assert uncertain_event.round_number==sampled.round_number==1
            assert uncertain_event.cycle_generation==sampled.cycle_generation
            assert uncertain_event.observation_id==live.state.snapshot().observation.observation_id
            assert (uncertain_event.account_id,uncertain_event.process_epoch,uncertain_event.venue)==(WALLET,'manual-'+variant,'polymarket')
            assert (uncertain_event.condition_id,uncertain_event.token_id)==(CONDITION_ID,TOKEN_ID)
            assert (uncertain_event.price,uncertain_event.quantity,uncertain_event.order_type)==(Decimal('.29'),Decimal('20'),'GTD')
            assert uncertain_event.expiration==int((NOW+timedelta(minutes=11)).timestamp())
            assert uncertain_event.post_only is True and uncertain_event.send_options==()
            assert uncertain_event.native_started_at==uncertain_event.observed_at==NOW
            assert uncertain_event.reason=={'timeout':'unknown_timeout','error':'unknown_error','missing_quantity':'invalid_receipt'}[variant]
            assert uncertain_event.venue_order_id==('possible-manual-order' if variant=='missing_quantity' else None)
            snapshot=live.state.snapshot()
            assert all(f.state=='unknown' and f.value is None for f in (snapshot.buy_reserved,snapshot.inventory_cost,snapshot.current_exposure,snapshot.new_capacity))
            before=(snapshot.trade_version,len(posted),sdk.order_reads)
            assert execution.lp_submit_entry(original|dict(quantity='999'))==result
            assert (live.state.snapshot().trade_version,len(posted),sdk.order_reads)==before
            assert live.rounds.snapshot()==sampled
            clock[0]+=timedelta(seconds=1);sdk.positions=None;live.run_once()
            assert live.state.snapshot().current_exposure.state=='unknown'
            clock[0]+=timedelta(seconds=1);sdk.positions=();live.run_once()
            assert live.state.snapshot().buy_reserved.value==live.state.snapshot().inventory_cost.value==live.state.snapshot().current_exposure.value==0
            assert live.control.status().fatal is None and len(posted)==1
        finally:
            release.set()
            if worker:worker.join(3)
            assert live.close(timeout=3);live.trading.close()


def test_d2_manual_native_and_identity_io_remain_owned_until_actual_end(tmp_path,monkeypatch):
    from tests.test_lp_d2_integration import settle_control
    for variant in ('finished','finish_failed'):
        execution,live,sdk,public,clock=manual_runtime(tmp_path/variant,monkeypatch)
        sdk_entered,sdk_release,returned,sql_entered,sql_release=Event(),Event(),Event(),Event(),Event()
        results=[];errors=[];worker=None;real_connect=sqlite3.connect
        post=sdk.post_order
        def held_post(order):
            sdk_entered.set();assert sdk_release.wait(6),'actual manual SDK watchdog'
            result=post(order);returned.set();return result
        sdk.post_order=held_post
        class HeldFinish(sqlite3.Connection):
            def execute(self,sql,*a,**kw):
                if sql=='COMMIT' and returned.is_set() and threading.current_thread().name=='lp-d2-manual-command':
                    sql_entered.set();assert sql_release.wait(6),'actual manual finish COMMIT watchdog'
                    if variant=='finish_failed':raise sqlite3.OperationalError('actual reliable result commit failure')
                return super().execute(sql,*a,**kw)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3,'connect',lambda *a,**kw:real_connect(*a,**(kw|dict(factory=HeldFinish))))
                def submit():
                    try:results.append(execution.lp_submit_entry(entry(clock)))
                    except BaseException as exc:errors.append(exc)
                worker=threading.Thread(target=submit);worker.start();assert sdk_entered.wait(3)
                assert live.status()['native_inflight']==1 and worker.is_alive()
                # Actual paused request cannot release native ownership or global exclusion.
                assert live.request_running(True).status=='accepted';settle_control(live)
                assert live.request_running(False).status=='accepted';settle_control(live)
                competing=execution.lp_submit_entry(entry(clock,market_id='market-2',condition_id=OTHER_CID,
                    token_id=OTHER_TOKEN,idempotency_key='competing'))
                assert competing['state']=='busy' and len(sdk.posts)==0
                sdk_release.set();assert sql_entered.wait(3)
                assert worker.is_alive() and len(sdk.posts)==1 and live.status()['native_inflight']==0
                # SQL ownership remains real but does not become a fictitious SDK call or hold current reads.
                assert live.state.snapshot().orders[0].key[3]=='manual-order-1'
                before=sdk.order_reads
                live.run_once()
                assert sdk.order_reads==before+1
                with (tmp_path/variant/'execution.lock').open('a+') as foreign:
                    with pytest.raises(BlockingIOError):fcntl.flock(foreign.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                assert execution.lp_submit_entry(entry(clock,idempotency_key='competing-finish'))['state']=='busy'
                assert live.close(timeout=.01) is False and worker.is_alive()
                sql_release.set();worker.join(3);assert not worker.is_alive() and errors==[]
            result,=results
            if variant=='finished':assert result['state']=='entry_open'
            else:
                assert result['state']=='needs_attention' and result['reason']=='manual_result_unknown'
                durable=live.store.lp_manual_command(WALLET,'manual-key')
                assert durable['outcome']=='unknown' and durable['venue_order_id'] is None
                assert durable['response']['submit_status']=='unknown'
            assert live.state.snapshot().orders[0].key[3]=='manual-order-1'
            assert live.control.status().fatal is None and len(sdk.posts)==1
            with (tmp_path/variant/'execution.lock').open('a+') as foreign:
                fcntl.flock(foreign.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                fcntl.flock(foreign.fileno(),fcntl.LOCK_UN)
            assert live.close(timeout=3)
        finally:
            sdk_release.set();sql_release.set()
            if worker:worker.join(3)
            assert live.close(timeout=3);live.trading.close()


def test_d2_explicit_paused_cancel_uses_exact_current_buy_sell_targets(tmp_path,monkeypatch):
    execution,live,sdk,public,clock=manual_runtime(tmp_path,monkeypatch)
    entered,release=Event(),Event();worker=None;results=[];errors=[]
    try:
        with pytest.raises(ValueError,match='confirm'):execution.lp_cancel_orders(dict(order_ids=['buy']))
        with pytest.raises(ValueError,match='exactly one'):execution.lp_cancel_orders(dict(confirm=True,order_ids=['buy'],scope='all'))
        sdk.orders=(_open_order('buy','BUY',price='.29',original='20',matched='3'),
            _open_order('sell','SELL',price='.32',original='10'),
            _open_order('not-displayed','BUY',price='.28',original='20'),
            _open_order('terminal','BUY',price='.29',original='20',matched='20',status='FILLED'))
        # Actual dashboard display subset; scope-all never expands to the whole account.
        execution._lp_dashboard_cache=dict(lp_orders_today=[dict(order_id='buy'),dict(order_id='sell'),dict(order_id='missing')])
        incident_execution=live.store.create_recovery_execution(dict(reason='incident'),idempotency_key='incident')
        live.store.open_incident(incident_execution['execution_id'],dict(reason='incident'))
        active=live.store.create_recovery_execution(dict(reason='nonLP'),idempotency_key='nonLP')
        live.store.transition_execution(active['execution_id'],state='submitting',evidence={})
        cancel=sdk.cancel_orders
        def held(**kw):
            entered.set();assert release.wait(6),'paused native cancel completion watchdog'
            return cancel(**kw)
        sdk.cancel_orders=held
        with (tmp_path/'execution.lock').open('a+') as foreign:
            fcntl.flock(foreign.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            def send():
                try:results.append(execution.lp_cancel_orders(dict(confirm=True,scope='all')))
                except BaseException as exc:errors.append(exc)
            worker=threading.Thread(target=send);worker.start();assert entered.wait(3)
            assert live.status()['native_inflight']==1, 'real dashboard cancel owns the shared native capacity'
            duplicate=execution.lp_cancel_orders(dict(confirm=True,order_ids=['buy']))
            assert duplicate['canceled']==[] and duplicate['skipped']==[dict(order_id='buy',reason='cancel_in_progress')]
            assert live.close(timeout=.01) is False
            release.set();worker.join(3);assert not worker.is_alive() and errors==[]
            fcntl.flock(foreign.fileno(),fcntl.LOCK_UN)
        result,=results
        assert result==dict(requested=3,canceled=['buy','sell'],not_canceled={},skipped=[dict(order_id='missing',reason='unknown_order')])
        assert sdk.cancels==[('buy','sell')]
        snapshot=live.state.snapshot()
        assert snapshot.current.effective_running is False and live.rounds.snapshot().round_number==0
        assert snapshot.openings=={} and snapshot.sells=={} and snapshot.own_fills=={}
        for oid in ('buy','sell'):
            order=next(o for o in snapshot.orders if o.key[3]==oid)
            assert order.terminal_proof.status=='CANCELED' and order.terminal_proof.identity_verified
            assert order.terminal_proof.matched.state=='unknown' and order.remaining.state=='unknown'
        assert execution.lp_cancel_orders(dict(confirm=True,order_ids=['not-displayed']))['not_canceled']=={'not-displayed':'program_stopping'}
    finally:
        release.set()
        if worker:worker.join(3)
        assert live.close(timeout=3);live.trading.close()
    # Fresh authoritative terminal quantities refine an ID-only cancel without invented final0.
    execution,live,sdk,public,clock=manual_runtime(tmp_path/'terminal',monkeypatch)
    try:
        sdk.orders=(_open_order('live-buy','BUY',price='.29',original='20',matched='3'),
            _open_order('live-sell','SELL',price='.32',original='10'))
        result=execution.lp_cancel_orders(dict(confirm=True,condition_id=CONDITION_ID))
        assert result['canceled']==['live-buy','live-sell'] and len(sdk.cancels)==1
        clock[0]+=timedelta(seconds=1)
        sdk.orders=(_open_order('live-buy','BUY',price='.29',original='20',matched='4',status='CANCELED'),
            _open_order('live-sell','SELL',price='.32',original='10',matched='0',status='CANCELED'))
        live.run_once()
        final={o.key[3]:o for o in live.state.snapshot().orders}
        assert final['live-buy'].terminal_proof.matched.value==4 and final['live-sell'].terminal_proof.matched.value==0
        assert live.state.snapshot().own_fills=={}
        assert execution.lp_cancel_orders(dict(confirm=True,order_ids=['terminal-missing','live-buy']))['canceled']==[]
    finally:
        assert live.close(timeout=3);live.trading.close()


def test_d2_named_stop_projects_review_without_forced_liquidation(tmp_path,monkeypatch):
    execution,live,sdk,public,clock=manual_runtime(tmp_path,monkeypatch)
    try:
        first=execution.lp_submit_entry(entry(clock))
        second=execution.lp_submit_entry(entry(clock,market_id='market-2',condition_id=OTHER_CID,
            token_id=OTHER_TOKEN,idempotency_key='other-entry'))
        assert first['state']==second['state']=='entry_open'
        group=first['session_id'];other=second['session_id']
        assert execution.lp_stop()['reason']=='session_ambiguous'
        assert execution.lp_stop('missing')==dict(state='none',session_id=None)
        before=live.state.snapshot();one=next(o for o in before.openings.values() if o.scope[2]==TOKEN_ID)
        two=next(o for o in before.openings.values() if o.scope[2]==OTHER_TOKEN)
        incident_execution=live.store.create_recovery_execution(dict(reason='incident'),idempotency_key='incident')
        incident=live.store.open_incident(incident_execution['execution_id'],dict(reason='incident'))
        assert execution.lp_stop(group)['state']=='locked' and sdk.cancels==[]
        assert live.state.snapshot().openings[one.scope]==one
        live.store.acknowledge_incident(incident,dict(note='clear'))
        clock[0]+=timedelta(seconds=1)
        sdk.orders+=(_open_order('manual-sell','SELL',price='.32',original='5').model_copy(update={'created_at':clock[0]}),)
        live.run_once()
        result=execution.lp_stop(group)
        assert result['state']=='review' and result['stop_requested'] is True,result
        current=live.state.snapshot()
        assert current.openings[two.scope]==two
        stopped=current.openings[one.scope]
        assert stopped.phase=='review' and stopped.review_status=='awaiting_reconciliation' and stopped.stop_requested
        assert stopped.review_at==one.review_at and stopped.stop_loss_latched==one.stop_loss_latched
        assert stopped.economics.opening_loss_usd.state=='unknown'
        assert sdk.cancels==[('manual-order-1','manual-sell')]
        assert len(sdk.posts)==2 and sdk.market_orders==[] and live.rounds.snapshot().round_number==0
        assert all(next(o for o in current.orders if o.key[3]==oid).terminal_proof.matched.state=='unknown'
            for oid in ('manual-order-1','manual-sell'))
        status=execution.lp_status(group)
        assert status['state']=='review' and status['stop_requested'] is True
        assert status['review_at']==(NOW+timedelta(minutes=10)).isoformat()
        assert execution.lp_status(other)['state']=='entry_open'
        assert execution.lp_submit_augment(dict(session_id=group,quantity='20',price='.28',idempotency_key='stopped'))['reason']=='session_review_exit'
        assert len(sdk.posts)==2 and sdk.market_orders==[]
    finally:
        assert live.close(timeout=3);live.trading.close()


def test_d2_manual_deadlines_survive_waiting_admission_io(tmp_path,monkeypatch):
    for variant in ('claim_expired_equal','claim_expired_later','signing_book_stale',
                    'final_sql_book_stale','final_sql_gtd_minimum','claimed_preview_expired','fresh'):
        execution,live,sdk,public,clock=manual_runtime(tmp_path/variant,monkeypatch)
        entered,release,signed=Event(),Event(),Event();worker=None;results=[];errors=[]
        try:
            preview=None
            request=entry(clock,idempotency_key=variant)
            if variant.startswith('claim_expired') or variant=='claimed_preview_expired':
                preview=execution.lp_preview(request)
                assert preview['state']=='previewed',preview
                clock[0]=NOW+timedelta(seconds=9)
            if variant=='final_sql_gtd_minimum':
                request['review_at']=(NOW+timedelta(seconds=121)).isoformat()
            sign=sdk.create_limit_order
            def create(**kw):
                result=sign(**kw);signed.set()
                if variant in ('signing_book_stale','claimed_preview_expired','fresh'):
                    entered.set();assert release.wait(6),'actual signing completion watchdog'
                return result
            sdk.create_limit_order=create
            real_connect=sqlite3.connect
            class WaitingSQL(sqlite3.Connection):
                def execute(self,sql,*a,**kw):
                    cursor=super().execute(sql,*a,**kw)
                    if threading.current_thread().name=='lp-d2-manual-command' and not entered.is_set():
                        claim=(variant.startswith('claim_expired') and self.in_transaction
                            and sql.startswith('SELECT * FROM previews WHERE preview_id='))
                        final=(variant in ('final_sql_book_stale','final_sql_gtd_minimum') and signed.is_set()
                            and sql.startswith('SELECT * FROM executions WHERE state NOT IN'))
                        if claim or final:
                            entered.set();assert release.wait(6),'actual SQLite admission completion watchdog'
                    return cursor
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3,'connect',lambda *a,**kw:real_connect(*a,**(kw|dict(factory=WaitingSQL))))
                def submit():
                    try:
                        results.append(execution.lp_start(preview['preview_id'],variant) if preview else execution.lp_submit_entry(request))
                    except BaseException as exc:errors.append(exc)
                worker=threading.Thread(target=submit);worker.start();assert entered.wait(3),variant
                original=live.state.snapshot().observation
                if variant in ('claim_expired_equal','claimed_preview_expired','fresh'):clock[0]=NOW+timedelta(seconds=10)
                elif variant=='final_sql_gtd_minimum':clock[0]=NOW+timedelta(seconds=2)
                else:clock[0]=NOW+timedelta(seconds=11)
                release.set();worker.join(4);assert not worker.is_alive() and errors==[],errors
            result,=results
            assert live.state.snapshot().observation is original
            assert live.status()['last_buy_uncertainty'] is None
            claim=live.store.lp_manual_command(WALLET,variant)
            if variant.startswith('claim_expired'):
                assert result['state']=='rejected' and result['reason']=='preview_expired',result
                assert claim is None and live.store.lp_preview(preview['preview_id'])['consumed_at'] is None
                assert sdk.posts==[] and live.state.snapshot().openings=={}
            elif variant=='fresh':
                assert result['state']=='entry_open' and result['submit_status']=='accepted',result
                assert len(sdk.posts)==1
                assert sdk.posts[0]['expiration']==int((NOW+timedelta(minutes=11)).timestamp())
                assert original.source_frame.sources[f'market:{CONDITION_ID}:{TOKEN_ID}'].checked_at==NOW
            else:
                assert result['state']=='rejected' and result['submit_status']=='not_sent',result
                assert sdk.posts==[] and live.state.snapshot().openings=={}
                assert claim['outcome']=='not_sent' and claim['command']['idempotency_key']==variant
                assert claim['command']['price']=='0.29' and claim['command']['quantity']=='20'
                assert execution.lp_submit_entry(request|dict(price='.28'))==result
                assert sdk.posts==[]
                if preview:
                    assert live.store.lp_preview(preview['preview_id'])['consumed_at'] is not None
                    assert execution.lp_start(preview['preview_id'],variant)==result
                    assert execution.lp_start(preview['preview_id'],variant+'-new')['reason']=='preview_expired'
        finally:
            release.set()
            if worker:worker.join(3)
            assert live.close(timeout=3);live.trading.close()


def test_d2_named_augment_rechecks_current_group_after_wait(tmp_path,monkeypatch):
    for variant in ('scoring_review','unchanged'):
        execution,previous,sdk,public,clock=manual_runtime(tmp_path/variant,monkeypatch)
        assert previous.close(timeout=3)
        monotonic=[0.0]
        live=LPD2LiveExecution(previous.store,previous.trading,account_id=WALLET,venue='polymarket',
            process_epoch='scoring-'+variant,policy=CalculationPolicy(None),clock=lambda:monotonic[0],utc_now=lambda:clock[0])
        live.initialize(ValidatedStartAdmission('scoring-'+variant,'offline-start-permit'))
        execution=PredictionExecutionService(store=live.store,monitor=object(),trading=live.trading,notifier=_Notifier(),
            lock_path=tmp_path/variant/'execution.lock',lp=PolymarketLPService(live.store,live.trading,clock=lambda:clock[0]),lp_d2=live)
        execution._breaker_open=False  # existing startup-clear fixture boundary
        entered,release,signed=Event(),Event(),Event();worker=None;results=[];errors=[]
        try:
            first=execution.lp_submit_entry(entry(clock,price='.30'))
            second=execution.lp_submit_entry(entry(clock,market_id='market-2',condition_id=OTHER_CID,
                token_id=OTHER_TOKEN,price='.30',idempotency_key='other-entry'))
            assert first['state']==second['state']=='entry_open',(first,second)
            scoring_reads=[]
            def scoring(*,order_id):
                scoring_reads.append(order_id)
                return order_id!=first['entry_order_id']
            sdk.get_order_scoring=scoring
            live.run_once();live.run_protection_once()
            before=live.state.snapshot()
            original=next(v for v in before.openings.values() if v.scope[2]==TOKEN_ID)
            other=next(v for v in before.openings.values() if v.scope[2]==OTHER_TOKEN)
            assert original.phase=='opening' and original.scoring.scoring.value is False
            assert original.scoring.lost_since_monotonic==0 and sdk.cancels==[]
            assert set(scoring_reads)=={first['entry_order_id'],second['entry_order_id']}
            # Separate injected monotonic and UTC clocks keep the real false interval
            # due while its accepted evidence and original book remain fresh.
            monotonic[0]=60;clock[0]=NOW+timedelta(seconds=1)
            sign=sdk.create_limit_order
            def create(**kw):
                result=sign(**kw);signed.set();return result
            sdk.create_limit_order=create
            real_connect=sqlite3.connect
            class FinalSQL(sqlite3.Connection):
                def execute(self,sql,*a,**kw):
                    cursor=super().execute(sql,*a,**kw)
                    if signed.is_set() and threading.current_thread().name=='lp-d2-manual-command' and not entered.is_set() and sql.startswith('SELECT * FROM executions WHERE state NOT IN'):
                        entered.set();assert release.wait(6),'augment final SQL completion watchdog'
                    return cursor
            payload=dict(session_id=first['session_id'],quantity='20',price='.28',idempotency_key='waiting-augment')
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3,'connect',lambda *a,**kw:real_connect(*a,**(kw|dict(factory=FinalSQL))))
                def submit():
                    try:results.append(execution.lp_submit_augment(payload))
                    except BaseException as exc:errors.append(exc)
                worker=threading.Thread(target=submit);worker.start();assert entered.wait(3),(results,errors)
                captured=live.state.snapshot()
                if variant=='scoring_review':
                    live.run_protection_once()
                    changed=live.state.snapshot()
                    assert changed.observation is captured.observation and changed.trade_version==captured.trade_version
                    assert tuple((o.key,o.terms_version) for o in changed.orders)==tuple((o.key,o.terms_version) for o in captured.orders)
                    reviewed=changed.openings[original.scope]
                    assert reviewed.phase=='review' and reviewed.stop_requested is True
                    assert reviewed.review_status=='scoring_continuously_lost' and reviewed.stop_loss_latched==original.stop_loss_latched
                    assert reviewed.review_at.value==clock[0] and reviewed.scope_version>original.scope_version
                    assert changed.openings[other.scope].phase==other.phase and changed.openings[other.scope].review_at==other.review_at
                    assert changed.openings[other.scope].stop_loss_latched==other.stop_loss_latched
                    assert sdk.cancels==[]  # exclusive manual lease blocks native protection dispatch
                release.set();worker.join(4);assert not worker.is_alive() and errors==[],errors
            result,=results
            if variant=='scoring_review':
                assert result['submit_status']=='not_sent' and result['state']=='rejected',result
                assert len(sdk.posts)==2 and len(live.state.snapshot().orders)==2
                assert live.status()['last_buy_uncertainty'] is None
                assert live.store.lp_manual_command(WALLET,'waiting-augment',session_id=first['session_id'])['outcome']=='not_sent'
                assert execution.lp_submit_augment(payload|dict(idempotency_key='after-review'))['reason']=='session_review_exit'
                assert live.state.snapshot().openings[original.scope]==reviewed
            else:
                assert result['state']=='entry_open' and result['submit_status']=='accepted',result
                assert result['entry_order_id']==first['entry_order_id'] and result['augment_order_id']=='manual-order-3'
                assert len(sdk.posts)==3 and sdk.posts[-1]['expiration']==int((NOW+timedelta(minutes=11)).timestamp())
                assert live.state.snapshot().openings[other.scope]==other
        finally:
            release.set()
            if worker:worker.join(3)
            assert live.close(timeout=3);live.trading.close()


def test_d2_condition_cancel_reports_discovery_failure(tmp_path,monkeypatch):
    for variant in ('incomplete_orders','read_busy','closed','complete_empty'):
        execution,live,sdk,public,clock=manual_runtime(tmp_path/variant,monkeypatch)
        entered,release=Event(),Event();worker=None;errors=[]
        try:
            sdk.orders=()
            if variant=='incomplete_orders':
                def unavailable(**kw):
                    sdk.order_reads+=1
                    raise RuntimeError('actual current orders unavailable')
                sdk.list_open_orders=unavailable
            elif variant=='read_busy':
                read=sdk.list_open_orders
                def held(**kw):
                    entered.set();assert release.wait(6),'actual Reader order discovery completion watchdog'
                    return read(**kw)
                sdk.list_open_orders=held
                def observe():
                    try:live.run_once(timeout=4)
                    except BaseException as exc:errors.append(exc)
                worker=threading.Thread(target=observe);worker.start();assert entered.wait(3)
            elif variant=='closed':
                assert live.close(timeout=3)
            execution._notifier.channel='feishu'  # actual external notifier destination
            notifications=execution._notifier.calls
            assert notifications==[]
            request=dict(confirm=True,condition_id=CONDITION_ID)
            if variant=='complete_empty':
                assert execution.lp_cancel_orders(request)==dict(requested=0,canceled=[],not_canceled={},skipped=[])
                assert notifications==[] and sdk.order_reads==1
            else:
                expected={'incomplete_orders':'current_orders_unknown','read_busy':'current_read_in_progress','closed':'program_stopping'}[variant]
                with pytest.raises(ValueError,match=expected):execution.lp_cancel_orders(request)
                assert len(notifications)==1 and notifications[0][0]=='LP 撤单失败'
                if variant=='incomplete_orders':assert sdk.order_reads==1
            assert sdk.cancels==[] and sdk.posts==[]
            assert live.status()['last_buy_uncertainty'] is None
        finally:
            release.set()
            if worker:
                worker.join(4);assert not worker.is_alive() and errors==[],errors
            assert live.close(timeout=3);live.trading.close()


def test_d2_named_augment_preserves_invalid_input_dispositions(tmp_path,monkeypatch):
    execution,live,sdk,public,clock=manual_runtime(tmp_path,monkeypatch)
    try:
        first=execution.lp_submit_entry(entry(clock))
        assert first['state']=='entry_open',first
        before=live.state.snapshot()
        with sqlite3.connect(live.store.path) as connection:
            preview_count=connection.execute('SELECT COUNT(*) FROM previews').fetchone()[0]
        for route in ('direct','preview'):
            for field,value,reason in (('quantity','0','quantity_invalid'),('quantity','invalid','quantity_invalid'),
                                       ('price','0','price_invalid'),('price','invalid','price_invalid')):
                key=route+'-'+field+'-'+value
                payload=dict(session_id=first['session_id'],quantity='20',price='.28',idempotency_key=key)
                payload[field]=value
                result=execution.lp_submit_augment(payload) if route=='direct' else execution.lp_augment_preview(payload)
                assert result==dict(state='rejected',reason=reason),(route,field,value,result)
                assert live.store.lp_manual_command(WALLET,key,session_id=first['session_id']) is None
                assert live.state.snapshot()==before and len(sdk.posts)==1
                with sqlite3.connect(live.store.path) as connection:
                    assert connection.execute('SELECT COUNT(*) FROM previews').fetchone()[0]==preview_count
        # Both omitted and explicit default price mean the original entry price,
        # which is still occupied. Neither can silently select a new price.
        for route in ('direct','preview'):
            payload=dict(session_id=first['session_id'],quantity='20',price=None,idempotency_key='default-'+route)
            result=execution.lp_submit_augment(payload) if route=='direct' else execution.lp_augment_preview(payload)
            assert result==dict(state='rejected',reason='price_level_active'),result
            assert live.store.lp_manual_command(WALLET,'default-'+route,session_id=first['session_id']) is None
        assert len(sdk.posts)==1 and sdk.cancels==[] and sdk.market_orders==[]
    finally:
        assert live.close(timeout=3);live.trading.close()
