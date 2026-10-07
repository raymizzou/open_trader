from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event
import time
from types import SimpleNamespace

import pytest
from timing_support import run_test_in_subprocess

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.polymarket_lp_risk import _maybe_decimal

NOW = datetime(2026, 9, 27, 8, tzinfo=UTC)


def _maybe_datetime(value):
    if value is None:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _manual_request(now):
    return {
        "market_id": "market-1",
        "condition_id": "m00",
        "token_id": "m00",
        "outcome": "YES",
        "price": Decimal("0.40"),
        "quantity": Decimal("10"),
        "review_at": now + timedelta(minutes=10),
        "reserved_usd": Decimal("4"),
    }



class Exchange:
    config = SimpleNamespace(wallet_address="test-wallet")

    def __init__(self):
        self.posts = []
        self.orders = []
        self.positions = []
        self.trades = []
        self.fail = False
        self.before_sign = None

    def lp_account_snapshot(self):
        return dict(authenticated=True, wallet_address="test-wallet", balance="1000", allowance="1000", checked_at=NOW,
                    open_orders=self.orders, positions=self.positions,
                    open_orders_complete=True, positions_complete=True)

    def direction(self, n):
        return dict(market=dict(market_id=n,condition_id=n,token_id=n,outcome="YES",
                    accepting_orders=True,exchange_type="CLOB",tick_size=Decimal('.01'),
                    minimum_order_size=Decimal('20'),reward_min_size=Decimal('20'),
                    reward_max_spread=Decimal('.10'),fee=Decimal(0),taker_fee_rate=Decimal(0),
                    fees_enabled=False,metadata_checked_at=NOW,fees_checked_at=NOW,
                    event_ended=False,event_start_time=NOW+timedelta(days=5),event_end_time=NOW+timedelta(days=6)),
                    book=dict(condition_id=n,token_id=n,received_at=NOW,
                    bids=[dict(price='.40',size='1000'),dict(price='.39',size='1000')],asks=[dict(price='.41',size='1000')]),
                    reward_active=True,daily_pool_usd=Decimal('24'),reward_checked_at=NOW,
                    event_windows=[],event_windows_complete=True)

    def lp_market_metadata_fresh(self, ids, **kwargs):
        return {n:{**self.direction(n)['market'], 'outcomes':{'yes':{'token_id':n,'label':'YES'}}} for n in ids}

    def lp_reward_catalog(self, *, condition_ids, **kwargs):
        return dict(checked_at=NOW, markets=[dict(condition_id=n,reward_active=True,
                    daily_pool_usd=Decimal('24'),reward_checked_at=NOW,rewards_min_size='20',
                    rewards_max_spread='10') for n in condition_ids])

    def lp_order_books(self, ids, **kwargs):
        return {n:self.direction(n)['book'] for n in ids}

    def lp_snapshot(self, request):
        d=self.direction(request['token_id'])
        return dict(account=self.lp_account_snapshot(),market=d['market'],book=d['book'],
                    orders=self.orders,trades=self.trades)

    def lp_create_limit_order(self, **kwargs):
        if self.before_sign:
            self.before_sign()
        return kwargs

    def lp_post_order(self, signed):
        self.posts.append(signed)
        if self.fail:
            raise TimeoutError()
        row=dict(order_id=f'o{len(self.posts)}',token_id=signed['token_id'],condition_id=signed['token_id'],
                 side=signed['side'],status='LIVE',price=signed['price'],original_size=signed['quantity'],size_matched='0')
        self.orders.append(row)
        return row


def setup(tmp_path, count=1):
    store=PredictionArbitrageStore(tmp_path/'state.sqlite')
    ex=Exchange()
    lp=PolymarketLPService(store,ex,clock=lambda:NOW)
    for i in range(count):
        n=f'm{i:02}'
        lp._candidate_pool_record_success(n,dict(condition_id=n),judged_at=NOW,facts=dict(directions=[ex.direction(n)],account=ex.lp_account_snapshot()))
        store.lp_save_price_history(n,n,[],dict(state='known',amplitude=Decimal('.005'),checked_at=NOW,valid_until=NOW+timedelta(days=1)))
    engine=PredictionExecutionService(store=store,monitor=SimpleNamespace(),trading=ex,
                notifier=SimpleNamespace(),lock_path=tmp_path/'execution.lock',lp=lp)
    engine._breaker_open = False  # Simulated clean startup; no real wallet is contacted.
    return engine,ex,lp,store


def _fresh_registration_bundle(x, lp):
    snapshot = x.lp_account_snapshot()
    snapshot.update(
        account_id='test-wallet',
        read_started_at=NOW,
        read_ended_at=NOW,
        checked_at=NOW,
        balance_complete=True,
        trades_complete=True,
        pagination_complete=True,
        raw_trades=x.trades,
        trade_generation=lp.store.lp_trade_generation(),
    )
    return snapshot


def test_default_configure_enable_and_idempotent_round(tmp_path):
    e,x,lp,s=setup(tmp_path,5)
    assert e.lp_auto_state()['desired_running'] is False
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=3))
    assert not x.posts
    e.lp_auto_set_desired_running(True)
    r=e.lp_auto_run_once(round_id='a')
    assert len(x.posts)==3, r['last_round']
    assert r['slots']['occupied']==3
    assert Decimal(r['funds']['available_usd'])==76
    e.lp_auto_run_once(round_id='a')
    assert len(x.posts)==3
    with pytest.raises(ValueError):
        e.lp_auto_configure(dict(budget_usd='120',target_buy_count=5))
    e.lp_auto_set_desired_running(False)
    with pytest.raises(ValueError):
        e.lp_auto_configure(dict(budget_usd='120',target_buy_count=5))


@pytest.mark.parametrize('reasons, expected', [
    (['market_read_capacity'], 'market_read_capacity'),
    (['market_read_timeout'], 'market_read_timeout'),
    (['market_read_cooling_down'], 'market_read_cooling_down'),
    (['market_read_in_progress'], 'market_read_in_progress'),
    (['market_read_capacity', 'market_read_timeout'], 'market_read_capacity'),
    (['market_read_capacity', 'strategy_funds_insufficient'], 'candidates_or_funds_insufficient'),
    (['market_read_capacity', None], 'target_filled'),
    (['market_read_capacity', None, 'market_read_timeout'], 'candidates_or_funds_insufficient'),
])
def test_round_summary_preserves_read_refusals_without_hiding_other_outcomes(tmp_path, monkeypatch, reasons, expected):
    e, exchange, _, _ = setup(tmp_path, len(reasons))
    e.lp_auto_configure(dict(budget_usd='100', target_buy_count=1 if expected == 'target_filled' else len(reasons)))
    e.lp_auto_set_desired_running(True)
    pool = e._lp_auto_pool()
    submit = pool._submit
    attempted = []

    def controlled(row, *args, **kwargs):
        reason = reasons[len(attempted)]
        attempted.append(row['condition_id'])
        if reason is not None:
            raise ValueError(reason)
        return submit(row, *args, **kwargs)

    monkeypatch.setattr(pool, '_submit', controlled)
    state = e.lp_auto_run_once(round_id='read-summary')
    assert len(attempted) == len(reasons)
    assert state['last_round']['reason'] == expected
    assert [a.get('reason') for a in state['last_round']['actions'] if a['state'] == 'rejected'] == [r for r in reasons if r]
    assert len(exchange.posts) == reasons.count(None)


@pytest.mark.parametrize('case, expected', [
    ('empty', 'candidates_or_funds_insufficient'),
    ('funds', 'candidates_or_funds_insufficient'),
    ('account', 'account_unknown'),
    ('filled', 'target_filled'),
])
def test_round_summary_keeps_existing_admission_and_empty_reasons(tmp_path, case, expected):
    e, exchange, lp, _ = setup(tmp_path, 0 if case == 'empty' else 1)
    e.lp_auto_configure(dict(budget_usd='1' if case == 'funds' else '100', target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    if case == 'account':
        lp._account_order_sync_error = 'account_unknown'
    if case == 'filled':
        assert e.lp_auto_run_once(round_id='fill')['slots']['occupied'] == 1
    state = e.lp_auto_run_once(round_id='summary')
    assert state['last_round']['reason'] == expected
    assert state['last_round']['actions'] == ([dict(condition_id='m00', state='rejected', reason='strategy_funds_insufficient')] if case == 'funds' else [])


def test_account_blocker_after_read_refusal_has_summary_priority(tmp_path, monkeypatch):
    e, _, lp, _ = setup(tmp_path, 2)
    e.lp_auto_configure(dict(budget_usd='100', target_buy_count=2))
    e.lp_auto_set_desired_running(True)
    def reject(*args, **kwargs):
        lp._account_order_sync_error = 'account_unknown'
        raise ValueError('market_read_capacity')
    monkeypatch.setattr(e._lp_auto_pool(), '_submit', reject)
    state = e.lp_auto_run_once()
    assert state['last_round']['reason'] == 'account_unknown'
    assert len(state['last_round']['actions']) == 1
    assert state['last_round']['actions'][0]['reason'] == 'market_read_capacity'


def test_full_pool_manual_exclusion_unknown_isolated_and_restart(tmp_path):
    e,x,lp,s=setup(tmp_path,13)
    x.orders=[dict(order_id=f'manual{i}',condition_id=f'm{i:02}',token_id=f'm{i:02}',side='BUY',
                   status='LIVE',price='.4',original_size='1',size_matched='0') for i in range(11)]
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=5))
    e.lp_auto_set_desired_running(True)
    x.fail=True
    r=e.lp_auto_run_once()
    assert len(x.posts)==2, r['last_round']
    assert [p['token_id'] for p in x.posts] == ['m11', 'm12']
    assert r['slots']['occupied']==2
    assert 'submission_unknown' in r['block_reasons']
    e.lp_auto_run_once()
    assert len(x.posts)==2
    e2=PredictionExecutionService(store=s,monitor=SimpleNamespace(),trading=x,notifier=SimpleNamespace(),lock_path=tmp_path/'execution.lock',lp=lp)
    e2.lp_auto_run_once()
    assert len(x.posts)==2
    assert e2.lp_auto_state()['run_id']==r['run_id']


def test_prepare_transport_error_is_determinate_not_sent(tmp_path):
    from polymarket.errors import TransportError
    from polymarket.models.clob import SignedOrder

    def sdk_signed(**kwargs):
        return SignedOrder(
            builder='0x1', expiration=int(kwargs['expiration']), maker='0x2',
            maker_amount=1, metadata='0x3', order_type='GTD', salt=1,
            side='BUY', signature='0x4', signature_type=0, signer='0x5',
            taker_amount=1, timestamp=1, token_id=str(kwargs['token_id']),
            post_only=True,
        )

    def prepare_then_transport(**kwargs):
        sdk_signed(**kwargs)
        raise TransportError('private')

    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    x.lp_create_limit_order=prepare_then_transport
    r=e.lp_auto_run_once()
    sid=r['intents'][0]['session_id']
    session=s.lp_session(sid)
    action=next(a for a in s.lp_actions(sid) if a['role']=='entry')
    assert not x.posts
    assert session['state']=='entry_rejected'
    assert session['submit_status']=='rejected'
    assert session['submit_stage']=='prepare_failed'
    assert session['submit_post_started_at'] is None
    assert session['submit_error_chain']==['TransportError']
    assert action['state']=='rejected'
    assert action['submit_error_chain']==['TransportError']
    assert r['slots']['occupied']==0
    assert r['funds']['status']=='known'
    assert Decimal(r['funds']['buy_reserved_usd'])==0


def test_post_timeout_keeps_one_temporary_reservation_without_retry(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    def sdk_signed(**kwargs):
        from polymarket.models.clob import SignedOrder
        return SignedOrder(
            builder='0x1', expiration=int(kwargs['expiration']), maker='0x2',
            maker_amount=1, metadata='0x3', order_type='GTD', salt=1,
            side='BUY', signature='0x4', signature_type=0, signer='0x5',
            taker_amount=1, timestamp=1, token_id=str(kwargs['token_id']),
            post_only=True,
        )
    x.lp_create_limit_order=sdk_signed
    def timeout(signed):
        x.posts.append(signed)
        raise TimeoutError('private')
    x.lp_post_order=timeout
    r=e.lp_auto_run_once()
    sid=r['intents'][0]['session_id']
    session=s.lp_session(sid)
    action=next(a for a in s.lp_actions(sid) if a['role']=='entry')
    assert len(x.posts)==1
    assert session['state']=='needs_attention'
    assert session['submit_status']=='unknown'
    assert session['submit_stage']=='send_unknown'
    assert session['submit_post_started_at']
    assert session['submit_finished_at']
    assert session['submit_timeout'] is True
    assert action['state']=='unknown'
    assert action['post_started'] is True
    e2=PredictionExecutionService(store=s,monitor=SimpleNamespace(),trading=x,
                notifier=SimpleNamespace(),lock_path=tmp_path/'execution.lock',lp=lp)
    e2._breaker_open=False
    assert len(e2.lp_auto_run_once()['intents'])==1
    assert len(x.posts)==1
    assert r['slots']['occupied']==1


def test_pause_during_signing_prevents_post(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    x.before_sign=lambda:e.lp_auto_set_desired_running(False)
    r=e.lp_auto_run_once()
    assert not x.posts
    assert r['pause_confirmed'] is True
    assert r['slots']['occupied']==0
    sid=r['intents'][0]['session_id']
    session=s.lp_session(sid)
    action=next(a for a in s.lp_actions(sid) if a['role']=='entry')
    for fact in (session,action):
        assert fact['submit_stage']=='pre_send_rejected'
        assert fact['post_started'] is False
        assert fact['submit_post_started_at'] is None


def test_callback_read_failure_before_post_releases_reservation(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    original=lp._read_candidate_snapshot
    reads=0
    def read(request,**kwargs):
        nonlocal reads
        reads+=1
        if reads==1:
            return original(request,**kwargs)
        raise RuntimeError('private pre-send failure')
    lp._read_candidate_snapshot=read
    r=e.lp_auto_run_once()
    sid=r['intents'][0]['session_id']
    session=s.lp_session(sid)
    action=next(a for a in s.lp_actions(sid) if a['role']=='entry')
    assert reads==2 and not x.posts
    assert session['state']=='entry_rejected'
    assert session['submit_stage']=='prepare_failed'
    assert session['submit_error_chain']==['RuntimeError']
    assert session['post_started'] is False
    assert action['submit_stage']=='prepare_failed'
    assert r['slots']['occupied']==0
    assert r['funds']['status']=='known'


def test_callback_lock_failure_before_post_releases_reservation(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    original=e._acquire_global_lock
    granted=[]
    def acquire():
        lock=original()
        if len(granted)==1:
            if lock is not None:
                e._release_global_lock(lock)
            granted.append(False)
            return None
        granted.append(lock is not None)
        if lock is not None:
            return lock
        return lock
    e._acquire_global_lock=acquire
    r=e.lp_auto_run_once()
    sid=r['intents'][0]['session_id']
    session=s.lp_session(sid)
    action=next(a for a in s.lp_actions(sid) if a['role']=='entry')
    assert granted==[True,False] and not x.posts
    assert session['state']=='entry_rejected'
    assert session['submit_stage']=='pre_send_rejected'
    assert session['reason']=='execution_lock'
    assert session['post_started'] is False
    assert action['submit_stage']=='pre_send_rejected'
    assert r['slots']['occupied']==0


def test_second_mutation_guard_before_post_releases_reservation(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    guards=0
    original=lp._require_mutation
    def guard():
        nonlocal guards
        guards+=1
        if guards==2:
            from open_trader.polymarket_lp import _MutationBlocked
            raise _MutationBlocked('mutation_blocked')
        original()
    lp._require_mutation=guard
    r=e.lp_auto_run_once()
    sid=r['intents'][0]['session_id']
    session=s.lp_session(sid)
    action=next(a for a in s.lp_actions(sid) if a['role']=='entry')
    assert guards==2 and not x.posts
    assert session['state']=='entry_rejected'
    assert session['submit_stage']=='prepare_failed'
    assert session['submit_error_chain']==['_MutationBlocked']
    assert session['post_started'] is False
    assert action['submit_stage']=='prepare_failed'
    assert r['slots']['occupied']==0
    assert r['funds']['status']=='known'
    assert Decimal(r['funds']['buy_reserved_usd'])==0


def test_missing_post_adapter_before_post_releases_reservation(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    x.lp_post_order=None
    r=e.lp_auto_run_once()
    sid=r['intents'][0]['session_id']
    session=s.lp_session(sid)
    action=next(a for a in s.lp_actions(sid) if a['role']=='entry')
    assert not x.posts
    assert session['state']=='entry_rejected'
    assert session['submit_stage']=='prepare_failed'
    assert session['submit_error_chain']==['RuntimeError']
    assert session['post_started'] is False
    assert action['submit_stage']=='prepare_failed'
    assert r['slots']['occupied']==0
    assert r['funds']['status']=='known'


@pytest.mark.parametrize('accepted', [True, False])
def test_terminal_receipt_facts_match_session_and_action(tmp_path,accepted):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    if not accepted:
        x.lp_post_order=lambda signed:x.posts.append(signed) or {
            'accepted':False,'ok':False,'status':'REJECTED','order_id':'rejected-1'}
    r=e.lp_auto_run_once()
    sid=r['intents'][0]['session_id']
    session=s.lp_session(sid)
    action=next(a for a in s.lp_actions(sid) if a['role']=='entry')
    expected_stage='receipt_received' if accepted else 'exchange_rejected'
    assert session['state']==('entry_open' if accepted else 'entry_rejected')
    assert session['submit_status']==('accepted' if accepted else 'rejected')
    assert action['state']==('accepted' if accepted else 'rejected')
    for name in ('submit_stage','post_started','submit_post_started_at',
                 'submit_finished_at','submit_receipt_at','submit_timeout'):
        assert session[name]==action[name]==({
            'submit_stage':expected_stage,
            'post_started':True,
            'submit_post_started_at':session['submit_post_started_at'],
            'submit_finished_at':session['submit_finished_at'],
            'submit_receipt_at':session['submit_receipt_at'],
            'submit_timeout':False,
        }[name])
    assert len(x.posts)==1
    assert r['slots']['occupied']==(1 if accepted else 0)


def test_concurrent_rounds_single_reservation(tmp_path, request):
    if run_test_in_subprocess(request):
        return
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    start = Barrier(2)
    def run_round(_):
        start.wait(timeout=5)
        return e.lp_auto_run_once(round_id='same')
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(run_round, range(2)))
    assert len(x.posts)==1
    assert len(e.lp_auto_state()['intents'])==1


def test_three_to_five_and_no_infinite_refill(tmp_path):
    e,x,lp,s=setup(tmp_path,3)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=5))
    e.lp_auto_set_desired_running(True)
    assert e.lp_auto_run_once()['slots']['occupied']==3
    for n in ('m03','m04'):
        lp._candidate_pool_record_success(n,dict(condition_id=n),judged_at=NOW,facts=dict(directions=[x.direction(n)],account=x.lp_account_snapshot()))
        s.lp_save_price_history(n,n,[],dict(state='known',amplitude=Decimal('.005'),checked_at=NOW,valid_until=NOW+timedelta(days=1)))
    assert e.lp_auto_run_once()['slots']['occupied']==5
    assert len(x.posts)==5


def test_fixed_budget_partial_sale_late_stream_and_restart(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    r=e.lp_auto_run_once()
    sid=r['intents'][0]['session_id']
    x.orders[0].update(status='FILLED',size_matched='20')
    x.positions=[dict(token_id='m00',condition_id='m00',size='20')]
    r=e.lp_auto_reconcile_unknown()
    assert Decimal(r['funds']['available_usd'])==92
    assert Decimal(r['funds']['inventory_cost_usd'])==8
    s.lp_update_session(sid,patch=dict(passive_exit_order_id='sell1',owned_order_ids=['o1','sell1']))
    x.orders.append(dict(order_id='sell1',token_id='m00',condition_id='m00',side='SELL',status='LIVE',price='.25',original_size='20',size_matched='0'))
    assert Decimal(e.lp_auto_reconcile_unknown()['funds']['inventory_cost_usd'])==8
    x.orders[1].update(size_matched='8')
    x.positions[0]['size']='12'
    r=e.lp_auto_reconcile_unknown()
    assert Decimal(r['funds']['realized_pnl_usd'])==Decimal('-1.20')
    assert Decimal(r['funds']['inventory_cost_usd'])==Decimal('4.80')
    assert Decimal(r['funds']['available_usd'])==Decimal('95.20')
    x.orders[1].update(status='FILLED',size_matched='20')
    x.positions=[]
    r=e.lp_auto_reconcile_unknown()
    assert Decimal(r['funds']['total_usd'])==100
    assert Decimal(r['funds']['available_usd'])==100
    x.trades=[dict(trade_id=f't{side}',status='CONFIRMED',matched_at=NOW.isoformat(),
              maker_orders=[dict(order_id=oid,token_id='m00',side=side,matched_amount='20',price=price,fee='0')])
              for side,oid,price in [('BUY','o1','.40'),('SELL','sell1','.25')]]
    s.lp_update_session(sid,patch=dict(facts_checked_at=NOW-timedelta(seconds=301)))
    before_events=e.lp_auto_report_facts()["events"]
    assert e.lp_generate_due_auto_reports()==[]
    assert e.lp_auto_report_facts()["events"]==before_events
    # Retained offline repair primitive; the paused production worker never calls it.
    e._lp_auto_pool().reconcile_reports()
    e._lp_auto_pool().reconcile_reports()
    r=e.lp_auto_state()
    assert Decimal(r['funds']['total_usd'])==100
    fills=[f for f in e.lp_auto_report_facts()['events'] if f['kind']=='fill']
    assert len(fills)==2
    assert all(f['occurred_at'] for f in fills)
    e.lp_auto_set_desired_running(False)
    assert Decimal(e.lp_auto_configure(dict(budget_usd='120',target_buy_count=1))['funds']['total_usd'])==120


def test_manual_origin_does_not_own_funds_and_unknown_fees_block(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    # A client-supplied origin field cannot create a server-owned identity.
    s.lp_create_session('manual','manual',state='complete',payload=dict(origin='auto',auto_run_id=e.lp_auto_state()['run_id'],buy_cost='50',sold_revenue='90'))
    assert e.lp_auto_state()['funds']['total_usd']=='100'
    assert e.lp_auto_report_facts()['sessions']==[]
    e.lp_auto_set_desired_running(True)
    r=e.lp_auto_run_once()
    x.orders[0].update(status='FILLED',size_matched='20')
    x.positions=[dict(token_id='m00',condition_id='m00',size='20')]
    original=x.lp_snapshot
    def unknown(request):
        snap=original(request)
        snap['market'].update(fees_enabled=True,fee=None)
        return snap
    x.lp_snapshot=unknown
    assert e.lp_auto_reconcile_unknown()['funds']['status']=='unknown'
    assert e.lp_auto_state()['funds']['available_usd'] is None
    assert Decimal(e.lp_auto_state()['funds']['buy_reserved_usd']) == 8
    assert e.lp_auto_state()['slots']['occupied'] == 1


def test_concurrent_configuration_is_atomic_target_total(tmp_path, request):
    if run_test_in_subprocess(request):
        return
    e,x,lp,s=setup(tmp_path)
    start = Barrier(2)
    def configure(_):
        start.wait(timeout=5)
        return e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(configure, range(2)))
    assert e.lp_auto_state()['funds']['total_usd']=='100'
    assert e.lp_auto_state()['config_version']==2


def test_sync_manages_exchange_ids_without_precomputed_match(tmp_path, monkeypatch):
    e,x,lp,s=setup(tmp_path,2)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=2))
    e.lp_auto_set_desired_running(True)
    create=x.lp_create_limit_order
    x.lp_create_limit_order=lambda **kwargs:{**create(**kwargs),'order_id':'audit-' + kwargs['token_id']}
    x.fail=True
    r=e.lp_auto_run_once()
    assert len(x.posts)==2
    assert 'submission_unknown' in r['block_reasons']
    e.lp_auto_set_desired_running(False)
    for token in ('m00', 'm01'):
        x.orders.append(dict(order_id='venue-' + token,token_id=token,condition_id=token,side='BUY',status='LIVE',price='.4',original_size='20',size_matched='0'))
    x.fail=False
    # A qualifying round begins strictly after both sends ended. The old
    # expectation kept missing-ID holds forever; account coverage replaces
    # those holds while preserving the UNKNOWN original request audit.
    monkeypatch.setitem(globals(), 'NOW', NOW + timedelta(seconds=1))
    first=lp.register_account_snapshot(_fresh_registration_bundle(x,lp))
    assert first['state']=='registered', first
    sessions=s.lp_active_sessions()
    assert {session['token_id'] for session in sessions}=={'m00','m01'}
    assert sorted(order_id for session in sessions for order_id in session['owned_order_ids'])==['venue-m00','venue-m01']
    assert all('audit-' not in order_id for session in sessions for order_id in session['owned_order_ids'])
    covered = e.lp_auto_state()
    assert covered['funds']['status']=='known'
    assert Decimal(covered['funds']['buy_reserved_usd']) == 16
    assert covered['slots']['occupied'] == 2
    assert all(i['state'] == 'unknown' and i['order_id'] is None and i['reservation_coverage'] for i in covered['intents'])
    assert covered['desired_running'] is False
    repeat=lp.register_account_snapshot(_fresh_registration_bundle(x,lp))
    assert repeat['state']=='registered', repeat
    assert len(s.lp_active_sessions())==2
    assert len(x.posts)==2


def test_full_verified_sell_releases_slot_and_wakes_refill_beside_missing_identity(tmp_path):
    e,x,lp,s=setup(tmp_path,5)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=5))
    e.lp_auto_set_desired_running(True)
    initial=e.lp_auto_run_once()
    completed=[i for i in initial['intents'] if i['order_id']=='o1'][0]
    unknown=[i for i in initial['intents'] if i['order_id']=='o2'][0]

    x.orders[0].update(status='FILLED',size_matched='20')
    x.orders.append(dict(order_id='sell-full',token_id='m00',condition_id='m00',side='SELL',
                         status='FILLED',price='.25',original_size='20',size_matched='20'))
    x.trades=[dict(trade_id=f't{side}',status='CONFIRMED',matched_at=NOW.isoformat(),
               maker_orders=[dict(order_id=oid,token_id='m00',side=side,matched_amount='20',price=price,fee='0')])
              for side,oid,price in [('BUY','o1','.40'),('SELL','sell-full','.25')]]
    s.lp_update_session(completed['session_id'],patch=dict(
        passive_exit_order_id='sell-full',owned_order_ids=['o1','sell-full']))
    lp.tick()
    x.orders[:] = [row for row in x.orders if row['order_id'] != 'sell-full']
    session=s.lp_session(completed['session_id'])
    receipt=session['order_history']['sell-full']
    s.lp_update_session(completed['session_id'],state='needs_attention',patch={
        'orders_terminal': False,
        'order_history': {'sell-full': {**receipt, 'status': 'UNKNOWN', 'read_error': 'order_lookup_unavailable'}},
    })
    e._lp_auto_pool()._update(lambda d:d['intents'][completed['intent_id']].update(
        state='unknown',financial_status='unknown',submission_unknown=True,
        reconcile_reason='order_lookup_unavailable',settled=False))

    s.lp_update_session(unknown['session_id'],state='needs_attention',patch={
        'entry_order_id': None,'owned_order_ids': [], 'order_history': {},
        'submit_status': 'unknown', 'resume_state': 'entry_submit_pending',
    })
    for action in s.lp_actions(unknown['session_id']):
        if action.get('role') == 'entry':
            s.lp_upsert_action(unknown['session_id'],action['action_key'],state='unknown',
                payload={key:value for key,value in action.items()
                         if key not in {'action_id','action_key','state','order_id'}})
    x.orders[:] = [row for row in x.orders if row.get('order_id') != unknown['order_id']]
    e._lp_auto_pool()._update(lambda d:d['intents'][unknown['intent_id']].update(
        state='unknown',financial_status='unknown',submission_unknown=True,
        reconcile_reason='missing_reliable_order_id',settled=False))

    lp._candidate_pool_record_success('m05',dict(condition_id='m05'),judged_at=NOW,
        facts=dict(directions=[x.direction('m05')],account=x.lp_account_snapshot()))
    s.lp_save_price_history('m05','m05',[],dict(state='known',amplitude=Decimal('.005'),
        checked_at=NOW,valid_until=NOW+timedelta(days=1)))
    state=e.lp_auto_run_once()
    assert state['slots'] == dict(active=4,pending=1,pending_review=1,canceling=0,occupied=5)
    assert state['slots']['pending_review'] == 1
    assert [p['token_id'] for p in x.posts[-1:]] == ['m05']
    unresolved=next(i for i in state['intents'] if i['session_id']==unknown['session_id'])
    settled=next(i for i in state['intents'] if i['session_id']==completed['session_id'])
    assert unresolved['reconcile_reason'] == 'missing_reliable_order_id'
    assert unresolved['submission_unknown'] is True
    assert Decimal(unresolved['reserved_usd']) == 8
    assert settled['state'] == 'terminal'
    assert settled['settled'] is True
    assert settled['financial_status'] == 'known'
    assert Decimal(settled['reserved_usd']) == 0


def test_partial_buy_cancel_releases_only_unfilled_and_config_deficit(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    r=e.lp_auto_run_once()
    x.orders[0].update(size_matched='8')
    x.positions=[dict(token_id='m00',condition_id='m00',size='8')]
    r=e.lp_auto_reconcile_unknown()
    assert r['slots']['occupied']==1
    assert Decimal(r['funds']['available_usd'])==92
    e.lp_auto_set_desired_running(False)
    x.orders[0]['status']='CANCELED'
    r=e.lp_auto_reconcile_unknown()
    assert r['slots']['occupied']==0
    assert Decimal(r['funds']['available_usd'])==Decimal('96.8')
    r=e.lp_auto_configure(dict(budget_usd='1',target_buy_count=1))
    assert 'inventory_exceeds_budget' in r['block_reasons']
    assert Decimal(r['funds']['deficit_usd'])==Decimal('2.2')


def test_read_only_financial_period_uses_trade_times(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    r=e.lp_auto_run_once()
    sid=r['intents'][0]['session_id']
    x.orders[0].update(status='FILLED',size_matched='20')
    x.positions=[dict(token_id='m00',condition_id='m00',size='20')]
    x.trades=[dict(trade_id='buy1',status='CONFIRMED',matched_at=(NOW-timedelta(hours=1)).isoformat(),maker_orders=[dict(order_id='o1',token_id='m00',side='BUY',matched_amount='20',price='.40',fee='0')])]
    e.lp_auto_reconcile_unknown()
    s.lp_update_session(sid,patch=dict(passive_exit_order_id='sell1',owned_order_ids=['o1','sell1']))
    x.orders.append(dict(order_id='sell1',token_id='m00',condition_id='m00',side='SELL',status='LIVE',price='.25',original_size='20',size_matched='8'))
    x.positions[0]['size']='12'
    x.trades.append(dict(trade_id='sell1',status='CONFIRMED',matched_at=NOW.isoformat(),maker_orders=[dict(order_id='sell1',token_id='m00',side='SELL',matched_amount='8',price='.25',fee='0')]))
    e.lp_auto_reconcile_unknown()
    f=e.lp_auto_report_facts(period_start=NOW-timedelta(minutes=30),period_end=NOW+timedelta(seconds=1))['financial_period']
    assert f['status']=='known'
    assert Decimal(f['realized_pnl_usd'])==Decimal('-1.2')
    assert Decimal(f['inventory_cost_usd'])==Decimal('4.8')
    assert f['inventories'][0]['quantity']=='12'
    midnight=e.lp_auto_report_facts(period_start=NOW,period_end=NOW)['financial_period']
    assert Decimal(midnight['realized_pnl_usd'])==0


def test_budget_skips_expensive_market_and_uses_later_candidate(tmp_path):
    e,x,lp,s=setup(tmp_path,2)
    original=x.direction
    def direction(n):
        d=original(n)
        if n=='m00':
            d['market'].update(minimum_order_size=Decimal('200'),reward_min_size=Decimal('200'))
            d['daily_pool_usd']=Decimal('2400')
        return d
    x.direction=direction
    lp._candidate_qualification_facts['m00']=dict(directions=[direction('m00')],account=x.lp_account_snapshot())
    e.lp_auto_configure(dict(budget_usd='10',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    e.lp_auto_run_once()
    assert len(x.posts)==1
    assert x.posts[0]['token_id']=='m01'


def test_read_state_and_report_facts_do_not_initialize_storage(tmp_path):
    e,x,lp,s=setup(tmp_path)
    first=e.lp_auto_state()
    assert e.lp_auto_report_facts()['auto_run_id']==first['run_id']
    with s._read_connection() as c:
        assert c.execute('SELECT COUNT(*) FROM lp_auto_pool').fetchone()[0]==0
    assert not x.posts


def test_send_time_cash_recheck_and_automatic_augment_block(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    account=x.lp_account_snapshot
    def drain():
        x.lp_account_snapshot=lambda:{**account(),'balance':'0','allowance':'0'}
    x.before_sign=drain
    r=e.lp_auto_run_once()
    assert not x.posts
    assert r['slots']['occupied']==0
    x.before_sign=None
    x.lp_account_snapshot=account
    r=e.lp_auto_run_once()
    assert len(x.posts)==1
    sid=[i['session_id'] for i in r['intents'] if i['state']=='active'][0]
    assert lp.submit_augment(sid,'1','manual-augment')['reason']=='automatic_session_augmentation_disabled'


def test_automatic_ui_skips_intent_reads_and_keeps_funds_and_slots():
    import subprocess
    from pathlib import Path
    source=Path('src/open_trader/dashboard_static/dashboard.js').read_text()
    fn=source[source.index('function lpAutoFundsAndOrders(auto)'):]
    code='''function escapeHtml(s){return String(s).replaceAll('<','&lt;');}
    function predictionHasValue(v){return v!==null && v!==undefined;}
    function lpDashboardMoney(v){return '$'+v;}
    function lpDashboardPrice(v){return v;}
    function predictionValue(v,f){return v??f;}
    '''+fn+'''
    const auto={target_buy_count:5,slots:{active:3,pending:2,pending_review:2,occupied:5},funds:{status:'unknown'},last_round:{reason:'<script>'}};
    Object.defineProperty(auto, 'intents', {get(){throw Error('paused intent list read');}});
    const html=lpAutoFundsAndOrders(auto);
    if(html.includes('自动订单')||html.includes('data-auto-intent')||html.includes('<script>')||!html.includes('&lt;script>')||!html.includes('UNKNOWN'))process.exit(1);
    if(!html.includes('目标 5 · 有效 BUY 3 · 待核对占位 2')||!html.includes('共占位 5'))process.exit(1);
    '''
    subprocess.run(['node','-e',code],check=True)


def test_account_switch_cannot_reconcile_another_wallets_pool(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    e.lp_auto_run_once()
    before=e.lp_auto_report_facts()
    x.config=SimpleNamespace(wallet_address='different-wallet')
    original=x.lp_snapshot
    x.lp_snapshot=lambda request:(_ for _ in ()).throw(AssertionError('wrong wallet must not read'))
    assert 'account_identity_unknown' in e.lp_auto_reconcile_unknown()['block_reasons']
    e.lp_auto_run_once()
    after=e.lp_auto_report_facts()
    assert after['funds'] == {**before['funds'], 'spendable_usd': None}
    assert after['events']==before['events']
    assert len(x.posts)==1
    with pytest.raises(ValueError,match='account_identity'):
        e.lp_auto_set_desired_running(True)


@pytest.mark.parametrize('stage',['snapshot','sign','post'])
def test_slow_automatic_network_does_not_block_existing_session_cancel(tmp_path,stage):
    from threading import Barrier, Event
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=2))
    e.lp_auto_set_desired_running(True)
    sid=e.lp_auto_run_once()['intents'][0]['session_id']
    lp._candidate_pool_record_success('m01',dict(condition_id='m01'),judged_at=NOW,
        facts=dict(directions=[x.direction('m01')],account=x.lp_account_snapshot()))
    s.lp_save_price_history('m01','m01',[],dict(state='known',amplitude=Decimal('.005'),checked_at=NOW,valid_until=NOW+timedelta(days=1)))
    entered,release=Event(),Event()
    def hold():
        entered.set()
        assert release.wait(5)
    if stage=='snapshot':
        original=lp._read_candidate_snapshot
        def read(request,**kwargs):
            if request['token_id']=='m01':
                hold()
            return original(request,**kwargs)
        lp._read_candidate_snapshot=read
    elif stage=='sign':
        x.before_sign=hold
    else:
        original=x.lp_post_order
        def post(signed):
            hold()
            return original(signed)
        x.lp_post_order=post
    canceled=[]
    def cancel(oid):
        canceled.append(oid)
        return {'canceled':[oid]}
    x.cancel_order=cancel
    with ThreadPoolExecutor(2) as pool:
        automatic=pool.submit(e.lp_auto_run_once)
        try:
            assert entered.wait(3)
            stopped=pool.submit(e.lp_stop,sid).result(timeout=2)
            assert stopped['state']=='review', stopped
            assert canceled==['o1']
            # A separate reconciler cannot release the reserved-before-send intent.
            assert e.lp_auto_reconcile_unknown()['round_reason']=='round_in_progress'
        finally:
            release.set()
        automatic.result(timeout=5)


def test_presend_rejection_leaves_round_quota_for_next_market(tmp_path):
    e,x,lp,s=setup(tmp_path,2)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    sign=x.lp_create_limit_order
    def change_first(**kwargs):
        signed=sign(**kwargs)
        if kwargs['token_id']=='m00':
            x.orders.append(dict(order_id='manual',condition_id='m00',token_id='m00',side='BUY',status='LIVE',price='.4',original_size='20',size_matched='0'))
        return signed
    x.lp_create_limit_order=change_first
    r=e.lp_auto_run_once()
    assert len(x.posts)==1, r['last_round']
    assert x.posts[0]['token_id']=='m01'


def test_owned_sell_actions_have_distinct_intents_even_without_order_id(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    parent=e.lp_auto_run_once()['intents'][0]
    sid=parent['session_id']
    s.lp_upsert_action(sid,'exit-attempt',state='unknown',payload=dict(role='protected_exit',side='SELL',quantity='20',min_price='.25',submit_requested_at=NOW.isoformat()))
    s.lp_update_session(sid,patch=dict(protected_exit_attempt_state='unknown'))
    r=e.lp_auto_reconcile_unknown()
    assert 'submission_unknown' in r['block_reasons']
    assert r['funds']['available_usd'] is None
    events=e.lp_auto_report_facts()['events']
    sells=[v for v in events if v['side']=='SELL']
    assert {v['kind'] for v in sells}=={'intent','unknown'}
    assert {v['intent_id'] for v in sells}=={'action:exit-attempt'}
    assert all(v['parent_intent_id']==parent['intent_id'] and not v['order_id'] for v in sells)
    e.lp_auto_reconcile_unknown()
    assert len(e.lp_auto_report_facts()['events'])==len(events)


@pytest.mark.parametrize('stage',['sign','post'])
def test_stop_of_same_inflight_session_survives_late_receipt(tmp_path,stage):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    def stop():
        sid=e.lp_auto_state()['intents'][0]['session_id']
        assert e.lp_stop(sid)['state']=='review'
    if stage=='sign':
        x.before_sign=stop
    else:
        original=x.lp_post_order
        def post(signed):
            stop()
            return original(signed)
        x.lp_post_order=post
    r=e.lp_auto_run_once()
    sid=r['intents'][0]['session_id']
    if stage=='sign':
        assert not x.posts
        assert r['slots']['occupied']==0
    else:
        assert s.lp_session(sid)['state']=='review'
        assert r['funds']['status']=='unknown'
        assert Decimal(r['funds']['buy_reserved_usd'])==8
        canceled=[]
        x.cancel_order=lambda oid:canceled.append(oid) or {'canceled':[oid]}
        e.lp_tick()
        assert canceled==['o1']


@pytest.mark.parametrize('sell_receipt_unknown', [False, True])
def test_late_live_receipt_preserves_already_verified_fill(tmp_path, sell_receipt_unknown):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    post=x.lp_post_order
    def delayed_receipt(signed):
        response=dict(post(signed))
        if signed['side'] == 'SELL':
            if sell_receipt_unknown:
                raise TimeoutError('offline SELL receipt unknown')
            return response
        x.orders[0].update(status='FILLED',size_matched='20')
        x.positions=[dict(token_id='m00',condition_id='m00',size='20')]
        assert lp.register_account_snapshot(_fresh_registration_bundle(x,lp))['state']=='registered'
        e.lp_tick()
        return response
    x.lp_post_order=delayed_receipt
    r=e.lp_auto_run_once()
    session=s.lp_session(r['intents'][0]['session_id'])
    assert session['order_history']['o1']['status']=='FILLED'
    assert r['funds']['status']=='unknown'  # A SELL changed the trading generation before the late BUY reply.
    assert r['slots']['occupied']==1
    assert Decimal(session['buy_filled_quantity']) == 20
    # A matched receipt does not supply the missing API position cost.
    assert r['funds']['inventory_cost_usd'] is None
    assert 'account_position_cost_unknown' in r['admission_block_reasons']
    x.positions[0]['average_price'] = '.40'
    x.trades = [dict(id='late-fill', asset_id='m00', status='CONFIRMED',
        trader_side='MAKER', taker_order_id='other-account', side='BUY',
        price='.40', size='20', match_time=NOW, fee_rate_bps='0',
        maker_orders=[dict(order_id='o1', token_id='m00', maker_address='test-wallet',
            side='BUY', matched_amount='20', price='.40', fee_rate_bps='0')])]
    now = [NOW]
    lp.clock = lambda: now[0]
    def complete_account_round(*, max_age_seconds=0, trade_generation_provider=None):
        del max_age_seconds
        now[0] += timedelta(microseconds=1)
        snapshot = _fresh_registration_bundle(x, lp)
        snapshot.update(read_started_at=now[0], read_ended_at=now[0], checked_at=now[0])
        if trade_generation_provider is not None:
            snapshot['trade_generation'] = trade_generation_provider()
        return snapshot
    x.lp_account_snapshot_shared = complete_account_round
    audit = s.lp_actions(session['session_id'])
    recovered=e.lp_auto_reconcile_unknown()
    facts = e._auto_pool._read()['account_financial_facts']
    assert facts['financial_status'] == 'known' and facts['reason_codes'] == []
    assert facts['buys'] == []
    assert facts['positions'] == [dict(token_id='m00', quantity='20', inventory_cost_usd='8.00')]
    assert facts['independent_sell_unknown'] is sell_receipt_unknown
    assert recovered['funds']['status'] == ('unknown' if sell_receipt_unknown else 'known')
    if sell_receipt_unknown:
        assert recovered['funds']['available_usd'] is None
        assert 'unbounded_financial_uncertainty' in recovered['admission_block_reasons']
        assert any(a['side'] == 'SELL' and a['state'] == 'unknown' and not a.get('order_id') for a in audit)
    else:
        assert not recovered['admission_block_reasons']
        assert any(a['side'] == 'SELL' and a['state'] == 'accepted' and a.get('order_id') == 'o2' for a in audit)
    assert recovered['slots']['occupied']==0
    assert Decimal(recovered['funds']['inventory_cost_usd'])==8
    current = s.lp_session(session['session_id'])
    assert Decimal(current['buy_filled_quantity']) == Decimal(current['residual_quantity']) == 20
    assert current['order_history']['o1']['status'] == 'FILLED'
    assert Decimal(current['verified_order_fills']['o1']['quantity']) == 20
    assert current['reservation_coverage']['state'] == 'covered'
    assert s.lp_actions(session['session_id']) == audit
    repeated = e.lp_auto_reconcile_unknown()
    assert {k: v for k, v in repeated['funds'].items() if k != 'as_of'} == {
        k: v for k, v in recovered['funds'].items() if k != 'as_of'}
    assert repeated['funds']['as_of'] > recovered['funds']['as_of']
    assert repeated['slots'] == recovered['slots']
    assert s.lp_actions(session['session_id']) == audit
    assert len(x.posts) == 2 and [p['side'] for p in x.posts] == ['BUY', 'SELL']


@pytest.mark.parametrize('signed_id',[False,True])
def test_receipt_apply_respects_another_service_tick_lock(tmp_path,signed_id):
    from threading import Barrier, Event, Lock
    e,x,lp,s=setup(tmp_path)
    other_store=PredictionArbitrageStore(s.data_dir)
    other_lp=PolymarketLPService(other_store,x,clock=lambda:NOW)
    other=PredictionExecutionService(store=other_store,monitor=SimpleNamespace(),trading=x,
        notifier=SimpleNamespace(),lock_path=tmp_path/'execution.lock',lp=other_lp)
    other._breaker_open=False
    other._process_lock=Lock()  # Separate-process mutex; both still share the real file lock.
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    if signed_id:
        create=x.lp_create_limit_order
        x.lp_create_limit_order=lambda **kwargs:{**create(**kwargs),'order_id':'o1'}
    posted,return_receipt,tick_holds_lock,release_tick=Event(),Event(),Event(),Event()
    post=x.lp_post_order
    def delayed(signed):
        response=dict(post(signed))
        if signed_id:
            x.orders[0].update(status='FILLED',size_matched='20')
            x.positions=[dict(token_id='m00',condition_id='m00',size='20')]
        posted.set()
        assert return_receipt.wait(5)
        return response
    x.lp_post_order=delayed
    update=other_store.lp_publish_facts
    def hold_apply(*args,**kwargs):
        if not tick_holds_lock.is_set():
            tick_holds_lock.set()
            assert release_tick.wait(5)
        return update(*args,**kwargs)
    other_store.lp_publish_facts=hold_apply
    with ThreadPoolExecutor(2) as pool:
        automatic=pool.submit(e.lp_auto_run_once)
        try:
            assert posted.wait(3)
            tick=pool.submit(other.lp_tick)
            assert tick_holds_lock.wait(3)
            return_receipt.set()
            r=automatic.result(timeout=2)
            sid=r['intents'][0]['session_id']
            assert s.lp_session(sid).get('submit_status')!='accepted'
            actions=s.lp_actions(sid)
            assert any(a['state']=='accepted' and a.get('order_id')=='o1' for a in actions)
        finally:
            release_tick.set()
            return_receipt.set()
        tick.result(timeout=5)
    r=e.lp_auto_reconcile_unknown()
    assert 'submission_unknown' not in r['block_reasons']
    assert r['slots']['occupied']==(0 if signed_id else 1)
    assert len(x.posts)==1
    assert Decimal(r['funds']['inventory_cost_usd'])==(8 if signed_id else 0)


def test_exchange_receipt_id_is_owner_when_signed_id_differs(tmp_path):
    e,x,lp,s=setup(tmp_path,2)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=2))
    e.lp_auto_set_desired_running(True)
    create=x.lp_create_limit_order
    x.lp_create_limit_order=lambda **kwargs:{**create(**kwargs),'order_id':'signed-id'}
    r=e.lp_auto_run_once()
    assert 'submission_unknown' not in r['block_reasons']
    assert len(x.posts)==2
    sessions=[s.lp_session(row['session_id']) for row in r['intents']]
    assert {row['session_id'] for row in r['intents']}=={row['session_id'] for row in sessions}
    assert sorted(order_id for session in sessions for order_id in session['owned_order_ids'])==['o1','o2']
    assert all('order_identity_conflict' not in session for session in sessions)
    assert all('signed-id' not in session['owned_order_ids'] for session in sessions)
    assert e.lp_auto_state()['slots']['occupied']==2
    e.lp_auto_run_once()
    assert len(x.posts)==2


def test_accepted_sell_missing_receipt_blocks_new_buys_until_exact_id_recovers(tmp_path):
    e,x,lp,s=setup(tmp_path,2)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    r=e.lp_auto_run_once()
    sid=r['intents'][0]['session_id']
    x.orders[0].update(status='FILLED',size_matched='20')
    x.positions=[dict(token_id='m00',condition_id='m00',size='20')]
    e.lp_tick()
    assert len(x.posts)==2 and x.posts[1]['side']=='SELL'
    assert any(a.get('side')=='SELL' and a['state']=='accepted' for a in s.lp_actions(sid))
    e.lp_auto_reconcile_unknown()
    sell=x.orders.pop()
    r=e.lp_auto_run_once()
    assert 'submission_unknown' in r['block_reasons']
    assert r['funds']['available_usd'] is None
    assert r['slots']['occupied']==1  # Unresolved owned SELL facts keep the session fenced.
    assert len(x.posts)==2
    events=e.lp_auto_report_facts()['events']
    assert any(v['kind']=='unknown' and v['side']=='SELL' and v['order_id']==sell['order_id'] for v in events)
    x.orders.append({**sell,'status':'CANCELED'})
    r=e.lp_auto_reconcile_unknown()
    assert 'submission_unknown' not in r['block_reasons']
    assert r['funds']['status']=='known'
    e.lp_auto_run_once()
    assert len(x.posts)==3 and x.posts[-1]['token_id']=='m01'
    event=next(v for v in e.lp_auto_report_facts()['events'] if v['event_id']==f"receipt_unknown:{sell['order_id']}")
    assert event['occurred_at'] is None and event['resolved_at']==NOW.isoformat()


def test_known_buy_receipt_uncertainty_resolves_and_reopens_without_new_intent(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    e.lp_auto_run_once()
    order=x.orders.pop()
    e.lp_auto_reconcile_unknown()
    first=next(v for v in e.lp_auto_report_facts()['events'] if v['event_id']=='receipt_unknown:o1')
    assert first['reason']=='order_receipt_unknown' and first['occurred_at'] is None
    x.orders.append(order)
    e.lp_auto_reconcile_unknown()
    restored=next(v for v in e.lp_auto_report_facts()['events'] if v['event_id']=='receipt_unknown:o1')
    assert restored['resolved_at']==NOW.isoformat()
    lp.clock=lambda:NOW+timedelta(seconds=1)
    x.orders.pop()
    e.lp_auto_reconcile_unknown()
    events=e.lp_auto_report_facts()['events']
    repeated=next(v for v in events if v['event_id']=='receipt_unknown:o1')
    assert 'resolved_at' not in repeated and repeated['observed_at']!=first['observed_at']
    assert len([v for v in events if v['kind']=='intent'])==1


def test_signed_id_does_not_quarantine_venue_order_in_public_tick_and_stop(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    create=x.lp_create_limit_order
    x.lp_create_limit_order=lambda **kwargs:{**create(**kwargs),'order_id':'prepared-id'}
    sid=e.lp_auto_run_once()['intents'][0]['session_id']
    cancellations=[]
    x.cancel_order=lambda oid:cancellations.append(oid) or {'canceled':[oid]}
    assert e.lp_tick()['state']=='entry_open'
    session=s.lp_session(sid)
    assert session['owned_order_ids']==['o1']
    assert 'order_identity_conflict' not in session
    original=x.direction
    def depleted(n):
        d=original(n)
        d['book']['bids'][0]['size']='20'
        return d
    x.direction=depleted
    assert e.lp_stop(sid)['state']=='review'
    assert cancellations==['o1']
    assert 'prepared-id' not in cancellations
    assert len(x.posts)==1


def test_slow_deadline_cancel_does_not_block_other_fact_publication(tmp_path):
    e,x,lp,s=setup(tmp_path,2)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=2))
    e.lp_auto_set_desired_running(True)
    created=e.lp_auto_run_once()
    rows=created['intents']
    sid_a,sid_b=rows[0]['session_id'],rows[1]['session_id']
    s.lp_update_session(sid_a,patch=dict(review_at=NOW))
    entered,release=Event(),Event()
    def slow_cancel(order_id):
        if order_id == 'o1':
            entered.set()
            assert release.wait(5)
        return {'canceled':[order_id]}
    x.cancel_order=slow_cancel
    locks=(e._acquire_global_lock,e._release_global_lock)
    with ThreadPoolExecutor(2) as pool:
        slow=pool.submit(lp.reconcile_facts,sid_a,monitor=True,apply_lock=locks)
        try:
            assert entered.wait(5)
            other=pool.submit(lp.reconcile_facts,sid_b,monitor=True,apply_lock=locks)
            result=other.result(timeout=2)
            assert result[3] is None, result[3]
            assert _maybe_datetime(s.lp_session(sid_b)['facts_checked_at'])==NOW
        finally:
            release.set()
        slow.result(timeout=5)


def test_slow_public_lp_stop_does_not_block_other_fact_publication(tmp_path):
    e,x,lp,s=setup(tmp_path,2)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=2))
    e.lp_auto_set_desired_running(True)
    rows=e.lp_auto_run_once()['intents']
    sid_a,sid_b=rows[0]['session_id'],rows[1]['session_id']
    entered,release=Event(),Event()
    def slow_cancel(order_id):
        if order_id == 'o1':
            entered.set()
            assert release.wait(5)
        return {'canceled':[order_id]}
    x.cancel_order=slow_cancel
    with ThreadPoolExecutor(2) as pool:
        slow=pool.submit(e.lp_stop,sid_a)
        try:
            assert entered.wait(5)
            other=pool.submit(lp.reconcile_facts,sid_b)
            result=other.result(timeout=2)
            assert result[3] is None, result[3]
            assert _maybe_datetime(s.lp_session(sid_b)['facts_checked_at'])==NOW
        finally:
            release.set()
        assert slow.result(timeout=5)['state']=='review'


def test_publication_lock_wait_arms_durable_attention_progress(tmp_path, monkeypatch):
    from tests import test_lp_auto_pool as venue
    from copy import deepcopy

    from threading import Barrier, Event as ThreadEvent
    class Notifier:
        def __init__(self):
            self.calls=[]; self.done=ThreadEvent()
        def notify(self,title,message):
            self.calls.append((title,message)); self.done.set()

    e,x,lp,s=setup(tmp_path)
    notifier=Notifier(); e._notifier=notifier
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    row=e.lp_auto_run_once()['intents'][0]
    intent_id,sid=row['intent_id'],row['session_id']
    read=x.lp_snapshot
    base_now=NOW
    monkeypatch.setattr(venue,'NOW',base_now)
    lp.clock=lambda:venue.NOW
    locks=(lambda:None,lambda handle:None)
    result=lp.reconcile_facts(sid,monitor=True,apply_lock=locks)
    assert result[3]=='execution_lock'
    intent=e._auto_pool._read()['intents'][intent_id]
    assert intent['financial_status']=='known', 'a valid facts lease must survive a publication wait'
    assert intent['reconcile_reason']=='execution_lock'
    assert intent['publication_pending'] is True
    assert intent['attention_since']==base_now.isoformat()
    assert intent['attention_due'] is False and not notifier.calls
    assert _maybe_datetime(intent['reconcile_retry_at'])==base_now+timedelta(seconds=60)
    assert intent['reconcile_retry_source']=='fallback_minute'

    monkeypatch.setattr(venue,'NOW',base_now+timedelta(seconds=301))
    def fresh(request):
        snapshot=deepcopy(read(request))
        snapshot['account']['checked_at']=venue.NOW
        return snapshot
    x.lp_snapshot=fresh
    result=lp.reconcile_facts(sid,monitor=True,apply_lock=locks)
    assert result[3]=='execution_lock'
    intent=e._auto_pool._read()['intents'][intent_id]
    assert intent['attention_due'] is False
    assert intent.get('attention_notified') is not True
    assert notifier.calls == []


def test_trade_claim_advances_both_fences_and_rejects_stale_claim(tmp_path):
    e,x,lp,s=setup(tmp_path)
    sid='claim-session'
    s.lp_create_session(sid,sid,state='entry_open',payload=dict(condition_id='a',token_id='a'))
    session=s.lp_session(sid)
    before_trade=s.lp_session_revision(sid,trading=True)
    before_generation=s.lp_trade_generation()
    claims=s.lp_claim_trade_actions(
        sid,trade_revision=before_trade,trade_generation=before_generation,
        actions=[(f'{sid}:test-cancel:o1',dict(role='reconciliation_cancel',order_id='o1'))],
        patch=dict(entry_cancel_requested=True))
    assert len(claims)==1
    assert s.lp_session_revision(sid,trading=True)==before_trade+1
    assert s.lp_trade_generation()==before_generation+1
    assert s.lp_session(sid)['facts_error']=='trade_change_pending'
    assert s.lp_claim_trade_actions(
        sid,trade_revision=before_trade,trade_generation=before_generation,
        actions=[(f'{sid}:test-cancel:o2',dict(role='reconciliation_cancel',order_id='o2'))],
        patch=dict(entry_cancel_requested=True))==[]
    assert s.lp_session_revision(sid,trading=True)==before_trade+1
    assert s.lp_trade_generation()==before_generation+1


def test_concurrent_monitor_and_manual_protected_sell_claim_one_intent(tmp_path):
    from threading import Barrier, Event
    e,x,lp,s=setup(tmp_path)
    sid='sell-session'; now=NOW; request=_manual_request(now)
    s.lp_create_session(sid,sid,state='stop_loss_exit',payload={
        **request,'position_reconciled':True,'residual_quantity':Decimal('10'),
        'entry_order_id':'old-buy','owned_order_ids':['old-buy']})
    row=s.lp_session_with_revision(sid,trading=True)
    assert row is not None
    session,expected_revision=row
    generation=s.lp_trade_generation()
    snapshot=dict(book=dict(received_at=now,bids=[dict(price='.40',size='100')]))
    entered,release=Event(),Event()
    def sell(**kwargs):
        entered.set(); assert release.wait(5)
        return {'order_id':'sell-one','status':'LIVE'}
    x.submit_protected_sell=sell
    with ThreadPoolExecutor(2) as pool:
        sender=pool.submit(lp._submit_protected_exit,session,Decimal('10'),snapshot,
                           trade_generation=generation,
                           expected_trade_revision=expected_revision)
        assert entered.wait(5)
        stale=pool.submit(lp._submit_protected_exit,session,Decimal('10'),snapshot,
                          trade_generation=generation,
                          expected_trade_revision=expected_revision)
        try:
            assert stale.result(timeout=2) is None
        finally:
            release.set()
        sender.result(timeout=5)
    submits=[a for a in s.lp_actions(sid) if a.get('role')=='protected_exit']
    assert len(submits)==1 and submits[0]['state']=='accepted'
    assert submits[0]['order_id']=='sell-one'


def test_retry_plan_uses_actual_future_not_expired_previous(tmp_path):
    from open_trader.polymarket_lp_auto import _reconcile_retry_plan
    assert _reconcile_retry_plan('read_failed',NOW,previous=NOW+timedelta(seconds=120)) == (
        NOW+timedelta(seconds=120),'existing_plan')
    assert _reconcile_retry_plan('read_failed',NOW,previous=NOW-timedelta(seconds=120),
                                 scheduled_at=NOW+timedelta(seconds=30)) == (
        NOW+timedelta(seconds=30),'read_failed')
    fallback=_reconcile_retry_plan('read_failed',NOW,previous=NOW-timedelta(seconds=120))
    assert fallback==(NOW+timedelta(seconds=60),'fallback_minute')
    assert _reconcile_retry_plan('rate_limited',NOW,scheduled_at=NOW+timedelta(seconds=459)) == (
        NOW+timedelta(seconds=459),'rate_limited')


def test_local_trade_change_invalidates_retained_shared_facts(tmp_path):
    e,x,lp,s=setup(tmp_path,2)
    sid='session-a'; other='session-b'
    s.lp_create_session(sid,sid,state='entry_open',payload=dict(condition_id='a',token_id='a'))
    s.lp_create_session(other,other,state='entry_open',payload=dict(condition_id='b',token_id='b'))
    for _ in range(100):
        s.lp_register_trade_change(sid)
    snapshot={'account':{'checked_at':NOW}}
    generation=s.lp_trade_generation()
    lp._retain_pending_facts(s.lp_session(sid),snapshot,1,generation)
    assert lp._take_pending_facts(s.lp_session(sid),1,generation)==snapshot
    lp._retain_pending_facts(s.lp_session(sid),snapshot,1,generation)
    s.lp_register_trade_change(other)
    changed_generation=s.lp_trade_generation()
    assert changed_generation>generation
    assert lp._take_pending_facts(s.lp_session(sid),1,changed_generation) is None


def test_manual_stale_account_cannot_settle_zero_position(tmp_path):
    now=NOW
    class Exchange:
        config=SimpleNamespace(wallet_address='test-wallet')
        def lp_snapshot(self,request):
            d=dict(account=dict(authenticated=True,wallet_address='test-wallet',balance='100',allowance='100',
                 checked_at=now-timedelta(seconds=61),open_orders=[],positions=[],
                 open_orders_complete=True,positions_complete=True),
                 market=None,book=None,orders=[],trades=[],
                 market_read_errors={'m00':{'error_type':'OSError'}})
            return d
    store=PredictionArbitrageStore(tmp_path/'state.sqlite')
    exchange=Exchange()
    lp=PolymarketLPService(store,exchange,clock=lambda:now)
    engine=PredictionExecutionService(store=store,monitor=SimpleNamespace(),trading=exchange,
        notifier=SimpleNamespace(),lock_path=tmp_path/'execution.lock',lp=lp)
    request={**_manual_request(now),'entry_order_id':'o1','owned_order_ids':['o1'],
             'submit_status':'accepted'}
    store.lp_create_session('manual','manual',state='entry_open',payload=request)
    result=lp.reconcile_facts('manual')
    assert result[3]=='account_facts_stale'
    session=store.lp_session('manual')
    assert session['state']=='entry_open'
    assert session['facts_error']=='account_facts_stale'
    assert _maybe_decimal(session.get('reserved_usd')) == Decimal('4')


def test_session_missing_and_duplicate_identity_arm_attention(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    pool=e._auto_pool
    class Notifier:
        def __init__(self): self.calls=[]
        def notify(self,title,message): self.calls.append((title,message))
    notifier=Notifier(); e._notifier=notifier
    def add_missing(d):
        d['intents']['missing']={
            'intent_id':'missing','session_id':'absent','state':'active',
            'created_at':NOW.isoformat(),'reserved_usd':'0',
        }
    pool._update(add_missing)
    pool._reconcile_intent(pool._read()['intents']['missing'],reuse=False)
    intent=pool._read()['intents']['missing']
    assert intent['reconcile_reason']=='session_missing'
    assert intent['manual_attention'] is True
    assert intent['attention_since']==NOW.isoformat()

    pool._update(lambda d:d['intents'].pop('missing',None))
    row=e.lp_auto_run_once()['intents'][0]
    sid=row['session_id']; intent_id=row['intent_id']
    session=s.lp_session(sid)
    assert session is not None and session.get('entry_order_id')
    def add_duplicate(d):
        other=dict(d['intents'][intent_id]); other['intent_id']='other'
        other['order_id']=session['entry_order_id']
        d['intents']['other']=other
    pool._update(add_duplicate)
    pool._record_session(intent_id,s.lp_session(sid))
    intents=pool._read()['intents']
    assert intents[intent_id]['reconcile_reason']=='duplicate_order_identity'
    assert intents[intent_id]['manual_attention'] is True
    assert intents[intent_id]['attention_since']==NOW.isoformat()


def test_reconciliation_attention_notifies_once_then_recovery_once(tmp_path, monkeypatch):
    from tests import test_lp_auto_pool as venue
    from copy import deepcopy

    class Notifier:
        def __init__(self): self.calls=[]
        def notify(self,title,message): self.calls.append((title,message))

    e,x,lp,s=setup(tmp_path)
    notifier=Notifier()
    e._notifier=notifier
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    r=e.lp_auto_run_once()
    intent_id=r['intents'][0]['intent_id']
    sid=r['intents'][0]['session_id']
    read=x.lp_snapshot
    base_now=NOW
    monkeypatch.setattr(venue,'NOW',base_now)
    lp.clock=lambda:venue.NOW
    x.lp_snapshot=lambda request: (_ for _ in ()).throw(OSError('account unavailable'))
    e.lp_auto_reconcile_unknown()
    d=e._auto_pool._read()['intents'][intent_id]
    assert d['attention_since'] and d['attention_due'] is False
    assert not d.get('attention_notified') and not notifier.calls

    monkeypatch.setattr(venue,'NOW',base_now+timedelta(seconds=301))
    e.lp_auto_reconcile_unknown()
    thread = lp._attention_thread
    if thread is not None:
        thread.join(timeout=2)
    d=e._auto_pool._read()['intents'][intent_id]
    assert d['attention_due'] is False
    assert d['attention_notified'] is True and len(notifier.calls)==1

    monkeypatch.setattr(venue,'NOW',base_now+timedelta(seconds=302))
    e.lp_auto_reconcile_unknown()
    assert len(notifier.calls)==1, 'a continuing episode must not repeat'

    def fresh_read(request):
        snapshot=deepcopy(read(request))
        snapshot['account']['checked_at']=venue.NOW
        return snapshot

    recovery_baseline=base_now+timedelta(seconds=302)
    monkeypatch.setattr(venue,'NOW',recovery_baseline)
    x.lp_snapshot=fresh_read
    e.lp_auto_reconcile_unknown()
    thread = lp._attention_thread
    if thread is not None:
        thread.join(timeout=2)
    d=e._auto_pool._read()['intents'][intent_id]
    assert d['financial_status']=='known'
    assert d['attention_recovery_due'] is True
    assert d['attention_recovery_ready_since']==recovery_baseline.isoformat()
    assert len(notifier.calls)==1

    recovery_now=base_now+timedelta(seconds=362)
    monkeypatch.setattr(venue,'NOW',recovery_now)
    e.lp_auto_reconcile_unknown()
    thread = lp._attention_thread
    if thread is not None:
        thread.join(timeout=2)
    d=e._auto_pool._read()['intents'][intent_id]
    assert d['financial_status']=='known'
    assert not d.get('attention_since') and not d.get('attention_notified')
    assert len(notifier.calls)==2
    title, message = notifier.calls[1]
    assert title.startswith('LP 标的资金核对恢复 · ')
    assert '本次 标的资金核对恢复：1 个市场。' in message


def test_reconciliation_attention_send_failure_retries_then_recovers(tmp_path, monkeypatch):
    from tests import test_lp_auto_pool as venue
    from copy import deepcopy

    class Notifier:
        def __init__(self): self.calls=[]; self.fail=True
        def notify(self,title,message):
            self.calls.append((title,message))
            if self.fail: raise RuntimeError('channel unavailable')

    e,x,lp,s=setup(tmp_path)
    notifier=Notifier(); e._notifier=notifier
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    r=e.lp_auto_run_once(); intent_id=r['intents'][0]['intent_id']
    read=x.lp_snapshot; base_now=NOW
    monkeypatch.setattr(venue,'NOW',base_now); lp.clock=lambda:venue.NOW
    fail=lambda request: (_ for _ in ()).throw(OSError('account unavailable'))
    x.lp_snapshot=fail

    def wait_for_attention():
        thread=lp._attention_thread
        if thread is not None:
            thread.join(timeout=2)

    e.lp_auto_reconcile_unknown()
    monkeypatch.setattr(venue,'NOW',base_now+timedelta(seconds=301))
    e.lp_auto_reconcile_unknown(); wait_for_attention()
    d=e._auto_pool._read()['intents'][intent_id]
    assert d['attention_notified'] is False and d['attention_due'] is True
    assert d['attention_send_error']=='notification_delivery_failed'
    assert len(notifier.calls)==1

    monkeypatch.setattr(venue,'NOW',base_now+timedelta(seconds=302))
    e.lp_auto_reconcile_unknown(); wait_for_attention()
    assert len(notifier.calls)==1

    notifier.fail=False
    monkeypatch.setattr(venue,'NOW',base_now+timedelta(seconds=362))
    e.lp_auto_reconcile_unknown(); wait_for_attention()
    d=e._auto_pool._read()['intents'][intent_id]
    assert d['attention_notified'] is True and d['attention_due'] is False
    assert len(notifier.calls)==2

    monkeypatch.setattr(venue,'NOW',base_now+timedelta(seconds=363))
    e.lp_auto_reconcile_unknown(); wait_for_attention()
    assert len(notifier.calls)==2

    def fresh_read(request):
        snapshot=deepcopy(read(request)); snapshot['account']['checked_at']=venue.NOW
        return snapshot

    recovery_baseline=base_now+timedelta(seconds=364)
    monkeypatch.setattr(venue,'NOW',recovery_baseline)
    x.lp_snapshot=fresh_read
    e.lp_auto_reconcile_unknown(); wait_for_attention()
    d=e._auto_pool._read()['intents'][intent_id]
    assert d['financial_status']=='known'
    assert d['attention_recovery_due'] is True
    assert d['attention_recovery_ready_since']==recovery_baseline.isoformat()
    assert len(notifier.calls)==2

    recovery_now=base_now+timedelta(seconds=424)
    monkeypatch.setattr(venue,'NOW',recovery_now)
    e.lp_auto_reconcile_unknown(); wait_for_attention()
    d=e._auto_pool._read()['intents'][intent_id]
    assert d['financial_status']=='known' and not d.get('attention_since')
    assert len(notifier.calls)==3
    assert notifier.calls[2][0].startswith('LP 标的资金核对恢复 · ')


def test_blocked_recovery_callback_cannot_overwrite_new_fault_episode(tmp_path, monkeypatch):
    from tests import test_lp_auto_pool as venue
    from copy import deepcopy

    e,x,lp,s=setup(tmp_path)
    entered,release=Event(),Event()
    class Notifier:
        def __init__(self): self.calls=[]
        def notify(self,title,message):
            if '恢复' in title:
                entered.set()
                assert release.wait(5)
            self.calls.append((title,message))
    e._notifier=Notifier()
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    r=e.lp_auto_run_once(); intent_id=r['intents'][0]['intent_id']
    read=x.lp_snapshot; base_now=NOW
    monkeypatch.setattr(venue,'NOW',base_now); lp.clock=lambda:venue.NOW
    fail=lambda request: (_ for _ in ()).throw(OSError('account unavailable'))
    x.lp_snapshot=fail
    e.lp_auto_reconcile_unknown()
    monkeypatch.setattr(venue,'NOW',base_now+timedelta(seconds=301))
    e.lp_auto_reconcile_unknown()
    thread=lp._attention_thread
    if thread is not None: thread.join(timeout=2)
    assert len(e._notifier.calls)==1

    def fresh_read(request):
        snapshot=deepcopy(read(request)); snapshot['account']['checked_at']=venue.NOW
        return snapshot

    recovery_baseline=base_now+timedelta(seconds=302)
    monkeypatch.setattr(venue,'NOW',recovery_baseline)
    x.lp_snapshot=fresh_read
    e.lp_auto_reconcile_unknown()
    thread=lp._attention_thread
    if thread is not None: thread.join(timeout=2)
    assert e._auto_pool._read()['intents'][intent_id]['attention_recovery_ready_since'] == recovery_baseline.isoformat()
    assert not entered.is_set()

    recovery_now=base_now+timedelta(seconds=362)
    monkeypatch.setattr(venue,'NOW',recovery_now)
    e.lp_auto_reconcile_unknown()
    assert entered.wait(2)

    monkeypatch.setattr(venue,'NOW',base_now+timedelta(seconds=363))
    x.lp_snapshot=fail
    e.lp_auto_reconcile_unknown()
    current=e._auto_pool._read()['intents'][intent_id]
    assert current['financial_status']=='unknown'
    assert not current.get('attention_recovery_due')
    assert current['attention_since']==(base_now+timedelta(seconds=363)).isoformat()
    assert current.get('attention_notified') is False

    thread=lp._attention_thread
    release.set()
    if thread is not None: thread.join(timeout=2)
    after=e._auto_pool._read()['intents'][intent_id]
    assert after['financial_status']=='unknown'
    assert after.get('attention_since')
    assert not after.get('attention_recovery_due')

    monkeypatch.setattr(venue,'NOW',base_now+timedelta(seconds=663))
    e.lp_auto_reconcile_unknown()
    thread=lp._attention_thread
    if thread is not None: thread.join(timeout=2)
    final=e._auto_pool._read()['intents'][intent_id]
    titles = [title for title,message in e._notifier.calls]
    assert titles[0] == 'LP 核对持续失败'
    assert titles[1].startswith('LP 标的资金核对恢复 · ')
    assert titles[2] == 'LP 核对持续失败'
    assert final['attention_since']==(base_now+timedelta(seconds=363)).isoformat()
    assert final['attention_notified'] is True and final['attention_due'] is False

    monkeypatch.setattr(venue,'NOW',base_now+timedelta(seconds=664))
    e.lp_auto_reconcile_unknown()
    thread=lp._attention_thread
    if thread is not None: thread.join(timeout=2)
    assert len(e._notifier.calls)==3


def test_partial_fault_recovery_gives_new_fault_its_own_episode(
    tmp_path, monkeypatch
):
    from copy import deepcopy

    from open_trader.notifications import (
        CompositeNotifier,
        FeishuWebhookNotifier,
        XiaoaiSSHNotifier,
    )

    class Feishu(FeishuWebhookNotifier):
        def __init__(self, calls):
            self.calls = calls

        def notify(self, title, message):
            self.calls.append(title)

    class Xiaoai(XiaoaiSSHNotifier):
        def __init__(self, calls):
            super().__init__(host="fake", ssh_key=Path("/tmp/key"))
            self.calls = calls
            self.fail = True

        def notify(self, title, message):
            if self.fail:
                raise RuntimeError("voice channel unavailable")
            self.calls.append(title)

    e, x, lp, s = setup(tmp_path)
    calls: list[str] = []
    voice = Xiaoai(calls)
    e._notifier = CompositeNotifier([Feishu(calls), voice])
    e.lp_auto_configure(dict(budget_usd="100", target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    row = e.lp_auto_run_once()
    intent_id = row["intents"][0]["intent_id"]
    read = x.lp_snapshot
    base_now = NOW
    from tests import test_lp_auto_pool as venue

    monkeypatch.setattr(venue, "NOW", base_now)
    lp.clock = lambda: venue.NOW

    def wait_for_calls(count):
        thread = lp._attention_thread
        if thread is not None:
            thread.join(timeout=2)
        assert len(calls) == count

    def failed_read(_request):
        raise OSError("account unavailable")

    x.lp_snapshot = failed_read
    e.lp_auto_reconcile_unknown()
    monkeypatch.setattr(venue, "NOW", base_now + timedelta(seconds=300))
    e.lp_auto_reconcile_unknown()
    wait_for_calls(1)
    current = e._auto_pool._read()["intents"][intent_id]
    assert current["attention_notified"] is False
    assert current["attention_delivered_channels"] == ["feishu"]
    assert calls == ["LP 核对持续失败"]

    voice.fail = False
    def fresh_read(request):
        snapshot = deepcopy(read(request))
        snapshot["account"]["checked_at"] = venue.NOW
        return snapshot

    recovery_baseline = base_now + timedelta(seconds=301)
    monkeypatch.setattr(venue, "NOW", recovery_baseline)
    x.lp_snapshot = fresh_read
    e.lp_auto_reconcile_unknown()
    wait_for_calls(1)
    baseline = e._auto_pool._read()["intents"][intent_id]
    assert baseline["financial_status"] == "known"
    assert baseline["attention_recovery_due"] is True

    recovery_now = base_now + timedelta(seconds=361)
    monkeypatch.setattr(venue, "NOW", recovery_now)
    e.lp_auto_reconcile_unknown()
    wait_for_calls(2)
    recovered = e._auto_pool._read()["intents"][intent_id]
    assert recovered["financial_status"] == "known"
    assert not recovered.get("attention_since")
    assert calls[0] == "LP 核对持续失败"
    assert calls[1].startswith("LP 标的资金核对恢复 · ")

    monkeypatch.setattr(venue, "NOW", base_now + timedelta(seconds=363))
    lp.clock = lambda: venue.NOW
    x.lp_snapshot = failed_read
    e.lp_auto_reconcile_unknown()
    renewed = e._auto_pool._read()["intents"][intent_id]
    assert renewed["attention_since"] == (
        base_now + timedelta(seconds=363)
    ).isoformat()
    assert renewed["attention_notified"] is False

    monkeypatch.setattr(venue, "NOW", base_now + timedelta(seconds=662))
    e.lp_auto_reconcile_unknown()
    assert calls == ["LP 核对持续失败", calls[1]]

    monkeypatch.setattr(venue, "NOW", base_now + timedelta(seconds=663))
    e.lp_auto_reconcile_unknown()
    wait_for_calls(4)
    final = e._auto_pool._read()["intents"][intent_id]
    assert calls[2:] == [
        "LP 核对持续失败",  # New episode reaches both channels once.
        "LP 核对持续失败",
    ]
    assert final["attention_since"] == (
        base_now + timedelta(seconds=363)
    ).isoformat()
    assert final["attention_notified"] is True


def test_attention_worker_exception_is_restartable(tmp_path, monkeypatch):
    from tests import test_lp_auto_pool as venue

    e,x,lp,s=setup(tmp_path)
    calls=[]
    class Notifier:
        def notify(self,title,message): calls.append((title,message))
    e._notifier=Notifier()
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    r=e.lp_auto_run_once(); row=r['intents'][0]
    startup = lp._attention_thread
    if startup is not None:
        startup.join(timeout=2)
        assert not startup.is_alive()
    assert lp._attention_thread is None
    pool=e._auto_pool
    pool._update(lambda d:d['intents'][row['intent_id']].update(
        attention_since=(NOW-timedelta(seconds=301)).isoformat(),
        attention_due=True,reconcile_error='account_unavailable'))
    original=pool._read; failures=[True]
    def broken_read():
        if failures:
            failures.pop(); raise RuntimeError('transient ledger read')
        return original()
    monkeypatch.setattr(pool,'_read',broken_read)
    lp._schedule_session_attention(row['session_id'])
    thread=lp._attention_thread
    if thread is not None: thread.join(timeout=2)
    assert calls==[] and lp._attention_thread is None

    lp._schedule_session_attention(row['session_id'])
    thread=lp._attention_thread
    if thread is not None: thread.join(timeout=2)
    assert len(calls)==1


def test_session_progress_uses_exact_hidden_ledger_reservation(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    row=e.lp_auto_run_once()['intents'][0]
    progress=e._lp_session_progress(s.lp_session(row['session_id']))
    assert Decimal(progress['reserved_usd'])==Decimal(row['reserved_usd'])==Decimal('8')
    assert progress['reason'] is None
    assert progress['next_action']=='review_deadline'
    assert progress['manual_attention'] is False
    projected=e.lp_auto_state(include_intents=False)
    assert 'intents' not in projected


@pytest.mark.parametrize('partial', [False, True])
def test_recovery_during_fault_send_delivers_recovery_to_successful_channels(tmp_path, monkeypatch, partial):
    from copy import deepcopy
    from open_trader.notifications import CompositeNotifier, FeishuWebhookNotifier, XiaoaiSSHNotifier
    from tests import test_lp_auto_pool as venue

    engine, exchange, lp, _ = setup(tmp_path)
    entered, release = Event(), Event()
    calls = []

    class Feishu(FeishuWebhookNotifier):
        def __init__(self): pass
        def notify(self, title, message):
            if title == 'LP 核对持续失败':
                entered.set()
                assert release.wait(5)
            calls.append(('feishu', title))

    class Voice(XiaoaiSSHNotifier):
        def __init__(self): pass
        def notify(self, title, message):
            if partial:
                raise RuntimeError('voice unavailable')
            calls.append(('xiaoai', title))

    engine._notifier = CompositeNotifier([Feishu(), Voice()])
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    engine.lp_auto_set_desired_running(True)
    intent_id = engine.lp_auto_run_once()['intents'][0]['intent_id']
    read = exchange.lp_snapshot
    base = venue.NOW
    lp.clock = lambda: venue.NOW
    exchange.lp_snapshot = lambda request: (_ for _ in ()).throw(OSError('account unavailable'))
    engine.lp_auto_reconcile_unknown()
    monkeypatch.setattr(venue, 'NOW', base + timedelta(seconds=300))
    engine.lp_auto_reconcile_unknown()
    try:
        assert entered.wait(2)
        monkeypatch.setattr(venue, 'NOW', base + timedelta(seconds=301))
        def recovered_snapshot(request):
            snapshot = deepcopy(read(request))
            snapshot['account']['checked_at'] = venue.NOW
            return snapshot
        exchange.lp_snapshot = recovered_snapshot
        engine.lp_auto_reconcile_unknown()
        assert engine._auto_pool._read()['intents'][intent_id]['financial_status'] == 'known'
        assert not any('恢复' in title for _, title in calls)
    finally:
        release.set()
        thread = lp._attention_thread
        if thread is not None:
            thread.join(timeout=3)

    monkeypatch.setattr(venue, 'NOW', base + timedelta(seconds=361))
    engine.lp_auto_reconcile_unknown()
    thread = lp._attention_thread
    if thread is not None:
        thread.join(timeout=3)
    channels = ['feishu'] if partial else ['feishu', 'xiaoai']
    recovery_title = calls[len(channels)][1]
    assert recovery_title.startswith('LP 标的资金核对恢复 · ')
    assert calls == (
        [(channel, 'LP 核对持续失败') for channel in channels]
        + [(channel, recovery_title) for channel in channels]
    )
    intent = engine._auto_pool._read()['intents'][intent_id]
    assert not intent.get('attention_due') and not intent.get('attention_recovery_due')
