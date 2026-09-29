from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from open_trader.prediction_arbitrage_execution import PredictionExecutionService

NOW = datetime(2026, 9, 27, 8, tzinfo=UTC)


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


def test_pause_during_signing_prevents_post(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    x.before_sign=lambda:e.lp_auto_set_desired_running(False)
    r=e.lp_auto_run_once()
    assert not x.posts
    assert r['pause_confirmed'] is True
    assert r['slots']['occupied']==0


def test_concurrent_rounds_single_reservation(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _:e.lp_auto_run_once(round_id='same'), range(2)))
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


def test_recycled_pnl_partial_sale_late_stream_and_restart(tmp_path):
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
    assert Decimal(r['funds']['available_usd'])==94
    x.orders[1].update(status='FILLED',size_matched='20')
    x.positions=[]
    r=e.lp_auto_reconcile_unknown()
    assert Decimal(r['funds']['total_usd'])==97
    assert Decimal(r['funds']['available_usd'])==97
    x.trades=[dict(trade_id=f't{side}',status='CONFIRMED',matched_at=NOW.isoformat(),
              maker_orders=[dict(order_id=oid,token_id='m00',side=side,matched_amount='20',price=price,fee='0')])
              for side,oid,price in [('BUY','o1','.40'),('SELL','sell1','.25')]]
    s.lp_update_session(sid,patch=dict(facts_checked_at=NOW-timedelta(seconds=301)))
    e.lp_generate_due_auto_reports()
    e.lp_generate_due_auto_reports()
    r=e.lp_auto_state()
    assert Decimal(r['funds']['total_usd'])==97
    fills=[f for f in e.lp_auto_report_facts()['events'] if f['kind']=='fill']
    assert len(fills)==2
    assert all(f['occurred_at'] for f in fills)
    e.lp_auto_set_desired_running(False)
    assert e.lp_auto_configure(dict(budget_usd='120',target_buy_count=1))['funds']['total_usd']=='120.00'


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


def test_concurrent_configuration_is_atomic_target_total(tmp_path):
    e,x,lp,s=setup(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _:e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1)),range(2)))
    assert e.lp_auto_state()['funds']['total_usd']=='100'
    assert e.lp_auto_state()['config_version']==2


def test_unknown_with_reliable_id_reconciles_without_resubmit(tmp_path):
    e,x,lp,s=setup(tmp_path,2)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=2))
    e.lp_auto_set_desired_running(True)
    create=x.lp_create_limit_order
    x.lp_create_limit_order=lambda **kwargs:{**create(**kwargs),'order_id':'known-' + kwargs['token_id']}
    x.fail=True
    r=e.lp_auto_run_once()
    assert len(x.posts)==2
    assert 'submission_unknown' in r['block_reasons']
    e.lp_auto_set_desired_running(False)
    for token in ('m00', 'm01'):
        x.orders.append(dict(order_id='known-' + token,token_id=token,condition_id=token,side='BUY',status='LIVE',price='.4',original_size='20',size_matched='0'))
    x.fail=False
    r=e.lp_auto_run_once()
    assert 'submission_unknown' not in r['block_reasons']
    assert r['desired_running'] is False
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


def test_automatic_ui_escapes_identity_and_distinguishes_no_order_id():
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
    const html=lpAutoFundsAndOrders({target_buy_count:5,slots:{active:3,pending:2,pending_review:2,occupied:5},funds:{status:'unknown'},intents:[{intent_id:'<script>',condition_id:'<img>',state:'unknown'}]});
    if(html.includes('<script>')||html.includes('<img>')||!html.includes('无可靠订单 ID')||!html.includes('UNKNOWN')||!html.includes('自动'))process.exit(1);
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
    from threading import Event
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


def test_late_live_receipt_preserves_already_verified_fill(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    create=x.lp_create_limit_order
    x.lp_create_limit_order=lambda **kwargs:{**create(**kwargs),'order_id':'o1'}
    post=x.lp_post_order
    def delayed_receipt(signed):
        response=dict(post(signed))
        x.orders[0].update(status='FILLED',size_matched='20')
        x.positions=[dict(token_id='m00',condition_id='m00',size='20')]
        e.lp_tick()
        return response
    x.lp_post_order=delayed_receipt
    r=e.lp_auto_run_once()
    session=s.lp_session(r['intents'][0]['session_id'])
    assert session['order_history']['o1']['status']=='FILLED'
    assert r['funds']['status']=='unknown'  # A SELL changed the trading generation before the late BUY reply.
    assert r['slots']['occupied']==1
    assert Decimal(r['funds']['inventory_cost_usd'])==8
    recovered=e.lp_auto_reconcile_unknown()
    assert recovered['funds']['status']=='known'
    assert recovered['slots']['occupied']==0
    assert Decimal(recovered['funds']['inventory_cost_usd'])==8


@pytest.mark.parametrize('signed_id',[False,True])
def test_receipt_apply_respects_another_service_tick_lock(tmp_path,signed_id):
    from threading import Event, Lock
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


def test_conflicting_signed_and_receipt_ids_remain_unknown(tmp_path):
    e,x,lp,s=setup(tmp_path,2)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=2))
    e.lp_auto_set_desired_running(True)
    create=x.lp_create_limit_order
    x.lp_create_limit_order=lambda **kwargs:{**create(**kwargs),'order_id':'signed-id'}
    r=e.lp_auto_run_once()
    assert 'submission_unknown' in r['block_reasons']
    assert r['funds']['available_usd'] is None
    assert len(x.posts)==1
    session=s.lp_session(r['intents'][0]['session_id'])
    assert session['order_identity_conflict']=={'prepared_order_id':'signed-id','response_order_id':'o1'}
    assert 'o1' not in session['owned_order_ids']
    x.orders.append(dict(order_id='signed-id',token_id='m00',condition_id='m00',side='BUY',status='LIVE',price='.4',original_size='20',size_matched='0'))
    assert 'submission_unknown' in e.lp_auto_reconcile_unknown()['block_reasons']
    e.lp_auto_run_once()
    assert len(x.posts)==1


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


def test_conflicting_identity_stays_quarantined_in_public_tick_and_stop(tmp_path):
    e,x,lp,s=setup(tmp_path)
    e.lp_auto_configure(dict(budget_usd='100',target_buy_count=1))
    e.lp_auto_set_desired_running(True)
    create=x.lp_create_limit_order
    x.lp_create_limit_order=lambda **kwargs:{**create(**kwargs),'order_id':'prepared-id'}
    sid=e.lp_auto_run_once()['intents'][0]['session_id']
    cancellations=[]
    x.cancel_order=lambda oid:cancellations.append(oid) or {'canceled':[oid]}
    assert e.lp_tick()['state']=='needs_attention'
    x.orders=[dict(order_id='prepared-id',token_id='m00',condition_id='m00',side='BUY',status='LIVE',price='.4',original_size='20',size_matched='0')]
    original=x.direction
    def depleted(n):
        d=original(n)
        d['book']['bids'][0]['size']='20'
        return d
    x.direction=depleted
    assert e.lp_tick()['state']=='needs_attention'
    assert s.lp_session(sid)['reconciliation']=='order_identity_conflict'
    assert e.lp_stop(sid)['state']=='needs_attention'
    assert not cancellations and len(x.posts)==1
