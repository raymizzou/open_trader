"""Approved P2 public-seam contracts. All trading boundaries remain offline."""
from decimal import Decimal

import pytest


def test_independent_pool_settings_preserve_old_budget_and_control(tmp_path, monkeypatch):
    from tests.test_lp_d2_execution_bridge import bridge_runtime, attach
    from tests.test_lp_d2_integration import settle_control
    live, producer, sdk, public, now = bridge_runtime(tmp_path, monkeypatch, target_count=3)
    try:
        execution = attach(live, producer, tmp_path)
        live.request_running(False)
        settle_control(live)
        for version in range(1, 7):
            live.store.lp_save_current_settings(live._settings_account(), expected_config_version=version,
                changes=dict(order_check_interval_seconds=10))
        live.load_settings()
        live.run_once(producer.capture_qualified_pool())
        old = execution.lp_auto_state()
        assert old['budget_usd'] == '40' and old['config_version'] == 7
        assert old.get('allocation_mode', 'minimum') == 'minimum'
        execution.lp_auto_configure(dict(income_budget_usd='100', expected_config_version=7))
        live._settings_thread.join(timeout=3)
        assert not live._settings_thread.is_alive()
        state = execution.lp_auto_state()
        assert state['allocation_mode'] == 'observed_pools'
        assert state['trial_budget_usd'] == state['budget_usd'] == '40'
        assert state['income_budget_usd'] == '100'
        assert state['trial_target_buy_count'] == state['income_target_buy_count'] == 3
        assert state['income_weights'] == ['0.5', '0.3', '0.2']
        assert state['desired_running'] is state['effective_running'] is False
        before = live.store.lp_load_current_settings(live._settings_account())
        for bad in (dict(income_budget_usd='-1'), dict(trial_budget_usd='NaN'),
                    dict(budget_usd='40', trial_budget_usd='41'),
                    dict(income_budget_usd='200', expected_config_version=7)):
            with pytest.raises(ValueError):
                execution.lp_auto_configure({'expected_config_version': 8, **bad})
            assert live.store.lp_load_current_settings(live._settings_account()) == before
        assert sdk.posts == sdk.cancels == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_trial_pool_keeps_model_ranking_minimum_sizes_and_three_slots(tmp_path):
    from datetime import UTC, datetime
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    from open_trader.polymarket_lp_market_reader import SourceResult
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from tests.test_lp_d2_integration import seed_running
    from tests.test_lp_order_registration_contract import _adapter, WALLET
    from tests.test_polymarket_lp_risk import _direction
    import hashlib

    adapter, sdk = _adapter(tmp_path)
    store = PredictionArbitrageStore(tmp_path)
    account = hashlib.sha256(WALLET.encode()).hexdigest()
    store.lp_save_current_settings(account, expected_config_version=0,
        changes=dict(trial_budget_usd='20', income_budget_usd='100'))
    seed_running(store)
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='observed-trial', policy=CalculationPolicy(None), load_settings_on_start=True)
    sdk.create_limit_order = lambda **kw: dict(kw, order_type='GTD')
    def post(signed):
        sdk.posts.append(signed)
        return dict(accepted=True, order_id='trial-'+signed['token_id'], status='LIVE', size_matched='0')
    sdk.post_order = post
    now = datetime.now(UTC)
    rows = []
    for name, capital, pool in [('A','6','1200'),('B','15','600'),('C','4','240'),('D','1','40')]:
        quantity = Decimal(capital)/Decimal('.4')
        direction = _direction(reward_min_size=str(quantity), minimum_order_size=str(quantity))
        direction['market'].update(condition_id='c'+name, token_id='t'+name,
            metadata_checked_at=now, fees_checked_at=now)
        direction['book'].update(condition_id='c'+name, token_id='t'+name, received_at=now,
            bids=[dict(price='.4',size='1000')], asks=[dict(price='.42',size='1000')])
        direction.update(daily_pool_usd=Decimal(pool), reward_checked_at=now)
        rows.append(dict(condition_id='c'+name, token_id='t'+name, market_id='m'+name,
            venue='polymarket', price=Decimal('.4'), quantity=quantity, direction=direction,
            order_type='GTD', expiration=1893456000, post_only=True, send_options=()))
    capture = SourceResult('offline-model-ranked', rows, checked_at=now)
    try:
        live.initialize(ValidatedStartAdmission('observed-trial', 'offline-permit'))
        live.run_once()  # one shared account startup cleanup
        result = live.run_once(capture,pool='trial')
        assert [r['token_id'] for r in sdk.posts] == ['tA','tC','tD']
        assert [r['price']*r['size'] for r in sdk.posts] == [Decimal('6'),Decimal('4'),Decimal('1')]
        assert len(result.outcomes) == 3
        assert all(o.status.value == 'accepted' for o in result.outcomes)
        assert sdk.cancels == []  # no official result is not a trial cancellation reason
    finally:
        assert live.close(timeout=3)
        adapter.close()


def _measured_service(tmp_path, monkeypatch, *, variant='valid'):
    """Real adapter/Store/LP composition, controlled external SDK/HTTP only."""
    from tests.test_prediction_service import (_lp_reward_dashboard_service,
        _lp_reward_dashboard_state, _lp_reward_order)
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader import polymarket_trading
    monkeypatch.setattr(polymarket_trading, 'signature_type_for', lambda wallet_type: 0)
    state = _lp_reward_dashboard_state(earning_percentage='0' if variant == 'zero' else '100')
    state['reward_markets'][0]['rewards_config'][0]['rate_per_day'] = Decimal('.24')
    state['orders'] = [_lp_reward_order('trial-D', remaining=Decimal('20'), matched=Decimal('0'))]
    if variant == 'missing':
        state['market_missing'] = True
    elif variant == 'wrong_order':
        state['orders'][0]['id'] = ''
    elif variant == 'wrong_terms':
        state['orders'][0]['price'] = Decimal('-.5')
    elif variant == 'mixed':
        state['orders'].append(_lp_reward_order('other-D', remaining=Decimal('20'), matched=Decimal('0')))
    elif variant == 'inventory':
        state['positions'] = [dict(condition_id='condition-1', token_id='yes-token',
            size=Decimal('30'), average_price=Decimal('.5'), outcome='YES')]
    service = _lp_reward_dashboard_service(tmp_path, state)
    service._lp = PolymarketLPService(service._store, service._trading)
    return service, state


def test_trial_result_uses_official_reward_and_exact_measured_buy(tmp_path, monkeypatch):
    for variant in ('valid','zero','missing','wrong_order','wrong_terms','mixed','inventory'):
        service, state = _measured_service(tmp_path/variant, monkeypatch, variant=variant)
        try:
            refreshed = service.refresh_lp_observations()
            row = refreshed['observations'].get('condition-1', {})
            result = row.get('pool_result')
            if variant in ('wrong_order', 'wrong_terms') and result is None:
                # The real account adapter rejects these invalid facts before
                # observation projection. UNKNOWN cannot manufacture a result.
                assert refreshed['state'] == 'unknown'
                assert not row or row.get('state') == 'unknown'
                continue
            assert result is not None, (variant, refreshed)
            if variant in ('valid','zero','inventory'):
                assert result['state'] == 'known', (variant, result)
                assert Decimal(result['yield_pct_per_hour']) == Decimal('0' if variant == 'zero' else '.1')
                assert Decimal(result['buy_capital_usd']) == 10
                assert Decimal(result['hourly_reward_usd']) == Decimal('0' if variant == 'zero' else '.01')
                assert result['source_order']['order_id'] == 'trial-D'
                assert result['source_order']['token_id'] == 'yes-token'
                assert result['source_order']['side'] == 'BUY'
                assert Decimal(result['source_order']['price']) == Decimal('.5')
                assert Decimal(result['source_order']['remaining_quantity']) == 20
                assert result['measured_at'] and result['reward_checked_at']
                assert result['basis'] == 'official_hourly_reward_per_measured_buy'
            else:
                assert result['state'] == 'unknown', (variant, result)
                assert result.get('yield_pct_per_hour') is None
        finally:
            service._trading.close()


def test_trial_result_survives_order_removal_until_120_seconds(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from open_trader import prediction_arbitrage_execution as execution_module
    from open_trader.polymarket_lp_observed_pools import select_income_results
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    service, external = _measured_service(tmp_path, monkeypatch)
    try:
        first = service.refresh_lp_observations()['observations']['condition-1']['pool_result']
        service.refresh_lp_share_watch()  # sibling publisher must preserve sample identity/time
        assert service.lp_observed_pool_state()['results']['condition-1']['result'] == first
        t0 = datetime.fromisoformat(first['measured_at'].replace('Z','+00:00'))
        clock = [t0 + timedelta(seconds=1)]
        monkeypatch.setattr(execution_module, '_utc_now', lambda: clock[0])
        external['orders'] = []  # authoritative account removal, not cancel ACK
        assert service._store.lp_advance_trade_generation(service._store.lp_trade_generation())
        flat = service.refresh_lp_observations()
        assert flat['observations']['condition-1']['stage'] == 'flat'
        at120 = service.lp_observed_pool_state(as_of=t0+timedelta(seconds=120))
        saved = at120['results']['condition-1']
        assert saved['eligible'] is True and saved['result'] == first
        assert select_income_results(at120['results'])['members'] == ('condition-1',)
        after = service.lp_observed_pool_state(as_of=t0+timedelta(seconds=120,microseconds=1))
        assert after['results']['condition-1']['eligible'] is False
        assert select_income_results(after['results'])['members'] == ()
        assert select_income_results(after['results'], members=('condition-1',))['cancel_members'] == ()
        # A new Store object reads the same detached original result. Neither
        # read/copy nor process restart grants new measured time.
        reopened = PredictionArbitrageStore(tmp_path/'data')
        original = reopened.lp_observations(service._lp_account_id())['condition-1']['pool_result']
        assert original == first
        assert service.lp_observed_pool_state(as_of=t0+timedelta(seconds=120)) == at120
        external['orders'] = [dict(id='trial-D-new', market='condition-1', asset_id='yes-token',
            side='BUY', status='LIVE', price=Decimal('.5'), original_size=Decimal('20'),
            size_matched=Decimal('0'), remaining_size=Decimal('20'))]
        external['reward_markets'][0]['rewards_config'][0]['rate_per_day'] = Decimal('.12')
        assert service._store.lp_advance_trade_generation(service._store.lp_trade_generation())
        clock[0] = t0 + timedelta(seconds=2)
        lower = service.refresh_lp_observations()['observations']['condition-1']['pool_result']
        assert Decimal(lower['yield_pct_per_hour']) == Decimal('.05')
        assert lower['source_order']['order_id'] == 'trial-D-new'
        assert lower['measured_at'] != first['measured_at']
        assert service.lp_observed_pool_state(as_of=clock[0])['results']['condition-1']['result'] == lower
    finally:
        service._trading.close()


class PoolRig:
    """One real D2/Store/adapter, with multi-market SDK/HTTP fixtures."""
    def __init__(self, tmp_path, monkeypatch, *, trial='20', income='100', wallet='200', clock=None):
        from datetime import UTC, datetime
        from types import SimpleNamespace
        from tests.test_lp_order_registration_contract import _adapter, _SDKPublicClient, WALLET
        from tests.test_lp_d2_integration import seed_running
        from tests.test_lp_d2_execution_bridge import attach
        from tests.test_prediction_service import _lp_reward_market_row
        from open_trader.polymarket_lp import PolymarketLPService
        from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
        from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
        from open_trader.polymarket_lp_market_calculation import CalculationPolicy
        from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
        from open_trader import polymarket_trading
        import hashlib
        self.now = clock() if clock else datetime.now(UTC)
        self.markets, self.hourly = {}, {}
        self.wallet, self.allowance = Decimal(wallet), Decimal(wallet)
        self.cancel_removes = False
        self.counter = 0
        self.history = {}
        rig = self
        base = _SDKPublicClient(self.now)
        class Public:
            def get_market(self, *, id):
                name = id.removeprefix('m')
                market = base.get_market(id='market-1')
                q = rig.markets[name]['quantity']
                return market.model_copy(update=dict(id=id, condition_id='c'+name,
                    outcomes=market.outcomes.model_copy(update=dict(
                        yes=market.outcomes.yes.model_copy(update=dict(token_id='t'+name)),
                        no=market.outcomes.no.model_copy(update=dict(token_id='n'+name)))),
                    rewards=market.rewards.model_copy(update=dict(rewards_min_size=q)),
                    trading=market.trading.model_copy(update=dict(minimum_order_size=q))))
            def list_markets(self, **kwargs):
                wanted = kwargs.get('condition_ids')
                return [self.get_market(id='m'+n) for n in rig.markets if wanted is None or 'c'+n in wanted]
            def get_order_book(self, *, token_id):
                from polymarket.models.clob.order_book import OrderBookLevel
                book = base.get_order_book(token_id=__import__('tests.test_lp_order_registration_contract',fromlist=['TOKEN_ID']).TOKEN_ID)
                name = token_id[1:]
                return book.model_copy(update=dict(market='c'+name, condition_id='c'+name, token_id=token_id, timestamp=rig.now,
                    bids=(OrderBookLevel(price=Decimal('.4'),size=Decimal('10000')),
                        OrderBookLevel(price=Decimal('.39'),size=Decimal('10000'))),
                    asks=(OrderBookLevel(price=Decimal('.42'),size=Decimal('10000')),)))
            def get_order_books(self, *, token_ids):
                return tuple(self.get_order_book(token_id=t) for t in token_ids)
            def list_market_rewards(self, *, condition_id, sponsored=None):
                row = _lp_reward_market_row(condition_id, earning_percentage='100',
                    rate_per_day=str(rig.markets[condition_id[1:]]['pool']))
                return (row,)
            def close(self): pass
        class Transport:
            def get_json(self, path, *, params):
                if path == '/rewards/user/markets' and params.get('sponsored') is not True:
                    rows = []
                    for name, hourly in rig.hourly.items():
                        pool = rig.markets[name]['pool']
                        rows.append(_lp_reward_market_row('c'+name,
                            earning_percentage=str(hourly*24*100/pool), rate_per_day=str(pool)))
                    return dict(data=rows,next_cursor='LTE=')
                if path == '/rewards/user/percentages': return {}
                return dict(data=[],next_cursor='LTE=')
        monkeypatch.setattr(polymarket_trading, 'signature_type_for', lambda _: 0)
        self.public = Public()
        self.adapter, self.sdk = _adapter(tmp_path, public_client=self.public)
        self.sdk._ctx = SimpleNamespace(secure_clob=Transport(),wallet_type=None)
        self.sdk.get_balance_allowance = lambda **kw: SimpleNamespace(balance=str(self.wallet*1000000),
            allowances={WALLET:str(self.allowance*1000000)})
        self.sdk.create_limit_order = lambda **kw: dict(kw,order_type='GTD')
        def post(signed):
            self.counter += 1
            self.sdk.posts.append(signed)
            name = signed['token_id'][1:]
            order_id = 'new-'+name+'-'+str(self.counter)
            self.add_order(name, signed['price']*signed['size'], order_id=order_id)
            return dict(accepted=True,order_id=order_id,status='LIVE',size_matched='0')
        self.sdk.post_order = post
        def cancel(*, order_ids):
            self.sdk.cancels.append(tuple(order_ids))
            if self.cancel_removes:
                self.release(*order_ids)
            return dict(canceled=tuple(order_ids),not_canceled={})
        self.sdk.cancel_orders = cancel
        self.sdk.get_order = lambda *, order_id: self.history[order_id]
        self.store = PredictionArbitrageStore(tmp_path)
        self.store.lp_save_preparation(dict(generation=1,global_recovery_generation=7))
        self.account = hashlib.sha256(WALLET.encode()).hexdigest()
        self.store.lp_save_current_settings(self.account, expected_config_version=0,
            changes=dict(trial_budget_usd=trial,income_budget_usd=income))
        seed_running(self.store)
        self.live = LPD2LiveExecution(self.store,self.adapter,account_id=WALLET,venue='polymarket',
            process_epoch='observed-pool-rig',policy=CalculationPolicy(None),load_settings_on_start=True,
            utc_now=clock or (lambda: datetime.now(UTC)))
        self.producer = PolymarketLPService(self.store,self.adapter,clock=clock or (lambda: datetime.now(UTC)))
        self.execution = attach(self.live,self.producer,tmp_path)
        self.live.initialize(ValidatedStartAdmission('observed-pool-rig','offline-permit'))
        self.live.run_once()

    def market(self,name,*,minimum='1',pool='100'):
        from tests.test_polymarket_lp_risk import _direction
        q=Decimal(minimum)/Decimal('.4')
        direction=_direction(reward_min_size=str(q),minimum_order_size=str(q))
        direction['market'].update(market_id='m'+name,condition_id='c'+name,token_id='t'+name,
            metadata_checked_at=self.now,fees_checked_at=self.now)
        direction['book'].update(condition_id='c'+name,token_id='t'+name,received_at=self.now,
            bids=[dict(price='.4',size='10000'),dict(price='.39',size='10000')],
            asks=[dict(price='.42',size='10000')])
        direction.update(daily_pool_usd=Decimal(pool),reward_checked_at=self.now)
        direction['history_summary']=dict(condition_id='c'+name,token_id='t'+name,state='known',
            checked_at=self.now,amplitude=Decimal('.01'))
        self.markets[name]=dict(condition_id='c'+name,token_id='t'+name,market_id='m'+name,
            venue='polymarket',price=Decimal('.4'),quantity=q,direction=direction,pool=Decimal(pool),
            order_type='GTD',expiration=1893456000,post_only=True,send_options=())
        assert self.producer._candidate_pool_record_success('c'+name,
            dict(condition_id='c'+name,state='eligible',selected_direction=dict(token_id='t'+name)),
            judged_at=self.now,facts=dict(checked_at=self.now,directions=[direction]),global_recovery_generation=7)

    def add_order(self,name,capital,*,order_id=None):
        from tests.test_lp_order_registration_contract import WALLET
        oid=order_id or 'old-'+name
        row=dict(id=oid,market='c'+name,market_id='m'+name,asset_id='t'+name,side='BUY',
            price=Decimal('.4'),original_size=Decimal(capital)/Decimal('.4'),size_matched=Decimal('0'),
            status='LIVE',order_type='GTC',expiration=None,maker_address=WALLET,owner=WALLET,
            outcome='YES',created_at=self.now)
        self.history[oid]=row
        self.sdk.orders=(*self.sdk.orders,row)
        return oid

    def release(self,*ids):
        self.sdk.orders=tuple(o for o in self.sdk.orders if o['id'] not in ids)
        for oid in ids:
            self.history[oid]={**self.history[oid],'status':'CANCELED'}

    def measure(self,rates):
        for name,rate in rates.items():
            capital=sum((o['price']*(o['original_size']-o['size_matched']) for o in self.sdk.orders
                if o['market']=='c'+name and o['side']=='BUY'),Decimal(0))
            self.hourly[name]=Decimal(rate)*capital/100
        assert self.store.lp_advance_trade_generation(self.store.lp_trade_generation())
        return self.execution.refresh_lp_observations()

    def capture(self):
        from open_trader.polymarket_lp_market_reader import SourceResult
        return SourceResult('offline-pool-candidates',tuple({k:v for k,v in r.items() if k!='pool'}
            for r in self.markets.values()),checked_at=self.now)

    def cycle(self,*,pool=None):
        return self.live.dispatch_rotation(self.live.run_once(self.capture(),pool=pool))

    def finish_cancels(self,result):
        for outcome in result.rotation_outcomes:
            if outcome.handle:
                outcome.handle.result(timeout=3)
                outcome.handle._thread.join(timeout=3)
                assert outcome.handle.done()

    def close(self):
        assert self.live.close(timeout=3)
        self.adapter.close()


def test_trial_candidate_rebuilds_income_as_measured_top_three(tmp_path,monkeypatch):
    from threading import Event
    rig=PoolRig(tmp_path,monkeypatch)
    release=Event()
    try:
        for name in 'ABCD': rig.market(name,minimum='1')
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','10')]: rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={name:dict(pool='income' if name[1:] in 'ABC' else 'trial',episode='old-'+name[1:])
                for name in ('cA','cB','cC','cD')})
        measured=rig.measure(dict(A='.6',B='1.0',C='.4',D='.8'))
        assert all(measured['observations']['c'+n]['pool_result']['state']=='known' for n in 'ABCD'), measured
        original_cancel=rig.sdk.cancel_orders
        def held_cancel(*,order_ids):
            result=original_cancel(order_ids=order_ids)
            assert release.wait(3),'cancel local completion watchdog'
            return result
        rig.sdk.cancel_orders=held_cancel
        planned=rig.cycle(pool='income')
        release.set()
        rig.finish_cancels(planned)
        assert {o.key[3] for o in planned.rotation.victims}=={'old-A','old-B','old-C','old-D'}
        # Local native capacity is two; a fresh round can attempt a previously
        # unadmitted exact cancellation. ACK still releases no cash or slot.
        sent={oid for batch in rig.sdk.cancels for oid in batch}
        if sent != {'old-A','old-B','old-C','old-D'}:
            again=rig.cycle(pool='income')
            rig.finish_cancels(again)
            assert again.selection.selection_state=='DECISION', (
                again.acceptance.status, [(o.key[3],o.venue_status,o.price.value,o.original_size.value,
                    o.size_matched.value,o.order_type.value,o.expiration.value) for o in again.acceptance.observation.orders],
                rig.sdk.orders)
        assert {oid for batch in rig.sdk.cancels for oid in batch}=={'old-A','old-B','old-C','old-D'}, (
            rig.live.rotation_diagnostics(), rig.live._cancel_blocks)
        assert rig.sdk.posts==[]  # all old exact BUYs still occupy account capacity
        rig.release('old-A','old-B','old-C','old-D')
        bought=rig.cycle(pool='income')
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[
            ('tB',Decimal('50')),('tD',Decimal('30')),('tA',Decimal('20'))]
        assert len(bought.outcomes)==3 and all(o.status.value=='accepted' for o in bought.outcomes)
    finally:
        release.set()
        rig.close()


def test_income_member_rates_alone_do_not_reorder_and_ties_keep_incumbents(tmp_path,monkeypatch):
    rig=PoolRig(tmp_path,monkeypatch)
    try:
        for name in 'ABC0': rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20')]: rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={'c'+n:dict(pool='income',episode='old-'+n) for n in 'ABC'})
        rig.measure(dict(A='.8',B='1',C='.6'))
        unchanged=rig.cycle(pool='income')
        rig.finish_cancels(unchanged)
        assert rig.sdk.cancels==rig.sdk.posts==[]
        rig.add_order('0','1')
        rig.measure({'0':'.6'})
        tied=rig.cycle(pool='income')
        rig.finish_cancels(tied)
        assert rig.sdk.cancels==rig.sdk.posts==[]
        rig.measure({'0':'.6000001'})
        improved=rig.cycle(pool='income')
        rig.finish_cancels(improved)
        assert {o.key[3] for o in improved.rotation.victims}=={'old-A','old-B','old-C','old-0'}
        assert {oid for batch in rig.sdk.cancels for oid in batch}=={'old-A','old-B','old-C','old-0'}
        assert rig.sdk.posts==[]
    finally:
        rig.close()


def test_income_bootstrap_leaves_empty_weights_and_filters_final_minimum(tmp_path,monkeypatch):
    for scenario in ('one','two','minimum','rounding'):
        rig=PoolRig(tmp_path/scenario,monkeypatch,trial='100',income='100.01' if scenario=='rounding' else '100')
        try:
            names='A' if scenario in ('one','rounding') else 'AB' if scenario=='two' else 'ABC'
            for name in names:
                minimum='40' if scenario=='minimum' and name=='B' else '1'
                rig.market(name,minimum=minimum)
                rig.add_order(name,minimum)
            rig.measure({n:r for n,r in zip(names,('1','.8','.6'))})
            rig.release(*('old-'+n for n in names))
            bought=rig.cycle(pool='income')
            assert all(o.status.value=='accepted' for o in bought.outcomes)
            actual=[(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]
            expected=([('tA',Decimal('50.004'))] if scenario=='rounding' else
                [('tA',Decimal('50'))] if scenario=='one' else
                [('tA',Decimal('50')),('tB',Decimal('30'))] if scenario=='two' else
                [('tA',Decimal('50')),('tC',Decimal('30'))])
            assert actual==expected
            assert rig.sdk.cancels==[]
        finally:
            rig.close()


def test_next_income_cycle_reselects_after_cancels_without_locked_group(tmp_path,monkeypatch):
    rig=PoolRig(tmp_path,monkeypatch)
    try:
        for name in 'ABCDE': rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','1')]: rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={'c'+n:dict(pool='income',episode='old-'+n) for n in 'ABC'})
        rig.measure(dict(A='.6',B='1',C='.4',D='.8'))
        first=rig.execution.lp_auto_scheduled_check()
        rig.finish_cancels(first)
        assert first.selection.targets==('cB','cD','cA')
        assert rig.sdk.posts==[]
        rig.add_order('E','1')
        rig.measure(dict(E='1.2'))
        rig.release('old-A','old-B','old-C','old-D','old-E')
        # The next income business opportunity uses a fresh qualified capture.
        # Automatic trial fairness is covered independently by P214-progress.
        second=rig.live.dispatch_rotation(rig.live.run_once(rig.producer.capture_qualified_pool(),pool='income'))
        rig.finish_cancels(second)
        assert second.selection.targets==('cE','cB','cD')
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[
            ('tE',Decimal('50')),('tB',Decimal('30')),('tD',Decimal('20'))]
        assert len({o.identity.request_id for o in second.outcomes})==3
    finally:
        rig.close()


def test_both_pool_cycles_share_actual_capacity_without_borrowing(tmp_path,monkeypatch):
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    rig=PoolRig(tmp_path,monkeypatch,wallet='40')
    entered,release=Event(),Event()
    try:
        rig.market('A',minimum='1',pool='10')
        rig.market('B',minimum='15',pool='1000')
        rig.market('C',minimum='5',pool='5000')
        rig.add_order('A','10')
        rig.add_order('A','5',order_id='manual-extra-A')
        rig.add_order('B','15')
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={'cA':dict(pool='income',episode='old-A')})
        rig.sdk.positions=(dict(condition_id='cB',token_id='tB',size='20',average_price='.4'),)
        sell={**rig.history['old-B'],'id':'inventory-SELL','side':'SELL','original_size':Decimal('20')}
        rig.sdk.orders=(*rig.sdk.orders,sell)
        bounded=rig.cycle(pool='trial')
        assert rig.sdk.posts==[]  # trial15 + unclassified manual5 exhaust trial20
        assert rig.live.state.snapshot().inventory_cost.value==8
        assert next(o for o in rig.live.state.snapshot().active_orders if o.key[3]=='inventory-SELL').side=='SELL'
        # Actual removal, not a desired cancellation, creates trial capacity.
        rig.release('manual-extra-A')
        rig.allowance=Decimal('28')  #25 actual BUYs leave only3, below C5
        limited=rig.cycle(pool='trial')
        assert rig.sdk.posts==[]
        rig.allowance=Decimal('40')
        original=rig.sdk.post_order
        def uncertain(signed):
            entered.set()
            assert release.wait(3),'BUY local native completion watchdog'
            rig.sdk.posts.append(signed)
            raise TimeoutError('offline unbound BUY response')
        rig.sdk.post_order=uncertain
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending=executor.submit(rig.cycle,pool='trial')
            assert entered.wait(2)
            assert len(rig.sdk.posts)==0
            with pytest.raises(RuntimeError,match='d2_run_in_progress'):
                rig.cycle(pool='income')
            release.set()
            unknown=pending.result(timeout=3)
        assert len(unknown.outcomes)==1 and unknown.outcomes[0].status.value=='unknown_timeout'
        original_request=unknown.outcomes[0].identity.request_id
        assert rig.live.state.snapshot().new_capacity.state=='unknown'
        # Fresh complete account proof may cover the ended unbound request;
        # historical UNKNOWN is retained, and that request is never replayed.
        rig.sdk.post_order=original
        after=rig.cycle(pool='income')
        assert all(o.identity.request_id!=original_request for o in after.outcomes)
        assert len(rig.sdk.posts)==1  # income cannot borrow the empty trial allocation
        assert rig.live.state.snapshot().inventory_cost.value==8
    finally:
        release.set()
        rig.close()


def test_promotion_excludes_trial_market_until_actual_release(tmp_path,monkeypatch):
    rig=PoolRig(tmp_path,monkeypatch)
    try:
        rig.market('D',minimum='10',pool='1000')
        rig.add_order('D','10')
        rig.measure(dict(D='.8'))
        old=rig.cycle(pool='income')
        rig.finish_cancels(old)
        assert rig.sdk.cancels==[('old-D',)]
        assert rig.sdk.posts==[]
        source=rig.live.state.snapshot().observation
        assert source.order_view.open_orders[0].key[3]=='old-D'
        # The external current list still contains the exact old ID after ACK.
        trial=rig.cycle(pool='trial')
        income=rig.cycle(pool='income')
        rig.finish_cancels(trial)
        rig.finish_cancels(income)
        assert rig.sdk.posts==[]
        assert [o['id'] for o in rig.sdk.orders]==['old-D']
        rig.release('old-D')
        # Even before the next income BUY, trial must not reopen promoted D.
        released_trial=rig.cycle(pool='trial')
        rig.finish_cancels(released_trial)
        assert rig.sdk.posts==[]
        new=rig.cycle(pool='income')
        rig.finish_cancels(new)
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[('tD',Decimal('50'))]
        new_id=new.outcomes[0].venue_order_id
        assert new_id!='old-D'
        assert all(o.identity.request_id!=old.rotation_outcomes[0].handle.identity.request_id for o in new.outcomes)
        # A historical rotation result has no current observation authority.
        rig.live.dispatch_rotation(old)
        assert rig.sdk.cancels==[('old-D',)]
        assert any(o.key[3]==new_id for o in rig.live.state.snapshot().active_orders)
    finally:
        rig.close()


@pytest.mark.parametrize('previous_member',[False,True])
def test_partial_income_submission_keeps_successes_and_unknown_identity(tmp_path,monkeypatch,previous_member):
    for disposition in ('rejected','unknown'):
        rig=PoolRig(tmp_path/disposition,monkeypatch)
        try:
            for name in 'BDA':
                rig.market(name)
                rig.add_order(name,'1')
            if previous_member:
                rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
                    roles={'cA':dict(pool='income',episode='old-A')})
            rig.measure(dict(B='1',D='.8',A='.6'))
            rig.release('old-B','old-D','old-A')
            original_post=rig.sdk.post_order
            def partial(signed):
                if signed['token_id']!='tA': return original_post(signed)
                rig.sdk.posts.append(signed)
                if disposition=='unknown': raise TimeoutError('offline last income response lost')
                return dict(accepted=False,status='REJECTED')
            rig.sdk.post_order=partial
            first=rig.cycle(pool='income')
            assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[
                ('tB',Decimal('50')),('tD',Decimal('30')),('tA',Decimal('20'))]
            by_token={o.identity.target.token_id:o for o in first.outcomes}
            assert by_token['tB'].status.value==by_token['tD'].status.value=='accepted'
            assert by_token['tA'].status.value==('unknown_timeout' if disposition=='unknown' else 'rejected')
            request_id=by_token['tA'].identity.request_id
            assert {o['asset_id'] for o in rig.sdk.orders}=={'tB','tD'}
            assert rig.sdk.cancels==[]
            if disposition=='unknown':
                assert rig.live.state.snapshot().new_capacity.state=='unknown'
                current_orders=rig.sdk.orders
                rig.sdk.orders=None
                blocked=rig.cycle(pool='income')
                assert not blocked.outcomes and len(rig.sdk.posts)==3
                rig.sdk.orders=current_orders
            next_cycle=rig.cycle(pool='income')
            rig.finish_cancels(next_cycle)
            assert rig.sdk.cancels==[]  # retain B/D; no teardown to retry the failed A slot
            assert len(rig.sdk.posts)==3  #20 stays cash; no old-sample replay
            assert all(o.identity.request_id!=request_id for o in next_cycle.outcomes)
            assert {o['asset_id'] for o in rig.sdk.orders}=={'tB','tD'}
        finally:
            rig.close()


def test_evicted_income_market_requires_new_trial_sample(tmp_path,monkeypatch):
    rig=PoolRig(tmp_path,monkeypatch)
    try:
        for name in 'ABCD': rig.market(name,minimum='1',pool='1000' if name=='C' else '100')
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','1')]:rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={'c'+n:dict(pool='income',episode='old-'+n) for n in 'ABC'})
        rig.measure(dict(A='.6',B='1',C='.4',D='.8'))
        replacing=rig.cycle(pool='income');rig.finish_cancels(replacing)
        rig.release('old-A','old-B','old-C','old-D')
        bought=rig.cycle(pool='income');rig.finish_cancels(bought)
        assert {o.identity.target.token_id for o in bought.outcomes}=={'tA','tB','tD'}
        assert not any(o['asset_id']=='tC' for o in rig.sdk.orders)
        old_c=rig.execution.lp_observed_pool_state()['results']['cC']['result']
        assert Decimal(old_c['yield_pct_per_hour'])==Decimal('.4')
        rig.measure(dict(A='.1',B='.3',D='.2'))
        before=list(rig.sdk.cancels)
        no_old_promotion=rig.cycle(pool='income');rig.finish_cancels(no_old_promotion)
        assert rig.sdk.cancels==before
        assert len(rig.sdk.posts)==3
        reentered=rig.cycle(pool='trial');rig.finish_cancels(reentered)
        assert [(o.identity.target.token_id,o.identity.target.terms.price*o.identity.target.terms.quantity)
            for o in reentered.outcomes]==[('tC',Decimal('1'))]
        new_c=reentered.outcomes[0].venue_order_id
        assert new_c!='old-C'
        before_new_sample=rig.cycle(pool='income');rig.finish_cancels(before_new_sample)
        assert rig.sdk.cancels==before
        rig.measure(dict(C='.5'))
        new_sample=rig.execution.lp_observed_pool_state()['results']['cC']['result']
        assert new_sample['source_order']['order_id']==new_c
        promoted=rig.cycle(pool='income');rig.finish_cancels(promoted)
        assert {o.key[3] for o in promoted.rotation.victims}=={o['id'] for o in rig.sdk.orders}
        assert promoted.selection.targets==('cC','cB','cD')
    finally:
        rig.close()


def test_promoted_size_uses_new_measurement_without_automatic_reshuffle(tmp_path,monkeypatch):
    rig=PoolRig(tmp_path,monkeypatch)
    try:
        rig.market('D',minimum='10')
        rig.market('E',minimum='1')
        rig.add_order('D','10')
        trial=rig.measure(dict(D='.8'))['observations']['cD']['pool_result']
        assert Decimal(trial['buy_capital_usd'])==10
        canceled=rig.cycle(pool='income');rig.finish_cancels(canceled)
        rig.release('old-D')
        promoted=rig.cycle(pool='income');rig.finish_cancels(promoted)
        new_d=promoted.outcomes[0].venue_order_id
        assert promoted.outcomes[0].identity.target.terms.price*promoted.outcomes[0].identity.target.terms.quantity==50
        assert rig.store.lp_advance_trade_generation(rig.store.lp_trade_generation())
        dashboard=rig.execution.refresh_lp_dashboard_snapshot()
        projected=dashboard['lp_observations']['cD']
        assert projected['current_yield_pct_per_hour'] is None
        detached=rig.execution.lp_observed_pool_state()['results']['cD']['result']
        assert detached==trial and detached['source_order']['order_id']=='old-D'
        lower=rig.measure(dict(D='.1'))['observations']['cD']['pool_result']
        assert lower['source_order']['order_id']==new_d
        assert Decimal(lower['buy_capital_usd'])==50
        assert Decimal(lower['yield_pct_per_hour'])==Decimal('.1')
        before=list(rig.sdk.cancels)
        stable=rig.cycle(pool='income');rig.finish_cancels(stable)
        assert rig.sdk.cancels==before and len(rig.sdk.posts)==1
        rig.add_order('E','1')
        rig.measure(dict(E='.2'))
        new_trial=rig.cycle(pool='income');rig.finish_cancels(new_trial)
        assert new_trial.selection.targets==('cE','cD')
        assert {o.key[3] for o in new_trial.rotation.victims}=={new_d,'old-E'}
        assert len(rig.sdk.posts)==1  # no anticipatory release/re-sizing
    finally:
        rig.close()


def test_two_business_loops_share_switch_and_progress_independently(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_d2_integration import settle_control
    rig=PoolRig(tmp_path,monkeypatch)
    entered,release=Event(),Event()
    try:
        rig.market('D');rig.add_order('D','1');rig.measure(dict(D='.8'))
        canceled=rig.cycle(pool='income');rig.finish_cancels(canceled)
        rig.release('old-D')
        # A real fresh producer has no recovered qualification inventory.
        cold=PolymarketLPService(rig.store,rig.adapter)
        rig.execution._lp=cold
        capture=cold.capture_qualified_pool()
        assert not capture.rows
        rig.execution.lp_auto_set_desired_running(False);settle_control(rig.live)
        scheduler=LPAutoScheduler(rig.execution)
        assert scheduler.run_due() and rig.sdk.posts==[]
        rig.execution.lp_auto_set_desired_running(True);settle_control(rig.live)
        scheduler.request_check();assert scheduler.run_due()  # real resume round-zero cleanup
        assert rig.sdk.posts==[]
        post=rig.sdk.post_order
        def held(signed):
            entered.set()
            assert release.wait(4),'independent native watchdog'
            return post(signed)
        rig.sdk.post_order=held
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending=workers.submit(scheduler.run_due)
            try:
                assert entered.wait(2),'income must progress without trial qualifications'
                scheduler.request_check()
                assert not scheduler.run_due()  # same serialized owner
                rig.execution.lp_auto_set_desired_running(False);settle_control(rig.live)
                assert not rig.execution.lp_auto_state()['effective_running']
            finally:
                release.set()
            assert pending.result(timeout=4)
        assert len(rig.sdk.posts)==1
        assert rig.sdk.posts[0]['token_id']=='tD'
        assert rig.sdk.posts[0]['price']*rig.sdk.posts[0]['size']==50
        # Restore a healthy producer: common pause prevents ordinary trial too.
        rig.execution._lp=rig.producer
        rig.market('E')
        for _ in range(2):
            scheduler.request_check();assert scheduler.run_due()
        assert len(rig.sdk.posts)==1
        assert rig.sdk.cancels==[('old-D',)]  # startup cleanup is not repeated per pool
    finally:
        release.set();rig.close()


def test_income_cycle_consumes_one_result_snapshot_and_next_cycle_gets_updates(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    rig=PoolRig(tmp_path,monkeypatch)
    entered,release=Event(),Event()
    try:
        for n in 'DE':rig.market(n)
        rig.add_order('D','1');rig.add_order('E','1')
        old=rig.measure(dict(D='.8'))['observations']['cD']['pool_result']
        canceled=rig.cycle(pool='income');rig.finish_cancels(canceled);rig.release('old-D')
        post=rig.sdk.post_order
        def held(signed):
            entered.set();assert release.wait(4),'native completion watchdog'
            return post(signed)
        rig.sdk.post_order=held
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending=workers.submit(rig.cycle,pool='income')
            try:
                assert entered.wait(2)
                new=rig.measure(dict(E='1.2'))['observations']['cE']['pool_result']
                assert new['source_order']['order_id']=='old-E'
            finally:release.set()
            fixed=pending.result(timeout=4)
        assert fixed.selection.targets==('cD',)
        assert len(fixed.outcomes)==1
        assert rig.sdk.posts[0]['token_id']=='tD'
        assert rig.sdk.posts[0]['price']*rig.sdk.posts[0]['size']==50
        assert rig.execution.lp_observed_pool_state()['results']['cD']['result']==old
        following=rig.cycle(pool='income');rig.finish_cancels(following)
        assert following.selection.targets==('cE','cD')
        assert {o.key[3] for o in following.rotation.victims}=={fixed.outcomes[0].venue_order_id,'old-E'}
        assert len(rig.sdk.posts)==1
    finally:release.set();rig.close()


@pytest.mark.parametrize('income_state',['new_results','market_unavailable'])
def test_ready_trial_progresses_while_income_results_change_or_wait(tmp_path,monkeypatch,income_state):
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    rig=PoolRig(tmp_path,monkeypatch)
    try:
        for name in 'DE':rig.market(name)
        rig.add_order('D','50')
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={'cD':dict(pool='income',episode='old-D')})
        rig.measure(dict(D='.8'))
        read=rig.public.get_order_book
        if income_state=='market_unavailable':
            def unavailable(*,token_id):
                if token_id=='tD':raise TimeoutError('offline income-only book unavailable')
                return read(token_id=token_id)
            rig.public.get_order_book=unavailable
        scheduler=LPAutoScheduler(rig.execution)
        measured=[]
        for _ in range(2):
            if income_state=='new_results':
                sample=rig.measure(dict(D='.8'))['observations']['cD']['pool_result']
                measured.append(sample['measured_at'])
            scheduler.request_check();assert scheduler.run_due()
        if measured:assert measured[0]!=measured[1]
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[('tE',Decimal('1'))]
        assert rig.sdk.cancels==[]
        assert {o['id'] for o in rig.sdk.orders}=={'old-D','new-E-1'}
        assert rig.live.state.snapshot().buy_reserved.value==51
    finally:rig.close()


def _pool_test_clock(monkeypatch):
    from datetime import UTC,datetime
    from importlib import import_module
    clock=[datetime.now(UTC)]
    class ClockMeta(type):
        def __instancecheck__(cls,value):return isinstance(value,datetime)
    class ClockDatetime(datetime,metaclass=ClockMeta):
        @classmethod
        def now(cls,tz=None):return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    for module in ('polymarket_trading','polymarket_lp_market_reader','polymarket_lp_current_state',
            'polymarket_lp_live_execution','prediction_arbitrage_execution','polymarket_lp'):
        monkeypatch.setattr(import_module('open_trader.'+module),'datetime',ClockDatetime)
    return clock


@pytest.mark.parametrize('prior_request',['abandoned','unknown'])
def test_restart_rebuilds_both_pool_state_without_old_send_authority(tmp_path,monkeypatch,prior_request):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Event
    from tests.test_lp_d2_execution_bridge import attach
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    clock=_pool_test_clock(monkeypatch)
    rig=PoolRig(tmp_path,monkeypatch,clock=lambda:clock[0])
    entered,release=Event(),Event()
    try:
        rig.market('D',minimum='1');rig.add_order('D','1')
        sample=rig.measure(dict(D='.8'))['observations']['cD']['pool_result']
        cancel=rig.cycle(pool='income');rig.finish_cancels(cancel);rig.release('old-D')
        old_requests={o.handle.identity.request_id for o in cancel.rotation_outcomes}
        post=rig.sdk.post_order
        if prior_request=='unknown':
            def uncertain(signed):
                entered.set();assert release.wait(4),'unresolved native watchdog'
                rig.sdk.posts.append(signed)
                raise TimeoutError('offline income response lost')
            rig.sdk.post_order=uncertain
            with ThreadPoolExecutor(max_workers=1) as workers:
                pending=workers.submit(rig.cycle,pool='income')
                try:
                    assert entered.wait(2)
                    with pytest.raises(RuntimeError,match='d2_run_in_progress'):rig.cycle(pool='trial')
                    assert rig.sdk.posts==[]
                finally:release.set()
                unknown=pending.result(timeout=4)
            assert unknown.outcomes[0].status.value=='unknown_timeout'
            old_requests.add(unknown.outcomes[0].identity.request_id)
            assert rig.live.state.snapshot().new_capacity.state=='unknown'
            rig.sdk.orders=None
            assert not rig.cycle(pool='trial').outcomes
            assert rig.live.state.snapshot().new_capacity.state=='unknown'
        assert rig.live.close(timeout=3)
        clock[0]+=timedelta(seconds=121);rig.now=clock[0];rig.market('D',minimum='1')
        rig.sdk.post_order=post
        rig.live=LPD2LiveExecution(rig.store,rig.adapter,account_id=rig.live.account_id,venue='polymarket',
            process_epoch='observed-restart',policy=CalculationPolicy(None),load_settings_on_start=True,
            utc_now=lambda:clock[0])
        rig.execution=attach(rig.live,rig.producer,tmp_path)
        rig.live.initialize(ValidatedStartAdmission('observed-restart','offline-permit'))
        if prior_request=='unknown':
            blocked=rig.cycle(pool='income')
            assert not blocked.outcomes and rig.live.rounds.snapshot().round_number==0
            assert len(rig.sdk.posts)==1
            rig.sdk.orders=()  # first complete fresh account evidence, not invented rejection
        startup=rig.cycle(pool='income')
        assert not startup.outcomes
        assert rig.live.rounds.snapshot().round_number==1
        expired=rig.execution.lp_observed_pool_state()['results']['cD']
        assert expired['result']==sample and not expired['eligible']
        assert not rig.cycle(pool='income').outcomes  # expired result has no send authority
        before=len(rig.sdk.posts)
        fresh=rig.cycle(pool='trial')
        assert len(fresh.outcomes)==1
        assert len(rig.sdk.posts)==before+1
        new=fresh.outcomes[0]
        assert new.venue_order_id!='old-D' and new.identity.request_id not in old_requests
        assert new.identity.target.terms.price*new.identity.target.terms.quantity==1
        clock[0]+=timedelta(seconds=1);rig.now=clock[0]
        measured=rig.measure(dict(D='.2'))['observations']['cD']['pool_result']
        assert measured['source_order']['order_id']==new.venue_order_id
        assert measured['measured_at']!=sample['measured_at']
    finally:release.set();rig.close()


def test_fill_and_protection_remain_authoritative_during_pool_changes(tmp_path,monkeypatch):
    from threading import Event
    from time import monotonic,sleep
    from tests.test_lp_d2_integration import settle_control
    rig=PoolRig(tmp_path,monkeypatch)
    entered,release=Event(),Event()
    try:
        for name in 'ABCDP':rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','10')]:rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={'c'+n:dict(pool='income',episode='old-'+n) for n in 'ABC'})
        rig.measure(dict(A='.6',B='1',D='.8'))  # C has no attributable measurement
        unsafe=rig.cycle(pool='income');rig.finish_cancels(unsafe)
        assert rig.sdk.cancels==[]  # unknown incumbent cannot authorize whole-group teardown
        rig.measure(dict(C='.4'))
        def partial_cancel(*,order_ids):
            rig.sdk.cancels.append(tuple(order_ids));entered.set()
            assert release.wait(4),'group cancel completion watchdog'
            rig.release('old-C')
            return dict(canceled=['old-C'],not_canceled={oid:'offline unknown finality' for oid in order_ids if oid!='old-C'})
        rig.sdk.cancel_orders=partial_cancel
        changing=rig.cycle(pool='income')
        try:
            assert entered.wait(2)
            handles=[o.handle for o in changing.rotation_outcomes]
            assert len(handles)==4 and len({h.identity.request_id for h in handles})==4
            assert all(not h.done() for h in handles)
            # A live shared native lease prevents another manual spending owner.
            busy=rig.execution.lp_start('unused-preview','manual-during-pool-cancel')
            assert busy==dict(state='busy',reason='execution_lock')
            for oid in ('old-A','old-D'):
                row={**rig.history[oid],'size_matched':Decimal('5'),'status':'PARTIALLY_FILLED'}
                rig.history[oid]=row
                rig.sdk.orders=tuple(row if o['id']==oid else o for o in rig.sdk.orders)
            rig.sdk.positions=tuple(dict(condition_id='c'+n,token_id='t'+n,size='5',average_price='.4') for n in 'AD')
            protected={**rig.history['old-D'],'id':'protected-SELL','market':'cP','asset_id':'tP',
                'side':'SELL','original_size':Decimal('2'),'size_matched':Decimal('0'),'price':Decimal('.45')}
            rig.sdk.orders=(*rig.sdk.orders,protected)
            rig.sdk.positions=(*rig.sdk.positions,dict(condition_id='cP',token_id='tP',size='2',average_price='.4'))
            rig.execution.lp_auto_set_desired_running(False);settle_control(rig.live)
            stopped=rig.cycle(pool='income')
            assert not stopped.outcomes and rig.sdk.posts==[]
            assert all(not h.done() for h in handles)  # control transition cannot drain a live cancel
        finally:release.set()
        rig.finish_cancels(changing)
        by_order={h.identity.target.existing_order_id:h.result(timeout=3).status.value for h in handles}
        assert by_order=={'old-A':'unknown_error','old-B':'unknown_error','old-C':'accepted','old-D':'unknown_error'}
        reconciled=rig.cycle(pool='trial')
        assert not reconciled.outcomes
        reservations=rig.execution.lp_auto_reservations()
        assert Decimal(reservations['buy_reserved_usd'])==86
        assert Decimal(reservations['inventory_cost_usd'])==Decimal('4.8')
        assert {r['order_id']:Decimal(r['amount_usd']) for r in reservations['reservations']}=={
            'old-A':Decimal('48'),'old-B':Decimal('30'),'old-D':Decimal('8')}
        protection=rig.live.run_protection_once()
        for handle in protection:
            handle.result(timeout=3);handle._thread.join(timeout=3)
        assert rig.sdk.posts==[]  # inventory and an existing SELL grant no forced/duplicate exit
        assert any(o['id']=='protected-SELL' and o['side']=='SELL' for o in rig.sdk.orders)
        deadline=monotonic()+3
        while monotonic()<deadline:
            status=rig.live.writer.status()
            if not status['pending_entries'] and not status['inflight_entries']:break
            sleep(.001)
        else:pytest.fail('actual audit completion watchdog')
        for h in handles:
            record,=rig.store.lp_actions('d2:'+h.identity.request_id)
            assert record['request_id']==h.identity.request_id
            assert record['order_id']==record['receipt']['order_id']==h.identity.target.existing_order_id
            assert record['kind']=='CANCEL'
            assert record['receipt']['outcome']==('accepted' if record['order_id']=='old-C' else 'unknown')
        assert rig.sdk.cancels==[('old-A','old-B','old-C','old-D')]
    finally:release.set();rig.close()

    unreadable=PoolRig(tmp_path/'unknown-incumbent-market',monkeypatch)
    try:
        for n in 'ABCD':unreadable.market(n);unreadable.add_order(n,'1')
        unreadable.store.lp_save_observed_pool_roles(unreadable.account,expected_revision=0,
            roles={'c'+n:dict(pool='income',episode='old-'+n) for n in 'ABC'})
        unreadable.measure(dict(A='.6',B='1',C='.4',D='.8'))
        read=unreadable.public.get_order_book
        def missing(*,token_id):
            if token_id=='tC':raise TimeoutError('offline current incumbent market unavailable')
            return read(token_id=token_id)
        unreadable.public.get_order_book=missing
        waiting=unreadable.cycle(pool='income');unreadable.finish_cancels(waiting)
        assert unreadable.sdk.cancels==[] and unreadable.sdk.posts==[]
        assert set(waiting.selection.targets)=={'cA','cB','cC'}
    finally:unreadable.close()

    # A separate real group proves actual owner drain without sacrificing the
    # audit verification above: close blocks admission but cannot end SDK work.
    import fcntl
    drain=PoolRig(tmp_path/'group-drain',monkeypatch)
    held,finish=Event(),Event()
    def available():
        with (tmp_path/'group-drain'/'execution.lock').open('a+') as lock:
            try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return False
            fcntl.flock(lock.fileno(),fcntl.LOCK_UN);return True
    try:
        for n in 'ABCD':drain.market(n);drain.add_order(n,'1')
        drain.store.lp_save_observed_pool_roles(drain.account,expected_revision=0,
            roles={'c'+n:dict(pool='income',episode='old-'+n) for n in 'ABC'})
        drain.measure(dict(A='.6',B='1',C='.4',D='.8'))
        cancel=drain.sdk.cancel_orders
        def owned(*,order_ids):
            held.set();assert finish.wait(4),'group drain native watchdog'
            return cancel(order_ids=order_ids)
        drain.sdk.cancel_orders=owned
        result=drain.cycle(pool='income')
        assert held.wait(2)
        assert drain.live.status()['native_inflight']==1 and not available()
        assert not drain.live.close(timeout=.01)
        assert drain.live.status()['native_inflight']==1 and not available()
        finish.set();drain.finish_cancels(result)
        assert drain.live.close(timeout=3) and available()
        assert drain.live.status()['native_inflight']==0
    finally:finish.set();drain.close()


def test_pool_http_and_cli_keep_schema_auth_and_config_confirmation(tmp_path,monkeypatch,capsys):
    import json
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from threading import Thread
    from open_trader import cli
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_auto_control import runtime_for
    from tests.test_lp_d2_integration import settle_control
    from tests.test_prediction_service import _server,_response,_production_request
    from tests.test_lp_auto_pool import setup
    rig=PoolRig(tmp_path/'real',monkeypatch)
    try:
        rig.execution.lp_auto_set_desired_running(False);settle_control(rig.live)
        rig.live.run_once()
        runtime=runtime_for(tmp_path/'runtime');runtime.execution=rig.execution
        runtime._lp_auto_scheduler=LPAutoScheduler(rig.execution)
        root='/api/prediction-arbitrage/lp/auto/'
        with _server(runtime,session_token='session-token',csrf_token='csrf-token') as base:
            code=cli.main(['prediction-arb','lp-auto','config','--url',base,
                '--trial-budget','40','--income-budget','150','--json'])
            result=json.loads(capsys.readouterr().out)
            assert code==0,result
            assert result['result']=='CONFIGURED'
            state=_response(base+root+'state')[1]
            assert state['trial_budget_usd']==state['budget_usd']=='40'
            assert state['income_budget_usd']=='150'
            assert state['trial_target_buy_count']==state['income_target_buy_count']==3
            assert state['income_weights']==['0.5','0.3','0.2']
            assert state['desired_running'] is state['effective_running'] is False
            before=rig.store.lp_load_current_settings(rig.account)
            for headers,status in [({'Cookie':'ot_prediction_session=invalid'},403),({'X-CSRF-Token':'invalid'},403)]:
                assert _response(_production_request(base,root+'config',b'{"income_budget_usd":"200"}',headers=headers))[0]==status
                assert rig.store.lp_load_current_settings(rig.account)==before
            for changes in [dict(income_budget_usd='-1'),dict(trial_budget_usd='NaN'),
                    dict(income_budget_usd='200',expected_config_version=0),
                    dict(budget_usd='40',trial_budget_usd='41',target_buy_count=3),
                    dict(trial_target_buy_count=4),dict(income_weights=['1','0','0'])]:
                payload=dict(expected_config_version=state['config_version'],**{k:v for k,v in changes.items() if k!='expected_config_version'})
                if 'expected_config_version' in changes:payload['expected_config_version']=changes['expected_config_version']
                assert _response(_production_request(base,root+'config',json.dumps(payload).encode()))[0]==400
                assert rig.store.lp_load_current_settings(rig.account)==before
            assert cli.main(['prediction-arb','lp-auto','config','--url',base,
                '--budget','50','--trial-budget','51','--income-budget','150','--json'])==2
            assert json.loads(capsys.readouterr().out)['result']=='UNKNOWN'
            assert rig.store.lp_load_current_settings(rig.account)==before
            for bad in ('NaN','Infinity','-1','not-a-number'):
                assert cli.main(['prediction-arb','lp-auto','config','--url',base,
                    '--trial-budget','40','--income-budget',bad,'--json'])==2
                assert json.loads(capsys.readouterr().out)['result']=='UNKNOWN'
                assert rig.store.lp_load_current_settings(rig.account)==before
        # External service response can never acknowledge a missing budget or leak auth.
        class Unconfirmed(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.respond(dict(csrf_token='secret-csrf') if self.path.endswith('/venues') else state)
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                self.respond({**state,'config_version':state['config_version']+1,
                    'trial_budget_usd':'60','budget_usd':'60','income_budget_usd':'150','csrf_token':'secret-csrf'})
            def respond(self,payload):
                body=json.dumps(payload).encode();self.send_response(200)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Unconfirmed);thread=Thread(target=server.serve_forever);thread.start()
        try:
            assert cli.main(['prediction-arb','lp-auto','config','--url',f'http://127.0.0.1:{server.server_port}',
                '--trial-budget','60','--income-budget','200','--json'])==2
            output=capsys.readouterr().out
            assert json.loads(output)['result']=='UNKNOWN' and 'secret-csrf' not in output
        finally:server.shutdown();thread.join(timeout=3);server.server_close()
        legacy,exchange,_,store=setup(tmp_path/'legacy')
        legacy.lp_auto_configure(dict(budget_usd='40',target_buy_count=3))
        store.lp_save_current_settings(legacy._lp_account_id(),expected_config_version=1,
            changes=dict(trial_budget_usd='40',income_budget_usd='100'))
        with pytest.raises(ValueError,match='observed_pools_requires_d2'):
            legacy.lp_auto_set_desired_running(True)
        with pytest.raises(ValueError,match='observed_pools_requires_d2'):
            legacy.lp_auto_run_once()
        assert exchange.posts==[]
        assert rig.sdk.posts==rig.sdk.cancels==[]
    finally:rig.close()


def test_pool_dashboard_names_budgets_and_preserves_unsaved_draft():
    import json
    from tests.test_dashboard_web import run_dashboard_js
    result=json.loads(run_dashboard_js(r'''
state.predictionMarket.csrfToken='csrf';
const legacy={desired_running:false,ever_enabled:false,pause_confirmed:true,budget_usd:'40',target_buy_count:3,config_version:7,slots:{occupied:0}};
state.predictionMarket.lpDashboard={auto:legacy};
const initial=predictionLpAutoControls(legacy,false);
handleLpAutoInput({target:{closest:()=>({dataset:{lpAutoField:'budget_usd'},value:'45'})}});
handleLpAutoInput({target:{closest:()=>({dataset:{lpAutoField:'income_budget_usd'},value:'100'})}});
const poll=predictionLpAutoControls({...legacy,budget_usd:'999',income_budget_usd:'888',config_version:8},false);
renderPredictionMarket=()=>{};fetchPredictionLpDashboard=async()=>{};
const calls=[];
predictionPost=async(path,body)=>{calls.push({path,body});return {...legacy,allocation_mode:'observed_pools',
  config_version:8,budget_usd:'45',trial_budget_usd:'45',income_budget_usd:'100',trial_target_buy_count:3,income_target_buy_count:3,income_weights:['0.5','0.3','0.2']};};
await controlLpAuto('config');
const saved=state.predictionMarket.lpDashboard.auto;
const dual=predictionLpAutoControls({...saved,observed_pools:{roles:{cD:{pool:'income',episode:'new-D'}},
  results:{cD:{eligible:true,age_seconds:3,result:{state:'known',yield_pct_per_hour:'.8',measured_at:'2026-10-21T12:00:00Z',buy_capital_usd:'10',source_order:{order_id:'trial-D'}}}}}},false);
const measured=lpDashboardYieldCell({state:'known',current_yield_pct_per_hour:'.1',trial_baseline:{yield_pct_per_hour:'.6'}});
console.log(JSON.stringify({initial,poll,calls,saved,dual,measured,draft:state.predictionMarket.lpAutoDraft}));
'''))
    assert '试挂资金' in result['initial'] and '收益资金' in result['initial']
    assert 'data-lp-auto-field="income_budget_usd"' in result['initial']
    assert 'data-lp-auto-field="income_budget_usd" aria-label="收益资金" type="number" min="0" step="0.01" value=""' in result['initial']
    assert 'value="45"' in result['poll'] and 'value="100"' in result['poll']
    assert 'value="999"' not in result['poll'] and 'value="888"' not in result['poll']
    assert result['calls']==[{'path':'/api/prediction-arbitrage/lp/auto/config','body':{
        'trial_budget_usd':'45','income_budget_usd':'100','allocation_mode':'observed_pools','expected_config_version':7}}]
    assert result['saved']['desired_running'] is False and result['draft'] is None
    assert '试挂 3' in result['dual'] and '收益 3' in result['dual'] and '50% / 30% / 20%' in result['dual']
    assert '实测结果' in result['dual'] and '收益池' in result['dual'] and 'trial-D' in result['dual']
    assert '采样' in result['dual'] and '20:00:00' in result['dual']
    assert '模型估算' in result['measured'] and '当前实测' in result['measured']


def test_trial_nonpromotion_keeps_order_and_model_rotation_still_replaces(tmp_path,monkeypatch):
    rig=PoolRig(tmp_path,monkeypatch)
    try:
        for n,minimum,pool in [('A','6','1200'),('B','15','600'),('C','4','240'),('D','1','40'),
                ('X','1','100'),('Y','1','100'),('Z','1','100')]:
            rig.market(n,minimum=minimum,pool=pool)
        for n,capital in [('A','6'),('C','4'),('D','1'),('X','50'),('Y','30'),('Z','20')]:rig.add_order(n,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={'c'+n:dict(pool='income',episode='old-'+n) for n in 'XYZ'})
        rig.measure(dict(X='1',Y='.8',Z='.6',D='.1'))
        income=rig.cycle(pool='income');rig.finish_cancels(income)
        trial=rig.cycle(pool='trial');rig.finish_cancels(trial)
        assert rig.sdk.cancels==[] and rig.sdk.posts==[]
        assert any(o['id']=='old-D' for o in rig.sdk.orders)
        rig.market('E',minimum='1',pool='120')
        replacement=rig.cycle(pool='trial');rig.finish_cancels(replacement)
        assert {o.key[3] for o in replacement.rotation.victims}=={'old-D'}
        assert rig.sdk.cancels==[('old-D',)] and rig.sdk.posts==[]
        assert {o['id'] for o in rig.sdk.orders}=={'old-A','old-C','old-D','old-X','old-Y','old-Z'}
        rig.release('old-D')
        bought=rig.cycle(pool='trial');rig.finish_cancels(bought)
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[('tE',Decimal('1'))]
        assert bought.outcomes[0].venue_order_id!='old-D'
        assert {o['id'] for o in rig.sdk.orders}=={'old-A','old-C','new-E-1','old-X','old-Y','old-Z'}
    finally:rig.close()
