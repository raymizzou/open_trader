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


def test_trial_pool_keeps_model_ranking_minimum_sizes_and_three_slots(tmp_path,monkeypatch):
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for name,capital,pool in [('A','6','1200'),('B','15','600'),('C','4','240'),('D','1','40')]:
            rig.market(name,minimum=capital,pool=pool)
        assert isinstance(rig.live.read_runtime,LPReadRuntime)
        result=rig.cycle(pool='trial')
        assert [row['token_id'] for row in rig.sdk.posts]==['tA','tC','tD']
        assert [row['price']*row['size'] for row in rig.sdk.posts]==[Decimal('6'),Decimal('4'),Decimal('1')]
        assert len(result.outcomes)==3
        assert all(outcome.status.value=='accepted' for outcome in result.outcomes)
        assert rig.sdk.cancels==[]  # no official result is not a trial cancellation reason
    finally:rig.close()


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
    def __init__(self, tmp_path, monkeypatch, *, trial='20', income='100', wallet='200', clock=None, build_owner=True):
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
                return market.model_copy(update=dict(id=id, condition_id=rig.cid(name),
                    outcomes=market.outcomes.model_copy(update=dict(
                        yes=market.outcomes.yes.model_copy(update=dict(token_id='t'+name)),
                        no=market.outcomes.no.model_copy(update=dict(token_id='n'+name)))),
                    rewards=market.rewards.model_copy(update=dict(rewards_min_size=q)),
                    trading=market.trading.model_copy(update=dict(minimum_order_size=q))))
            def list_markets(self, **kwargs):
                wanted = kwargs.get('condition_ids')
                return [self.get_market(id='m'+n) for n in rig.markets if wanted is None or rig.cid(n) in wanted]
            def get_order_book(self, *, token_id):
                from polymarket.models.clob.order_book import OrderBookLevel
                book = base.get_order_book(token_id=__import__('tests.test_lp_order_registration_contract',fromlist=['TOKEN_ID']).TOKEN_ID)
                name = token_id[1:]
                return book.model_copy(update=dict(market=rig.cid(name), condition_id=rig.cid(name), token_id=token_id, timestamp=rig.now,
                    bids=(OrderBookLevel(price=Decimal('.4'),size=Decimal('10000')),
                        OrderBookLevel(price=Decimal('.39'),size=Decimal('10000'))),
                    asks=(OrderBookLevel(price=Decimal('.42'),size=Decimal('10000')),)))
            def get_order_books(self, *, token_ids):
                return tuple(self.get_order_book(token_id=t) for t in token_ids)
            def list_market_rewards(self, *, condition_id, sponsored=None):
                row = _lp_reward_market_row(condition_id, earning_percentage='100',
                    rate_per_day=str(rig.markets[rig.name(condition_id)]['pool']))
                return (row,)
            def close(self): pass
        class Transport:
            def get_json(self, path, *, params):
                if path == '/rewards/user/markets' and params.get('sponsored') is not True:
                    rows = []
                    for name, hourly in rig.hourly.items():
                        pool = rig.markets[name]['pool']
                        rows.append(_lp_reward_market_row(rig.cid(name),
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
        if not build_owner:
            return
        self.live = LPD2LiveExecution(self.store,self.adapter,account_id=WALLET,venue='polymarket',
            process_epoch='observed-pool-rig',policy=CalculationPolicy(None),load_settings_on_start=True,
            utc_now=clock or (lambda: datetime.now(UTC)))
        self.producer = PolymarketLPService(self.store,self.adapter,clock=clock or (lambda: datetime.now(UTC)))
        self.execution = attach(self.live,self.producer,tmp_path)
        self.live.initialize(ValidatedStartAdmission('observed-pool-rig','offline-permit'))
        self.live.run_once()

    def cid(self,name):return 'c'+name

    def name(self,condition):return condition[1:]

    def market(self,name,*,minimum='1',pool='100'):
        from tests.test_polymarket_lp_risk import _direction
        q=Decimal(minimum)/Decimal('.4')
        direction=_direction(reward_min_size=str(q),minimum_order_size=str(q))
        direction['market'].update(market_id='m'+name,condition_id=self.cid(name),token_id='t'+name,
            metadata_checked_at=self.now,fees_checked_at=self.now)
        direction['book'].update(condition_id=self.cid(name),token_id='t'+name,received_at=self.now,
            bids=[dict(price='.4',size='10000'),dict(price='.39',size='10000')],
            asks=[dict(price='.42',size='10000')])
        direction.update(daily_pool_usd=Decimal(pool),reward_checked_at=self.now)
        direction['history_summary']=dict(condition_id=self.cid(name),token_id='t'+name,state='known',
            checked_at=self.now,amplitude=Decimal('.01'))
        self.markets[name]=dict(condition_id=self.cid(name),token_id='t'+name,market_id='m'+name,
            venue='polymarket',price=Decimal('.4'),quantity=q,direction=direction,pool=Decimal(pool),
            order_type='GTD',expiration=1893456000,post_only=True,send_options=())
        assert self.producer._candidate_pool_record_success(self.cid(name),
            dict(condition_id=self.cid(name),state='eligible',selected_direction=dict(token_id='t'+name)),
            judged_at=self.now,facts=dict(checked_at=self.now,directions=[direction]),global_recovery_generation=7)

    def add_order(self,name,capital,*,order_id=None):
        from tests.test_lp_order_registration_contract import WALLET
        oid=order_id or 'old-'+name
        row=dict(id=oid,market=self.cid(name),market_id='m'+name,asset_id='t'+name,side='BUY',
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
                if o['market']==self.cid(name) and o['side']=='BUY'),Decimal(0))
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
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    release=Event()
    try:
        for name in 'ABCD': rig.market(name,minimum='1')
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','10')]: rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(name):dict(pool='income' if name in 'ABC' else 'trial',episode='old-'+name)
                for name in 'ABCD'})
        measured=rig.measure(dict(A='.6',B='1.0',C='.4',D='.8'))
        assert all(measured['observations'][rig.cid(n)]['pool_result']['state']=='known' for n in 'ABCD'), measured
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
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for name in 'ABC0': rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20')]: rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income',episode='old-'+n) for n in 'ABC'})
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
        rig=AsyncPoolRig(tmp_path/scenario,monkeypatch,trial='100',income='100.01' if scenario=='rounding' else '100')
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
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for name in 'ABCD': rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','1')]: rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income',episode='old-'+n) for n in 'ABC'})
        rig.measure(dict(A='.6',B='1',C='.4',D='.8'))
        rig.tick()
        from time import monotonic,sleep
        end=monotonic()+4
        while True:
            first=rig.execution.lp_auto_scheduled_check()
            if first.rotation_outcomes:break
            assert monotonic()<end,rig.live.rotation_diagnostics()
            sleep(.001)
        rig.finish_cancels(first)
        assert first.selection.targets==(rig.cid('B'),rig.cid('D'),rig.cid('A'))
        assert rig.sdk.posts==[]
        rig.market('E');rig.add_order('E','1')
        rig.measure(dict(E='1.2'))
        rig.release('old-A','old-B','old-C','old-D','old-E')
        # The next income business opportunity uses a fresh qualified capture.
        # Automatic trial fairness is covered independently by P214-progress.
        second=rig.cycle(pool='income',qualified=True)
        rig.finish_cancels(second)
        assert second.selection.targets==(rig.cid('E'),rig.cid('B'),rig.cid('D'))
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[
            ('tE',Decimal('50')),('tB',Decimal('30')),('tD',Decimal('20'))]
        assert len({o.identity.request_id for o in second.outcomes})==3
    finally:
        rig.close()


def test_both_pool_cycles_share_actual_capacity_without_borrowing(tmp_path,monkeypatch):
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    rig=AsyncPoolRig(tmp_path,monkeypatch,wallet='40')
    entered,release=Event(),Event()
    try:
        rig.market('A',minimum='1',pool='10')
        rig.market('B',minimum='15',pool='1000')
        rig.market('C',minimum='5',pool='5000')
        rig.add_order('A','10')
        rig.add_order('A','5',order_id='manual-extra-A')
        rig.add_order('B','15')
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid('A'):dict(pool='income',episode='old-A')})
        rig.sdk.positions=(dict(condition_id=rig.cid('B'),token_id='tB',size='20',average_price='.4'),)
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
                rig.live.run_once(rig.capture(),pool='income')
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
    rig=AsyncPoolRig(tmp_path,monkeypatch)
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
        rig=AsyncPoolRig(tmp_path/disposition,monkeypatch)
        try:
            for name in 'BDA':
                rig.market(name)
                rig.add_order(name,'1')
            if previous_member:
                rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
                    roles={rig.cid('A'):dict(pool='income',episode='old-A')})
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
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for name in 'ABCD': rig.market(name,minimum='1',pool='1000' if name=='C' else '100')
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','1')]:rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income',episode='old-'+n) for n in 'ABC'})
        rig.measure(dict(A='.6',B='1',C='.4',D='.8'))
        replacing=rig.cycle(pool='income');rig.finish_cancels(replacing)
        rig.release('old-A','old-B','old-C','old-D')
        bought=rig.cycle(pool='income');rig.finish_cancels(bought)
        assert {o.identity.target.token_id for o in bought.outcomes}=={'tA','tB','tD'}
        assert not any(o['asset_id']=='tC' for o in rig.sdk.orders)
        old_c=rig.execution.lp_observed_pool_state()['results'][rig.cid('C')]['result']
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
        new_sample=rig.execution.lp_observed_pool_state()['results'][rig.cid('C')]['result']
        assert new_sample['source_order']['order_id']==new_c
        promoted=rig.cycle(pool='income');rig.finish_cancels(promoted)
        assert {o.key[3] for o in promoted.rotation.victims}=={o['id'] for o in rig.sdk.orders}
        assert promoted.selection.targets==(rig.cid('C'),rig.cid('B'),rig.cid('D'))
    finally:
        rig.close()


def test_promoted_size_uses_new_measurement_without_automatic_reshuffle(tmp_path,monkeypatch):
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        rig.market('D',minimum='10')
        rig.market('E',minimum='1')
        rig.add_order('D','10')
        trial=rig.measure(dict(D='.8'))['observations'][rig.cid('D')]['pool_result']
        assert Decimal(trial['buy_capital_usd'])==10
        canceled=rig.cycle(pool='income');rig.finish_cancels(canceled)
        rig.release('old-D')
        promoted=rig.cycle(pool='income');rig.finish_cancels(promoted)
        new_d=promoted.outcomes[0].venue_order_id
        assert promoted.outcomes[0].identity.target.terms.price*promoted.outcomes[0].identity.target.terms.quantity==50
        assert rig.store.lp_advance_trade_generation(rig.store.lp_trade_generation())
        dashboard=rig.execution.refresh_lp_dashboard_snapshot()
        projected=dashboard['lp_observations'][rig.cid('D')]
        assert projected['current_yield_pct_per_hour'] is None
        detached=rig.execution.lp_observed_pool_state()['results'][rig.cid('D')]['result']
        assert detached==trial and detached['source_order']['order_id']=='old-D'
        lower=rig.measure(dict(D='.1'))['observations'][rig.cid('D')]['pool_result']
        assert lower['source_order']['order_id']==new_d
        assert Decimal(lower['buy_capital_usd'])==50
        assert Decimal(lower['yield_pct_per_hour'])==Decimal('.1')
        before=list(rig.sdk.cancels)
        stable=rig.cycle(pool='income');rig.finish_cancels(stable)
        assert rig.sdk.cancels==before and len(rig.sdk.posts)==1
        rig.add_order('E','1')
        rig.measure(dict(E='.2'))
        new_trial=rig.cycle(pool='income');rig.finish_cancels(new_trial)
        assert new_trial.selection.targets==(rig.cid('E'),rig.cid('D'))
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
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    entered,release=Event(),Event()
    try:
        rig.market('D');rig.add_order('D','1');rig.measure(dict(D='.8'))
        canceled=rig.cycle(pool='income');rig.finish_cancels(canceled)
        rig.release('old-D')
        # A real fresh producer has no recovered qualification inventory.
        cold=PolymarketLPService(rig.store,rig.adapter,clock=rig.utc)
        rig.execution._lp=cold
        capture=cold.capture_qualified_pool()
        assert not capture.rows
        rig.execution.lp_auto_set_desired_running(False);settle_control(rig.live)
        scheduler=LPAutoScheduler(rig.execution,clock=rig.utc);rig.scheduler=scheduler
        assert scheduler.run_due() and rig.sdk.posts==[]
        rig.execution.lp_auto_set_desired_running(True);settle_control(rig.live)
        rig.tick();rig.pump(lambda:rig.live.rounds.snapshot().round_number>0)  # real async resume round-zero
        assert rig.sdk.posts==[]
        post=rig.sdk.post_order
        def held(signed):
            entered.set()
            assert release.wait(4),'independent native watchdog'
            return post(signed)
        rig.sdk.post_order=held
        rig.tick()
        def drive_to_post():
            from time import monotonic,sleep
            end=monotonic()+4
            while not entered.is_set():
                scheduler.run_due()
                assert monotonic()<end
                sleep(.001)
            return True
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending=workers.submit(drive_to_post)
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
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    entered,release=Event(),Event()
    try:
        for n in 'DE':rig.market(n)
        rig.add_order('D','1');rig.add_order('E','1')
        old=rig.measure(dict(D='.8'))['observations'][rig.cid('D')]['pool_result']
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
                new=rig.measure(dict(E='1.2'))['observations'][rig.cid('E')]['pool_result']
                assert new['source_order']['order_id']=='old-E'
            finally:release.set()
            fixed=pending.result(timeout=4)
        assert fixed.selection.targets==(rig.cid('D'),)
        assert len(fixed.outcomes)==1
        assert rig.sdk.posts[0]['token_id']=='tD'
        assert rig.sdk.posts[0]['price']*rig.sdk.posts[0]['size']==50
        assert rig.execution.lp_observed_pool_state()['results'][rig.cid('D')]['result']==old
        following=rig.cycle(pool='income');rig.finish_cancels(following)
        assert following.selection.targets==(rig.cid('E'),rig.cid('D'))
        assert {o.key[3] for o in following.rotation.victims}=={fixed.outcomes[0].venue_order_id,'old-E'}
        assert len(rig.sdk.posts)==1
    finally:release.set();rig.close()


@pytest.mark.parametrize('income_state',['new_results','market_unavailable'])
def test_ready_trial_progresses_while_income_results_change_or_wait(tmp_path,monkeypatch,income_state):
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from open_trader.polymarket_lp import PolymarketLPService
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for name in 'DE':rig.market(name)
        rig.add_order('D','50')
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid('D'):dict(pool='income',episode='old-D')})
        rig.measure(dict(D='.8'))
        read=rig.public.get_order_book
        if income_state=='market_unavailable':
            def unavailable(*,token_id):
                if token_id=='tD':raise TimeoutError('offline income-only book unavailable')
                return read(token_id=token_id)
            rig.public.get_order_book=unavailable
        rig.execution._lp=PolymarketLPService(rig.store,rig.adapter,clock=rig.utc)
        rig.tick();rig.pump(lambda:rig.live.state.snapshot().observation.components['open_orders'].checked_at==rig.utc())
        rig.execution._lp=rig.producer
        scheduler=rig.scheduler
        measured=[]
        for _ in range(2):
            rig.tick()
            if income_state=='new_results':
                sample=rig.measure(dict(D='.8'))['observations'][rig.cid('D')]['pool_result']
                measured.append(sample['measured_at'])
            scheduler.request_check();assert scheduler.run_due()
        rig.pump(lambda:len(rig.sdk.posts)==1)
        rig.cycle(pool='trial')  # fresh post-receipt account publication
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
    rig=AsyncPoolRig(tmp_path,monkeypatch,clock_values=clock)
    entered,release=Event(),Event()
    try:
        rig.market('D',minimum='1');rig.add_order('D','1')
        sample=rig.measure(dict(D='.8'))['observations'][rig.cid('D')]['pool_result']
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
                    with pytest.raises(RuntimeError,match='d2_run_in_progress'):rig.live.run_once(rig.capture(),pool='trial')
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
            utc_now=lambda:clock[0],clock=lambda:rig.seconds,
            read_transport=__import__('httpx').MockTransport(rig.http))
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
        expired=rig.execution.lp_observed_pool_state()['results'][rig.cid('D')]
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
        measured=rig.measure(dict(D='.2'))['observations'][rig.cid('D')]['pool_result']
        assert measured['source_order']['order_id']==new.venue_order_id
        assert measured['measured_at']!=sample['measured_at']
    finally:release.set();rig.close()


def test_fill_and_protection_remain_authoritative_during_pool_changes(tmp_path,monkeypatch):
    from threading import Event
    from time import monotonic,sleep
    from tests.test_lp_d2_integration import settle_control
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    entered,release=Event(),Event()
    try:
        for name in 'ABCDP':rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','10')]:rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income',episode='old-'+n) for n in 'ABC'})
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
            rig.sdk.positions=tuple(dict(condition_id=rig.cid(n),token_id='t'+n,size='5',average_price='.4') for n in 'AD')
            protected={**rig.history['old-D'],'id':'protected-SELL','market':rig.cid('P'),'asset_id':'tP',
                'side':'SELL','original_size':Decimal('2'),'size_matched':Decimal('0'),'price':Decimal('.45')}
            rig.sdk.orders=(*rig.sdk.orders,protected)
            rig.sdk.positions=(*rig.sdk.positions,dict(condition_id=rig.cid('P'),token_id='tP',size='2',average_price='.4'))
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

    unreadable=AsyncPoolRig(tmp_path/'unknown-incumbent-market',monkeypatch)
    try:
        for n in 'ABCD':unreadable.market(n);unreadable.add_order(n,'1')
        unreadable.store.lp_save_observed_pool_roles(unreadable.account,expected_revision=0,
            roles={unreadable.cid(n):dict(pool='income',episode='old-'+n) for n in 'ABC'})
        unreadable.measure(dict(A='.6',B='1',C='.4',D='.8'))
        read=unreadable.public.get_order_book
        def missing(*,token_id):
            if token_id=='tC':raise TimeoutError('offline current incumbent market unavailable')
            return read(token_id=token_id)
        unreadable.public.get_order_book=missing
        waiting=unreadable.cycle(pool='income');unreadable.finish_cancels(waiting)
        assert unreadable.sdk.cancels==[] and unreadable.sdk.posts==[]
        assert set(waiting.selection.targets)=={rig.cid('A'),rig.cid('B'),rig.cid('C')}
    finally:unreadable.close()

    # A separate real group proves actual owner drain without sacrificing the
    # audit verification above: close blocks admission but cannot end SDK work.
    import fcntl
    drain=AsyncPoolRig(tmp_path/'group-drain',monkeypatch)
    held,finish=Event(),Event()
    def available():
        with (tmp_path/'group-drain'/'execution.lock').open('a+') as lock:
            try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return False
            fcntl.flock(lock.fileno(),fcntl.LOCK_UN);return True
    try:
        for n in 'ABCD':drain.market(n);drain.add_order(n,'1')
        drain.store.lp_save_observed_pool_roles(drain.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income',episode='old-'+n) for n in 'ABC'})
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
    rig=AsyncPoolRig(tmp_path/'real',monkeypatch)
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
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for n,minimum,pool in [('A','6','1200'),('B','15','600'),('C','4','240'),('D','1','40'),
                ('X','1','100'),('Y','1','100'),('Z','1','100')]:
            rig.market(n,minimum=minimum,pool=pool)
        for n,capital in [('A','6'),('C','4'),('D','1'),('X','50'),('Y','30'),('Z','20')]:rig.add_order(n,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income',episode='old-'+n) for n in 'XYZ'})
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


class AsyncPoolRig(PoolRig):
    """Production async owner; every HTTP request terminates at a local transport."""
    def __init__(self, tmp_path, monkeypatch, *, trial='20',income='100',wallet='200',clock_values=None):
        import httpx, threading
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace
        from open_trader.polymarket_lp import PolymarketLPService
        from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
        from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
        from open_trader.polymarket_lp_market_calculation import CalculationPolicy
        from open_trader.polymarket_lp_scheduler import LPAutoScheduler
        from tests.test_lp_d2_execution_bridge import attach
        self.dates=_pool_test_clock(monkeypatch) if clock_values is None else clock_values
        self.origin=self.dates[0]
        self.utc=lambda:self.dates[0]
        super().__init__(tmp_path,monkeypatch,trial=trial,income=income,wallet=wallet,clock=self.utc,build_owner=False)
        self.wake=threading.Event();self.release_http=threading.Event()
        self.http_mode='ok';self.requests=[];self.failed_book_tokens=set();self.http_gate=None
        self.sdk._ctx.environment=SimpleNamespace(gamma_url='http://127.0.0.1',
            clob_url='http://127.0.0.1',data_url='http://127.0.0.1',
            standard_exchange=self.sdk.environment.standard_exchange)
        self.sdk._ctx.wallet_type='GNOSIS_SAFE'
        self.sdk._ctx.secure_clob._header_resolver=lambda method,path,body:{'test-owned-get':'yes'}
        self.live=LPD2LiveExecution(self.store,self.adapter,account_id=self.adapter.config.wallet_address,
            venue='polymarket',process_epoch='async-pools',policy=CalculationPolicy(None),
            load_settings_on_start=True,clock=lambda:self.seconds,utc_now=self.utc,
            read_transport=httpx.MockTransport(self.http))
        self.producer=PolymarketLPService(self.store,self.adapter,clock=self.utc)
        self.execution=attach(self.live,self.producer,tmp_path)
        self.live.initialize(ValidatedStartAdmission('async-pools','offline-permit'))
        self.scheduler=LPAutoScheduler(self.execution,clock=self.utc)
        self.pump(lambda:self.live.rounds.snapshot().round_number>0)

    @property
    def seconds(self):return (self.utc()-self.origin).total_seconds()

    def cid(self,name):return '0x'+format(ord(name),'064x')

    def name(self,condition):return chr(int(condition,16))

    async def http(self,req):
        import asyncio,httpx,json
        from tests.test_lp_async_source_contract import market,reward,book
        from tests.test_lp_async_live_contract import sdk_wire
        source=req.headers['x-lp-source'];oid=req.headers['x-lp-order']
        self.requests.append((source,oid,self.seconds))
        if self.http_gate is not None:
            await asyncio.get_running_loop().run_in_executor(None,self.http_gate.wait)
        if source=='book' and self.http_mode in ('held','swallow'):
            try:await asyncio.get_running_loop().run_in_executor(None,self.release_http.wait)
            except asyncio.CancelledError:
                if self.http_mode!='swallow':raise
                await asyncio.get_running_loop().run_in_executor(None,self.release_http.wait)
        if source=='book' and (self.http_mode=='failed' or req.url.params['token_id'] in self.failed_book_tokens):return httpx.Response(503)
        if source=='balance':value=sdk_wire(self.sdk.get_balance_allowance())
        elif source=='open_orders':
            rows=sdk_wire(self.sdk.orders)
            for row in rows:
                row['expiration']='0' if row.get('expiration') is None else str(row['expiration'])
                row['created_at']=int(self.utc().timestamp())
            value=dict(data=rows,next_cursor='LTE=')
        elif source=='positions':value=[dict(conditionId=row['condition_id'],asset=row['token_id'],
            size=str(row['size']),avgPrice=str(row['average_price'])) for row in self.sdk.positions]
        elif source=='metadata':
            if req.url.path=='/events/keyset':value=dict(events=[dict(id='7',slug='event',title='Event')],next_cursor=None)
            else:
                cid=req.url.params['condition_ids'];name=self.name(cid);q=str(self.markets[name]['quantity'])
                value=dict(markets=[dict(market(),id='m'+name,conditionId=cid,
                    clobTokenIds=json.dumps(['t'+name,'n'+name]),orderMinSize=float(q),rewardsMinSize=float(q))],next_cursor=None)
        elif source=='book':
            model=self.public.get_order_book(token_id=req.url.params['token_id'])
            value=dict(sdk_wire(model),timestamp=str(int(model.timestamp.timestamp()*1000)))
        elif source in ('native_rewards','sponsored_rewards'):
            cid=req.url.path.rsplit('/',1)[-1];r=reward();r['condition_id']=cid
            r['tokens']=[dict(token_id='t'+self.name(cid),outcome='Yes',price='.4')]
            r['rewards_config']=[dict(r['rewards_config'][0],rate_per_day=str(self.markets[self.name(cid)]['pool']))]
            value=dict(data=[r],next_cursor='LTE=')
        elif source=='order':
            value=sdk_wire(self.history[req.url.path.rsplit('/',1)[-1]])
            value.update(expiration='0' if value.get('expiration') is None else str(value['expiration']),created_at=int(self.utc().timestamp()))
        elif source=='scoring':value=dict(scoring=True)
        elif source=='fills':value=dict(data=sdk_wire(self.sdk.list_account_trades(market=req.url.params['market'])),next_cursor='LTE=')
        else:raise AssertionError(source)
        return httpx.Response(200,json=value)

    def pump(self,predicate,*,timeout=4):
        import time
        end=time.monotonic()+timeout
        while not predicate():
            self.scheduler.run_due()
            assert not self.scheduler.snapshot()['last_check_error'], self.live.diagnostics()
            if time.monotonic()>=end:
                observation=self.live.state.snapshot().observation
                raise AssertionError(repr(dict(status=self.live.status(),
                    sources=dict(observation.source_frame.sources) if observation else {},
                    diagnostics=self.live.diagnostics(),requests=self.requests)))
            self.wake.wait(.01);self.wake.clear()

    def tick(self,seconds=5):
        from datetime import timedelta
        self.dates[0]+=timedelta(seconds=seconds);self.now=self.utc()
        self.live.read_runtime.wake();self.scheduler.request_check()

    def converge_current(self,predicate,*,timeout=4):
        """After wake assertions, follow actual read deadlines to current proof."""
        import time
        end=time.monotonic()+timeout
        while not predicate():
            def progress():
                status=self.live.read_runtime.status()
                due=status['next_dependency_monotonic']
                return (predicate() or (status['ready_count']==0
                    and not any(scope['active'] for scope in status['scopes'])
                    and status['observed_monotonic']>=self.seconds
                    and due is not None and due>self.seconds))
            self.pump(progress,timeout=max(0,end-time.monotonic()))
            if predicate():return
            status=self.live.read_runtime.status()
            due=status['next_dependency_monotonic']
            assert time.monotonic()<end,self.live.diagnostics()
            if (status['ready_count']==0 and not any(scope['active'] for scope in status['scopes'])
                    and due is not None and due>self.seconds):
                self.tick(due-self.seconds)

    def finish_income_rebuild(self,condition,token,*,after,timeout=4):
        """Wait for candidate proof and its BUY in a receipt-current account."""
        import time
        end=time.monotonic()+timeout;candidate_seen=False
        self.tick()
        while True:
            result=self.live.dispatch_rotation(self.live.run_once(
                self.producer.capture_qualified_pool(),pool='income'))
            self.finish_cancels(result)
            snapshot=self.live.state.snapshot();observation=snapshot.observation
            if observation is not None:
                source=observation.source_frame.sources.get('market_income:'+condition+':'+token)
                candidate_seen=candidate_seen or (source is not None and source.complete
                    and source.error is None and source.checked_at is not None
                    and source.checked_at>=after
                    and 0<=(self.utc()-source.checked_at).total_seconds()<=10)
                if (candidate_seen and observation.orders_complete
                        and observation.components['open_orders'].checked_at>after
                        and observation.read_ticket.trade_version_at_start==snapshot.trade_version
                        and any(o.key[1:3]==(condition,token) for o in observation.orders)
                        and self.live.status()['native_inflight']==0):return
            assert time.monotonic()<end,(self.live.status(),self.live.rotation_diagnostics())
            status=self.live.read_runtime.status();due=status['next_dependency_monotonic']
            if (status['ready_count']==0 and not any(scope['active'] for scope in status['scopes'])
                    and status['observed_monotonic']>=self.seconds
                    and self.live.status()['native_inflight']==0
                    and due is not None and due>self.seconds):
                self.tick(due-self.seconds)
            self.wake.wait(.01);self.wake.clear()

    def cycle(self,*,pool=None,qualified=False):
        import time
        self.tick()
        end=time.monotonic()+4;latest=None
        while True:
            result=self.live.dispatch_rotation(self.live.run_once(
                self.producer.capture_qualified_pool() if qualified else self.capture(),pool=pool))
            if result.selection is not None:latest=result
            if result.outcomes or result.rotation_outcomes:return result
            status=self.live.read_runtime.status()
            idle=status['ready_count']==0 and not any(scope['active'] for scope in status['scopes'])
            if idle:
                observation=latest.acceptance.observation if latest is not None else None
                if observation is not None and observation.accepted_at==self.utc():return latest
                # A cancel/BUY receipt may invalidate a ticket already in flight.
                # Follow the real next start, preserving the five-second cadence;
                # do not call an older accepted snapshot a fresh account read.
                due=status['next_dependency_monotonic']
                if due is not None and due>self.seconds:self.tick(due-self.seconds)
            assert time.monotonic()<end,(self.live.status(),self.live.rotation_diagnostics())
            time.sleep(.001)

    def close(self):
        self.release_http.set()
        super().close()


@pytest.mark.parametrize('source_state',['complete','failed','expired','stale_delivery'])
def test_observed_pools_use_owned_async_current_reads(tmp_path,monkeypatch,source_state,pool_async_watchdog):
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    # A completed measured source can leave before promotion; trial qualification
    # is independently cold. Only a real fresh owned market result authorizes D50.
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        assert isinstance(rig.live.read_runtime,LPReadRuntime)
        rig.market('D');rig.add_order('D','1');rig.measure(dict(D='.8'))
        sample=rig.execution.lp_observed_pool_state()['results'][rig.cid('D')]['result']
        projected=rig.execution.lp_observed_pool_state(as_of=rig.utc())['results'][rig.cid('D')]
        assert projected['eligible'] and projected['age_seconds']==0, (rig.utc(),sample,projected)
        assert sample['source_order']['order_id']=='old-D'
        assert sample['source_order']['token_id']=='tD'
        assert sample['account_id']==rig.account
        assert Decimal(sample['source_order']['price'])==Decimal('.4')
        assert Decimal(sample['source_order']['remaining_quantity'])==Decimal('2.5')
        assert Decimal(sample['buy_capital_usd'])==1
        assert rig.live.rounds.snapshot().round_number>0
        assert rig.live.state.snapshot().current.effective_running
        rig.release('old-D')
        rig.execution._lp=PolymarketLPService(rig.store,rig.adapter,clock=rig.utc)
        assert not rig.execution._lp.capture_qualified_pool().rows
        if source_state=='expired':
            rig.tick(121)
            rig.pump(lambda:rig.live.state.snapshot().observation.components['open_orders'].checked_at==rig.utc())
            view=rig.execution.lp_observed_pool_state()['results'][rig.cid('D')]
            assert view['age_seconds']==121 and not view['eligible']
            assert view['result']==sample
            assert rig.sdk.posts==rig.sdk.cancels==[]
            return
        rig.http_mode='held';rig.tick()
        rig.pump(lambda:rig.live.state.snapshot().observation is not None and
            rig.live.state.snapshot().observation.components['open_orders'].checked_at==rig.utc())
        rig.pump(lambda:any(source=='book' and oid.startswith('income:') for source,oid,_ in rig.requests))
        assert rig.sdk.posts==[] and rig.sdk.cancels==[]
        rig.http_mode='failed' if source_state=='failed' else 'ok';rig.release_http.set()
        if source_state=='stale_delivery':
            import time
            end=time.monotonic()+4
            while not any(scope['key'][3]=='income' and not scope['active'] for scope in rig.live.read_runtime.status()['scopes']):
                assert time.monotonic()<end
                time.sleep(.001)
            # Successful HTTP completion and owner collection are different
            # boundaries. Keep the original source time when collection is late.
            rig.http_mode='held';rig.release_http.clear();rig.tick(11)
            rig.pump(lambda:any(name.startswith('market_income:') for name in
                rig.live.state.snapshot().observation.source_frame.sources))
            rig.scheduler.run_due();rig.scheduler.run_due()
            assert rig.sdk.posts==rig.sdk.cancels==[]
            assert rig.execution.lp_observed_pool_state()['results'][rig.cid('D')]['eligible']
            return
        if source_state=='failed':
            rig.pump(lambda:any(source.error for name,source in
                rig.live.state.snapshot().observation.source_frame.sources.items() if name.startswith('market_income:')))
            assert rig.sdk.posts==rig.sdk.cancels==[]
            rig.http_mode='ok';rig.tick()
        rig.pump(lambda:len(rig.sdk.posts)==1)
        assert rig.sdk.posts[0]['token_id']=='tD'
        assert rig.sdk.posts[0]['price']*rig.sdk.posts[0]['size']==50
        assert rig.execution.lp_observed_pool_state()['results'][rig.cid('D')]['result']==sample
        rig.tick();rig.scheduler.run_due()
        assert len(rig.sdk.posts)==1 and rig.sdk.cancels==[]
    finally:rig.close()


@pytest.fixture
def pool_async_watchdog():
    import faulthandler
    faulthandler.dump_traceback_later(30,exit=True)
    try:yield
    finally:faulthandler.cancel_dump_traceback_later()


@pytest.mark.parametrize('income_state',['fresh_results','held_market'])
def test_observed_pool_due_work_survives_async_wait_and_wakeup(tmp_path,monkeypatch,income_state,pool_async_watchdog):
    import time
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for name in 'DE':rig.market(name)
        rig.add_order('D','50')
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid('D'):dict(pool='income',episode='old-D')})
        rig.measure(dict(D='.8'))
        if income_state=='held_market':rig.http_mode='held'
        measured=[]
        for _ in range(2):
            rig.tick()
            measured.append(rig.measure(dict(D='.8'))['observations'][rig.cid('D')]['pool_result']['measured_at'])
            rig.pump(lambda:rig.live.state.snapshot().observation.components['open_orders'].checked_at==rig.utc())
            rig.scheduler.run_due();rig.scheduler.run_due()
            if rig.sdk.posts:break
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[('tE',Decimal(1))]
        assert rig.sdk.cancels==[]
        assert {r['id'] for r in rig.sdk.orders}=={'old-D','new-E-1'}
        # Drain already published work before checking real dependency sleep.
        rig.pump(lambda:rig.live.status()['plan_wait'] is not None)
        assert all(not rig.scheduler.run_due() for _ in range(20))
        if income_state=='held_market':
            assert any(source=='book' and oid=='old-D' for source,oid,_ in rig.requests)
            assert not rig.release_http.is_set()
            stamp=rig.seconds
            rig.http_mode='ok';rig.release_http.set()
            end=time.monotonic()+4
            while not rig.live.read_runtime.status()['ready_count']:
                assert time.monotonic()<end
                time.sleep(.001)
            assert rig.scheduler.run_due(),'source completion must wake real scheduler at unchanged business time'
            rig.pump(lambda:any(key[3]=='old-D' and y.yield_pct_per_hour.state=='known'
                for key,y in rig.live.state.snapshot().yields.items()))
            assert rig.seconds==stamp
        rig.tick()
        rig.pump(lambda:rig.live.state.snapshot().observation.components['open_orders'].checked_at==rig.utc())
        assert len(rig.sdk.posts)==1 and rig.sdk.cancels==[]
        assert rig.live.rounds.snapshot().round_number>1
    finally:rig.close()



def test_pool_group_cancel_wakes_deferred_async_protection(tmp_path,monkeypatch,pool_async_watchdog):
    from threading import Event
    import time
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    group_entered,group_release,other_entered,other_release=Event(),Event(),Event(),Event()
    notice=Event();protection_entered=Event();planned=[];other=None
    try:
        for name in 'ABCDEF':rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','10'),('E','1'),('F','1')]:
            rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income' if n in 'ABC' else 'trial',episode='old-'+n) for n in 'ABCDEF'})
        # A partially matched BUY has no queue-baseline cancellation path;
        # isolate the existing 11th read failure without disabling its guard.
        rig.history['old-E']['size_matched']=Decimal('.5')
        rig.failed_book_tokens={'tE'}
        # Ten real failures are below the existing read-failure cancellation
        # threshold. All current facts are owned runtime publications.
        def failures():
            return next((f.consecutive_failures for k,f in rig.live.state.snapshot().read_failures.items()
                if k[3]=='old-E'),0)
        for expected in range(1,11):
            rig.tick()
            rig.pump(lambda:failures()==expected)
        assert rig.sdk.cancels==[] and failures()==10
        partial=next(o for o in rig.live.state.snapshot().orders if o.key[3]=='old-E')
        assert (partial.original_size.value,partial.size_matched.value,partial.remaining.value)==(Decimal('2.5'),Decimal('.5'),Decimal('2'))
        rig.measure(dict(A='.6',B='1.0',C='.4',D='.8'))
        actual_cancel=rig.sdk.cancel_orders
        def cancel(*,order_ids):
            if len(order_ids)>1:
                assert set(order_ids)=={'old-A','old-B','old-C','old-D'}
                group_entered.set();assert group_release.wait(8),'held group external watchdog'
                rig.sdk.cancels.append(tuple(order_ids))
                return dict(canceled=tuple(oid for oid in order_ids if oid!='old-C'),not_canceled={'old-C':'offline unknown'})
            if order_ids==('old-F',):
                other_entered.set();assert other_release.wait(8),'held peer external watchdog'
            if order_ids==('old-E',):
                protection_entered.set();rig.release('old-E')
            return actual_cancel(order_ids=order_ids)
        rig.sdk.cancel_orders=cancel
        # Public D2 pool planning is used to retain the real group result/IDs;
        # the subsequent protection and wakeup use the real service scheduler.
        rig.tick()
        end=time.monotonic()+4
        while not planned:
            result=rig.live.dispatch_rotation(rig.live.run_once(rig.producer.capture_qualified_pool(),pool='income'))
            if result.rotation_outcomes:planned.append(result)
            assert time.monotonic()<end,rig.live.rotation_diagnostics()
            if not planned:time.sleep(.001)
        assert group_entered.wait(2)
        group=planned[0];handle=group.rotation_outcomes[0].handle
        assert len(group.rotation_outcomes)==4
        assert len({out.handle.identity.request_id for out in group.rotation_outcomes})==4
        other=rig.live.cancel_independent((rig.live.account_id,rig.cid('F'),'tF','old-F'))
        assert other is not None and other_entered.wait(2)
        assert rig.live.status()['native_inflight']==2
        assert rig.execution._lp_d2_lease_users==2
        rig.pump(lambda:failures()==11 and
            rig.live.diagnostics()['cancel_blocks']['by_reason'].get('native_capacity',0)>0)
        assert not protection_entered.is_set()
        failure=next(f for k,f in rig.live.state.snapshot().read_failures.items() if k[3]=='old-E')
        assert failure.consecutive_failures==11 and failure.protection_key is not None
        # Wait for all unrelated HTTP mailboxes to be published and consumed,
        # then observe only the held group's real physical completion.
        rig.pump(lambda:rig.live.read_runtime.status()['ready_count']==0 and
            not any(s['active'] for s in rig.live.read_runtime.status()['scopes']))
        def wake():notice.set();rig.scheduler.request_check()
        rig.execution.set_lp_auto_wakeup(wake)
        notice.clear();before=list(rig.requests);stamp=rig.seconds
        assert not handle.done() and rig.live.status()['native_inflight']==2
        group_release.set();handle._thread.join(3);assert not handle._thread.is_alive()
        assert rig.execution._lp_d2_lease_users==1
        assert notice.wait(.5),'physical group completion must notify existing scheduler'
        assert rig.requests==before  # completion itself needs no HTTP or status GET
        assert rig.scheduler.run_due()
        rig.pump(protection_entered.is_set)
        assert rig.seconds==stamp
        assert all(t==stamp for _,_,t in rig.requests[len(before):])
        assert not other_release.is_set() and rig.sdk.posts==[]
        outcomes=[out.handle.result(0) for out in group.rotation_outcomes]
        assert {out.venue_order_id for out in outcomes}=={'old-A','old-B','old-C','old-D'}
        assert {out.venue_order_id for out in outcomes if out.status.value=='accepted'}=={'old-A','old-B','old-D'}
        assert next(out for out in outcomes if out.venue_order_id=='old-C').status.value=='unknown_error'
        snapshot=rig.live.state.snapshot()
        partial=next(o for o in snapshot.orders if o.key[3]=='old-E')
        assert (partial.original_size.value,partial.size_matched.value)==(Decimal('2.5'),Decimal('.5'))
        assert partial.remaining.state=='unknown'  # final quantity awaits exact venue proof
        for order in snapshot.orders:
            if order.key[3] in {'old-A','old-B','old-D'}:
                assert order.terminal_proof.matched.state=='unknown'  # cancel ACK is not final fill
        assert sum(batch==('old-E',) for batch in rig.sdk.cancels)==1
        rig.tick()
        rig.converge_current(lambda:next(o for o in rig.live.state.snapshot().orders if o.key[3]=='old-E').remaining.state=='known')
        final=next(o for o in rig.live.state.snapshot().orders if o.key[3]=='old-E')
        assert final.terminal_proof.matched.value==Decimal('.5') and final.remaining.value==2
        for _ in range(3):rig.scheduler.run_due()
        assert sum(batch==('old-E',) for batch in rig.sdk.cancels)==1
    finally:
        group_release.set();other_release.set()
        if other:other._thread.join(3)
        rig.close()


def test_pool_group_completion_preserves_early_wake_until_physical_exit(tmp_path,monkeypatch,pool_async_watchdog):
    from threading import Event
    import time
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    group_entered,group_release,other_entered,other_release=Event(),Event(),Event(),Event()
    notice=Event();callback_release=Event();http_release=Event();protection_entered=Event();planned=[];other=None
    try:
        for name in 'ABCDEF':rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','10'),('E','1'),('F','1')]:
            rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income' if n in 'ABC' else 'trial',episode='old-'+n) for n in 'ABCDEF'})
        # A partially matched BUY has no queue-baseline cancellation path;
        # isolate the existing 11th read failure without disabling its guard.
        rig.history['old-E']['size_matched']=Decimal('.5')
        rig.failed_book_tokens={'tE'}
        # Ten real failures are below the existing read-failure cancellation
        # threshold. All current facts are owned runtime publications.
        def failures():
            return next((f.consecutive_failures for k,f in rig.live.state.snapshot().read_failures.items()
                if k[3]=='old-E'),0)
        for expected in range(1,11):
            rig.tick()
            rig.pump(lambda:failures()==expected)
        assert rig.sdk.cancels==[] and failures()==10
        partial=next(o for o in rig.live.state.snapshot().orders if o.key[3]=='old-E')
        assert (partial.original_size.value,partial.size_matched.value,partial.remaining.value)==(Decimal('2.5'),Decimal('.5'),Decimal('2'))
        rig.measure(dict(A='.6',B='1.0',C='.4',D='.8'))
        actual_cancel=rig.sdk.cancel_orders
        def cancel(*,order_ids):
            if len(order_ids)>1:
                assert set(order_ids)=={'old-A','old-B','old-C','old-D'}
                group_entered.set();assert group_release.wait(8),'held group external watchdog'
                rig.sdk.cancels.append(tuple(order_ids))
                return dict(canceled=tuple(oid for oid in order_ids if oid!='old-C'),not_canceled={'old-C':'offline unknown'})
            if order_ids==('old-F',):
                other_entered.set();assert other_release.wait(8),'held peer external watchdog'
            if order_ids==('old-E',):
                protection_entered.set();rig.release('old-E')
            return actual_cancel(order_ids=order_ids)
        rig.sdk.cancel_orders=cancel
        # Public D2 pool planning is used to retain the real group result/IDs;
        # the subsequent protection and wakeup use the real service scheduler.
        rig.tick()
        end=time.monotonic()+4
        while not planned:
            result=rig.live.dispatch_rotation(rig.live.run_once(rig.producer.capture_qualified_pool(),pool='income'))
            if result.rotation_outcomes:planned.append(result)
            assert time.monotonic()<end,rig.live.rotation_diagnostics()
            if not planned:time.sleep(.001)
        assert group_entered.wait(2)
        group=planned[0];handle=group.rotation_outcomes[0].handle
        assert len(group.rotation_outcomes)==4
        assert len({out.handle.identity.request_id for out in group.rotation_outcomes})==4
        other=rig.live.cancel_independent((rig.live.account_id,rig.cid('F'),'tF','old-F'))
        assert other is not None and other_entered.wait(2)
        assert rig.live.status()['native_inflight']==2
        assert rig.execution._lp_d2_lease_users==2
        rig.pump(lambda:failures()==11 and
            rig.live.diagnostics()['cancel_blocks']['by_reason'].get('native_capacity',0)>0)
        assert not protection_entered.is_set()
        failure=next(f for k,f in rig.live.state.snapshot().read_failures.items() if k[3]=='old-E')
        assert failure.consecutive_failures==11 and failure.protection_key is not None
        # Wait for all unrelated HTTP mailboxes to be published and consumed,
        # then observe only the held group's real physical completion.
        rig.pump(lambda:rig.live.read_runtime.status()['ready_count']==0 and
            not any(s['active'] for s in rig.live.read_runtime.status()['scopes']))
        def wake():
            rig.scheduler.request_check()
            notice.set()
            if __import__('threading').current_thread().name=='lp-pool-group-cancel':
                assert callback_release.wait(8),'completion callback external watchdog'
        rig.execution.set_lp_auto_wakeup(wake)
        notice.clear();before=list(rig.requests);stamp=rig.seconds
        rig.http_gate=http_release
        assert not handle.done() and rig.live.status()['native_inflight']==2
        group_release.set()
        assert notice.wait(2),'actual group completion must enter registered callback'
        assert handle._thread.is_alive()
        assert rig.scheduler.run_due(),'drive early wake before physical thread exit'
        assert not protection_entered.is_set()
        assert rig.live.status()['native_inflight']==2
        assert rig.execution._lp_d2_lease_users==1
        callback_release.set();handle._thread.join(3)
        assert not handle._thread.is_alive()
        # Receipt invalidation may start current-account HTTP, but the transport
        # gate prevents any unrelated publication/wake masking this interleaving.
        assert rig.scheduler.run_due()
        rig.pump(protection_entered.is_set)
        assert rig.seconds==stamp
        assert all(t==stamp for _,_,t in rig.requests[len(before):])
        assert not other_release.is_set() and rig.sdk.posts==[]
        outcomes=[out.handle.result(0) for out in group.rotation_outcomes]
        assert {out.venue_order_id for out in outcomes}=={'old-A','old-B','old-C','old-D'}
        assert {out.venue_order_id for out in outcomes if out.status.value=='accepted'}=={'old-A','old-B','old-D'}
        assert next(out for out in outcomes if out.venue_order_id=='old-C').status.value=='unknown_error'
        snapshot=rig.live.state.snapshot()
        partial=next(o for o in snapshot.orders if o.key[3]=='old-E')
        assert (partial.original_size.value,partial.size_matched.value)==(Decimal('2.5'),Decimal('.5'))
        assert partial.remaining.state=='unknown'  # final quantity awaits exact venue proof
        for order in snapshot.orders:
            if order.key[3] in {'old-A','old-B','old-D'}:
                assert order.terminal_proof.matched.state=='unknown'  # cancel ACK is not final fill
        assert sum(batch==('old-E',) for batch in rig.sdk.cancels)==1
        http_release.set();rig.http_gate=None
        rig.tick()
        rig.converge_current(lambda:next(o for o in rig.live.state.snapshot().orders if o.key[3]=='old-E').remaining.state=='known')
        final=next(o for o in rig.live.state.snapshot().orders if o.key[3]=='old-E')
        assert final.terminal_proof.matched.value==Decimal('.5') and final.remaining.value==2
        for _ in range(3):rig.scheduler.run_due()
        assert sum(batch==('old-E',) for batch in rig.sdk.cancels)==1
    finally:
        http_release.set();callback_release.set();group_release.set();other_release.set()
        if other:other._thread.join(3)
        rig.close()



@pytest.mark.parametrize('native_result',['accepted','unknown'])
def test_pool_restart_and_expiry_preserve_owned_async_holds(tmp_path,monkeypatch,native_result,pool_async_watchdog):
    import httpx
    from threading import Event
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_d2_execution_bridge import attach
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    entered,release=Event(),Event()
    try:
        rig.market('D');rig.add_order('D','1');rig.measure(dict(D='.8'))
        sample=rig.execution.lp_observed_pool_state()['results'][rig.cid('D')]['result']
        actual_cancel=rig.sdk.cancel_orders
        def held(*,order_ids):
            assert order_ids==('old-D',)
            entered.set();assert release.wait(8),'restart native external watchdog'
            if native_result=='unknown':
                rig.sdk.cancels.append(tuple(order_ids));raise TimeoutError('offline unknown cancel result')
            rig.release('old-D')
            return actual_cancel(order_ids=order_ids)
        rig.sdk.cancel_orders=held
        rig.tick()
        rig.pump(entered.is_set)
        rig.http_mode='swallow';rig.tick()
        rig.pump(lambda:any(source=='book' and oid=='old-D' and stamp==rig.seconds
            for source,oid,stamp in rig.requests))
        assert rig.sdk.posts==[] and rig.live.status()['native_inflight']==1
        assert rig.store.lp_load_observed_pool_roles(rig.account)['roles'][rig.cid('D')]['promotion_pending']
        rig.tick(6)
        rig.pump(lambda:rig.live.read_runtime.status()['cleanup_faults']>0)
        old=rig.live
        assert old.close(timeout=0) is False
        state=old.status()
        assert state['native_inflight']==1
        assert state['read_runtime']['owned_buy_scopes']==1
        assert state['read_runtime']['cleanup_faults']>0
        assert rig.execution._lp_d2_lease_users==1
        rig.tick(121)
        original=rig.execution.lp_observed_pool_state()['results'][rig.cid('D')]
        assert original['result']==sample and not original['eligible']
        assert rig.sdk.posts==[] and old.status()['native_inflight']==1
        release.set();rig.release_http.set()
        assert old.close(timeout=3)
        assert rig.execution._lp_d2_lease_users==0
        assert rig.store.lp_observations(rig.account)[rig.cid('D')]['pool_result']==sample
        # Only real resource drain permits a replacement owner. It starts from
        # current Store facts, never from an old income group/BUY request plan.
        rig.http_mode='ok'
        rig.live=LPD2LiveExecution(rig.store,rig.adapter,account_id=old.account_id,venue='polymarket',
            process_epoch='async-pool-restart',policy=CalculationPolicy(None),load_settings_on_start=True,
            clock=lambda:rig.seconds,utc_now=rig.utc,read_transport=httpx.MockTransport(rig.http))
        rig.producer=PolymarketLPService(rig.store,rig.adapter,clock=rig.utc)
        rig.execution=attach(rig.live,rig.producer,tmp_path)
        rig.live.initialize(ValidatedStartAdmission('async-pool-restart','offline-permit'))
        rig.scheduler=LPAutoScheduler(rig.execution,clock=rig.utc)
        rig.market('D')  # fresh normal trial qualification, no new measured sample
        rig.pump(lambda:rig.live.rounds.snapshot().round_number>0)
        if native_result=='accepted':
            rig.tick()
            rig.pump(lambda:len(rig.sdk.posts)==1)
            assert rig.sdk.posts[0]['price']*rig.sdk.posts[0]['size']==1
            assert {r['id'] for r in rig.sdk.orders}=={'new-D-1'}
            assert rig.store.lp_load_observed_pool_roles(rig.account)['roles'][rig.cid('D')]['episode']=='new-D-1'
        else:
            rig.tick()
            rig.pump(lambda:rig.live.state.snapshot().observation is not None and
                rig.live.state.snapshot().observation.components['open_orders'].checked_at==rig.utc())
            assert rig.sdk.posts==[] and 'old-D' in {r['id'] for r in rig.sdk.orders}
        assert rig.execution.lp_observed_pool_state()['results'][rig.cid('D')]['result']==sample
        assert not rig.execution.lp_observed_pool_state()['results'][rig.cid('D')]['eligible']
    finally:
        release.set();rig.release_http.set();rig.close()



def test_async_read_protection_precedes_pool_rebuild(tmp_path,monkeypatch,pool_async_watchdog):
    from tests.test_lp_d2_integration import settle_control
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for name in 'ABCD':rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','10')]:rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income' if n in 'ABC' else 'trial',episode='old-'+n) for n in 'ABCD'})
        rig.history['old-B']['size_matched']=Decimal('3')
        rig.failed_book_tokens={'tB'}
        actual_cancel=rig.sdk.cancel_orders
        def unknown(*,order_ids):
            if order_ids==('old-B',):
                rig.sdk.cancels.append(tuple(order_ids));raise TimeoutError('offline unknown exact protection')
            return actual_cancel(order_ids=order_ids)
        rig.sdk.cancel_orders=unknown
        def failure():
            return next((f for k,f in rig.live.state.snapshot().read_failures.items() if k[3]=='old-B'),None)
        for count in range(1,12):
            rig.tick()
            rig.pump(lambda:failure() is not None and failure().consecutive_failures==count)
            if count<11:assert rig.sdk.cancels==[]
        rig.pump(lambda:rig.sdk.cancels==[('old-B',)] and rig.live.status()['native_inflight']==0)
        assert failure().consecutive_failures==11 and failure().protection_key is not None
        before={t.request_id for p in rig.live.state.snapshot().progress.values() for t in p.cancel_targets
            if t.order_key[3]=='old-B'}
        assert len(before)==1
        rig.measure(dict(A='.6',B='1.0',C='.4',D='.8'))
        rig.failed_book_tokens.clear();rig.tick()
        rig.pump(lambda:rig.live.state.snapshot().observation.components['open_orders'].checked_at==rig.utc()
            and any(k[3]=='old-B' and y.yield_pct_per_hour.state=='known'
                for k,y in rig.live.state.snapshot().yields.items()))
        for _ in range(3):rig.scheduler.run_due()
        assert failure().protection_key is not None  # later success cannot unlatch
        assert rig.sdk.cancels==[('old-B',)] and rig.sdk.posts==[]
        partial=next(o for o in rig.live.state.snapshot().orders if o.key[3]=='old-B')
        assert (partial.original_size.value,partial.size_matched.value,partial.remaining.value)==(75,3,72)
        assert not rig.sdk.market_orders  # protection never forces liquidation
        # Common pause still permits existing exact protection. A replacement
        # BUY of the same market cannot inherit the old order's request/latch.
        rig.execution.lp_auto_set_desired_running(False);settle_control(rig.live)
        rig.release('old-B');rig.add_order('B','30',order_id='replacement-B')
        rig.tick()
        rig.pump(lambda:any(k[3]=='replacement-B' for k in rig.live.state.snapshot().read_failures))
        new=next(f for k,f in rig.live.state.snapshot().read_failures.items() if k[3]=='replacement-B')
        assert new.consecutive_failures==0 and new.protection_key is None
        after={t.request_id for p in rig.live.state.snapshot().progress.values() for t in p.cancel_targets
            if t.order_key[3]=='old-B'}
        assert after==before
        assert rig.sdk.cancels==[('old-B',)] and rig.sdk.posts==[] and not rig.sdk.market_orders
    finally:rig.close()


@pytest.mark.parametrize('manual_market',['A','C','D'])
def test_income_rebuild_preserves_unclassified_same_market_orders(tmp_path,monkeypatch,manual_market,pool_async_watchdog):
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for name in 'ABCD':rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','10')]:rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income' if n in 'ABC' else 'trial',episode='old-'+n) for n in 'ABCD'})
        measured=rig.measure(dict(A='.6',B='1.0',C='.4',D='.8'))
        assert all(measured['observations'][rig.cid(n)]['pool_result']['state']=='known' for n in 'ABCD')
        manual='manual-extra-'+manual_market
        rig.add_order(manual_market,'5',order_id=manual)
        planned=rig.cycle(pool='income',qualified=True);rig.finish_cancels(planned)
        sent={oid for batch in rig.sdk.cancels for oid in batch}
        assert manual not in sent,'pool authority must never cancel an unclassified same-market BUY'
        assert sent=={'old-A','old-B','old-C','old-D'}
        assert rig.sdk.posts==[]
        snapshot=rig.live.state.snapshot()
        assert sum((o.price.value*o.remaining.value for o in planned.acceptance.observation.orders),Decimal(0))==Decimal('115')
        assert snapshot.buy_reserved.value==Decimal('5')
        assert next(o for o in snapshot.active_orders if o.key[3]==manual).remaining.value==Decimal('12.5')
        rig.release('old-A','old-B','old-C','old-D')
        rig.cycle(pool='income',qualified=True)
        expected=[('tB',Decimal('50')),('tD',Decimal('30')),('tA',Decimal('20'))]
        expected=[row for row in expected if row[0]!='t'+manual_market]
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==expected
        assert manual in {row['id'] for row in rig.sdk.orders}
        assert all(manual not in batch for batch in rig.sdk.cancels)
        # Actual external account coverage, rather than a cancel ACK or pool
        # classification, keeps the manual capital and its same-market slot.
        rig.tick()
        rig.converge_current(lambda:rig.live.state.snapshot().observation.components['open_orders'].checked_at==rig.utc()
            and rig.live.state.snapshot().observation.read_ticket.trade_version_at_start==rig.live.state.snapshot().trade_version)
        snapshot=rig.live.state.snapshot()
        assert snapshot.buy_reserved.value==Decimal('5')+sum((capital for _,capital in expected),Decimal(0))
        assert len([o for o in snapshot.active_orders if o.key[1]==rig.cid(manual_market)])==1
        assert rig.execution.lp_observed_pool_state()['results'][rig.cid(manual_market)]['result']==measured['observations'][rig.cid(manual_market)]['pool_result']
        if manual_market=='D':
            assert all(p['token_id']!='tD' for p in rig.sdk.posts)
            rig.release(manual)
            # A later current round may rebuild the successful partial group.
            # Model actual venue removal for these new exact cancel IDs, rather
            # than leaving contradictory LIVE rows after their accepted receipt.
            rig.cancel_removes=True
            rig.finish_income_rebuild(rig.cid('D'),'tD',after=rig.utc())
            assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts if p['token_id']=='tD']==[('tD',Decimal('30'))]
            rig.cycle(pool='income',qualified=True)  # publish completed partial group's current account
            assert {(row['asset_id'],row['price']*(row['original_size']-row['size_matched']))
                for row in rig.sdk.orders}=={('tB',Decimal('50')),('tD',Decimal('30')),('tA',Decimal('20'))}
            assert len(rig.sdk.orders)==3
        assert all(manual not in batch for batch in rig.sdk.cancels)
        assert not rig.sdk.market_orders
    finally:rig.close()


@pytest.mark.parametrize('change',['unchanged','paused','identity','denied'])
def test_pool_group_permission_wait_keeps_current_state_responsive(tmp_path,monkeypatch,change,pool_async_watchdog):
    import sqlite3,time,threading
    from concurrent.futures import ThreadPoolExecutor
    from tests.test_lp_d2_integration import settle_control
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    sql_entered,sql_release=threading.Event(),threading.Event()
    pool=ThreadPoolExecutor(max_workers=3);pending=None;dispatched=None
    try:
        for name in 'ABCD':rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','10')]:rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income' if n in 'ABC' else 'trial',episode='old-'+n) for n in 'ABCD'})
        rig.measure(dict(A='.6',B='1.0',C='.4',D='.8'));rig.tick()
        end=time.monotonic()+4
        while True:
            planned=rig.live.run_once(rig.producer.capture_qualified_pool(),pool='income')
            if planned.rotation and len(planned.rotation.victims)==4:break
            assert time.monotonic()<end,'public income decision watchdog'
            time.sleep(.001)
        assert rig.sdk.cancels==rig.sdk.posts==[]
        real_connect=sqlite3.connect
        class Boundary(sqlite3.Connection):
            def execute(self,sql,*args,**kwargs):
                if (threading.current_thread().name=='lp-pool-group-cancel'
                        and 'FROM incidents WHERE acknowledged_at IS NULL' in sql):
                    sql_entered.set()
                    assert sql_release.wait(8),'real permission SQL external watchdog'
                return super().execute(sql,*args,**kwargs)
        monkeypatch.setattr(sqlite3,'connect',lambda *a,**k:real_connect(*a,**(k|dict(factory=Boundary))))
        pending=pool.submit(rig.live.dispatch_rotation,planned)
        assert sql_entered.wait(2),'group must reach actual Store-backed permission read'
        visible=pool.submit(rig.live.state.snapshot)
        snapshot=visible.result(timeout=1)
        assert snapshot.observation is not None and not sql_release.is_set()
        assert rig.sdk.cancels==[]
        if change=='paused':
            control=pool.submit(rig.execution.lp_auto_set_desired_running,False)
            assert control.result(timeout=2)['desired_running'] is False
            settle_control(rig.live)
        elif change=='identity':
            rig.release('old-A');rig.add_order('A','50',order_id='replacement-A');rig.tick()
            def publish():
                end=time.monotonic()+4
                while not any(o.key[3]=='replacement-A' for o in rig.live.state.snapshot().active_orders):
                    rig.live.run_once(rig.producer.capture_qualified_pool(),pool='income')
                    assert time.monotonic()<end,'public replacement publication watchdog'
                    time.sleep(.001)
            pool.submit(publish).result(timeout=5)
        elif change=='denied':
            historical=rig.store.create_recovery_execution(dict(reason='offline permission gate'),idempotency_key='group-permission')
            rig.store.open_incident(historical['execution_id'],dict(reason='offline unacknowledged incident'))
        assert not sql_release.is_set() and rig.sdk.cancels==[]
        sql_release.set();dispatched=pending.result(timeout=3);rig.finish_cancels(dispatched)
        outcomes=[out.handle.result(0) for out in dispatched.rotation_outcomes]
        assert len(outcomes)==4 and len({out.identity.request_id for out in outcomes})==4
        if change=='unchanged':
            assert len(rig.sdk.cancels)==1 and set(rig.sdk.cancels[0])=={'old-A','old-B','old-C','old-D'}
            assert all(out.status.value=='accepted' for out in outcomes)
            assert all(o.terminal_proof.matched.state=='unknown' for o in rig.live.state.snapshot().orders)
        else:
            assert rig.sdk.cancels==[]
            assert all(out.status.value=='not_sent' for out in outcomes)
        before=list(rig.sdk.cancels)
        rig.live.dispatch_rotation(planned)
        assert rig.sdk.cancels==before and rig.sdk.posts==rig.sdk.market_orders==[]
        assert rig.execution._lp_d2_lease_users==0
    finally:
        sql_release.set()  # never join an executor/native thread behind its SQL gate
        if pending is not None:
            dispatched=pending.result(timeout=3)
            rig.finish_cancels(dispatched)
        pool.shutdown(wait=True)
        rig.close()


@pytest.mark.parametrize('case',['timing','mode_and_aliases'])
def test_observed_pool_config_preserves_live_timing_and_explicit_mode_changes(tmp_path,monkeypatch,case,pool_async_watchdog):
    from tests.test_lp_d2_integration import settle_control
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        def save(changes):
            before=rig.execution.lp_auto_state()
            rig.execution.lp_auto_configure(dict(expected_config_version=before['config_version'],**changes))
            # The reply can precede the native save. Verify actual owner
            # publication and the real Store CAS result, never reply alone.
            rig.pump(lambda:rig.execution.lp_auto_state()['config_version']==before['config_version']+1
                and not rig.execution.lp_auto_state()['settings_pending'])
            after=rig.execution.lp_auto_state()
            saved=rig.store.lp_load_current_settings(rig.account)
            assert saved['config_version']==after['config_version']==before['config_version']+1
            for key,value in changes.items():assert saved[key]==after[key]==value
            return before,after
        initial=rig.execution.lp_auto_state()
        assert initial['allocation_mode']=='observed_pools'
        assert initial['desired_running'] is initial['effective_running'] is True
        assert initial['trial_budget_usd']==initial['budget_usd']=='20' and initial['income_budget_usd']=='100'
        if case=='timing':
            before,after=save(dict(api_retry_interval_seconds=30))
            assert after['trading_config_version']==before['trading_config_version']
            for key in ('allocation_mode','budget_usd','trial_budget_usd','income_budget_usd',
                    'target_buy_count','trial_target_buy_count','income_target_buy_count','income_weights',
                    'buy_price_level','order_check_interval_seconds','desired_running','effective_running'):
                assert after[key]==before[key]
            assert after['desired_running'] is after['effective_running'] is True
        else:
            rig.execution.lp_auto_set_desired_running(False);settle_control(rig.live)
            rig.live.run_once(rig.producer.capture_qualified_pool())
            _,minimum=save(dict(allocation_mode='minimum'))
            assert minimum['desired_running'] is minimum['effective_running'] is False
            _,observed=save(dict(allocation_mode='observed_pools'))
            assert observed['budget_usd']==observed['trial_budget_usd']=='20'
            assert observed['income_budget_usd']=='100'
            for patch,trial,income in [(dict(budget_usd='25'),'25','100'),
                    (dict(trial_budget_usd='30'),'30','100'),(dict(income_budget_usd='150'),'30','150')]:
                _,state=save(patch)
                assert state['allocation_mode']=='observed_pools'
                assert state['budget_usd']==state['trial_budget_usd']==trial
                assert state['income_budget_usd']==income
                assert state['target_buy_count']==state['trial_target_buy_count']==state['income_target_buy_count']==3
                assert state['income_weights']==['0.5','0.3','0.2']
                assert state['desired_running'] is state['effective_running'] is False
            # Real pool/alias writes retain the existing running trading guard.
            rig.execution.lp_auto_set_desired_running(True);settle_control(rig.live)
            rig.live.run_once(rig.producer.capture_qualified_pool())
            before=rig.store.lp_load_current_settings(rig.account)
            with pytest.raises(ValueError,match='pause_and_finish_automatic_buys_before_configuring'):
                rig.execution.lp_auto_configure(dict(budget_usd='35',expected_config_version=before['config_version']))
            assert rig.store.lp_load_current_settings(rig.account)==before
        assert rig.sdk.posts==rig.sdk.cancels==rig.sdk.market_orders==[]
    finally:rig.close()


def test_last_pool_group_exit_keeps_deferred_protection_runnable(tmp_path,monkeypatch,pool_async_watchdog):
    from threading import Event,Lock,current_thread
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    trial_entered,income_entered,native_release=Event(),Event(),Event()
    callbacks_entered,callback_release,http_release=Event(),Event(),Event()
    callback_lock=Lock();callback_count=0;groups=[];protection_entered=Event()
    try:
        pools=dict(A='100',B='100',C='100',D='500',E='400',F='300',G='200',H='100',J='1000')
        capitals=dict(A='50',B='30',C='20',D='5',E='5',F='5',G='5',H='5',J='1')
        for name in 'ABCDEFGHJ':
            rig.market(name,minimum='1',pool=pools[name]);rig.add_order(name,capitals[name])
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income' if n in 'ABC' else 'trial',episode='old-'+n) for n in 'ABCDEFGHJ'})
        rig.measure(dict(A='.6',B='1',C='.4',D='.8',E='.7',F='.5',G='.3',H='.2'))
        def cancel(*,order_ids):
            ids=set(order_ids);rig.sdk.cancels.append(tuple(order_ids))
            if ids=={'old-F','old-G','old-H'}:trial_entered.set()
            elif ids=={'old-A','old-B','old-C','old-D','old-E'}:income_entered.set()
            elif ids=={'old-J'}:
                protection_entered.set();rig.release('old-J')
                return dict(canceled=('old-J',),not_canceled={})
            else:raise AssertionError(('unexpected cancel identity',order_ids))
            assert native_release.wait(8),'two real group calls external watchdog'
            return dict(canceled=(),not_canceled={oid:'offline unknown' for oid in order_ids})
        rig.sdk.cancel_orders=cancel
        trial=rig.cycle(pool='trial');assert trial_entered.wait(2)
        income=rig.cycle(pool='income');assert income_entered.wait(2)
        assert {o.key[3] for o in trial.rotation.victims}=={'old-F','old-G','old-H'}
        assert {o.key[3] for o in income.rotation.victims}=={'old-A','old-B','old-C','old-D','old-E'}
        groups=[trial.rotation_outcomes[0].handle.parent,income.rotation_outcomes[0].handle.parent]
        assert rig.live.status()['native_inflight']==2 and rig.execution._lp_d2_lease_users==2
        assert rig.sdk.posts==[]
        # Partial matching isolates the existing eleventh read-failure guard
        # from the unrelated queue-baseline cancellation path.
        rig.history['old-J']['size_matched']=Decimal('.5');rig.failed_book_tokens={'tJ'}
        def failure():
            return next((f for k,f in rig.live.state.snapshot().read_failures.items() if k[3]=='old-J'),None)
        for expected in range(1,12):
            rig.tick();rig.pump(lambda:failure() is not None and failure().consecutive_failures==expected)
        assert failure().protection_key is not None and not protection_entered.is_set()
        rig.pump(lambda:rig.live.read_runtime.status()['ready_count']==0 and
            not any(s['active'] for s in rig.live.read_runtime.status()['scopes']))
        # Consume the two real public pool opportunities at the same clock.
        # No internal pending/generation/scheduler state is replaced or edited.
        rig.live.run_once(rig.producer.capture_qualified_pool(),pool='trial')
        rig.live.run_once(rig.producer.capture_qualified_pool(),pool='income')
        rig.pump(lambda:rig.live.read_runtime.status()['ready_count']==0 and
            not any(s['active'] for s in rig.live.read_runtime.status()['scopes']))
        partial=next(o for o in rig.live.state.snapshot().active_orders if o.key[3]=='old-J')
        assert (partial.original_size.value,partial.size_matched.value,partial.remaining.value)==(Decimal('2.5'),Decimal('.5'),Decimal('2'))
        def wake():
            nonlocal callback_count
            rig.scheduler.request_check()
            if current_thread().name=='lp-pool-group-cancel':
                with callback_lock:
                    callback_count+=1
                    if callback_count==2:callbacks_entered.set()
                assert callback_release.wait(8),'last-owner completion callback watchdog'
        rig.execution.set_lp_auto_wakeup(wake)
        rig.http_gate=http_release;before_http=list(rig.requests);stamp=rig.seconds
        native_release.set();assert callbacks_entered.wait(3)
        assert all(h._thread.is_alive() and not h.done() for h in groups)
        before_blocks=rig.live.diagnostics()['cancel_blocks']['by_reason'].get('native_capacity',0)
        # Deterministic public owner boundary: capacity is checked while both
        # physical threads are held, then readiness is read after both exit.
        rig.live.run_protection_once()
        assert rig.live.diagnostics()['cancel_blocks']['by_reason'].get('native_capacity',0)>before_blocks
        assert not protection_entered.is_set() and rig.live.status()['native_inflight']==2
        callback_release.set()
        for group in groups:
            group._thread.join(3);assert not group._thread.is_alive()
        assert rig.execution._lp_d2_lease_users==0
        assert rig.requests==before_http  # no unrelated HTTP publication/wake
        status=rig.live.status()
        assert status['native_inflight']==0
        assert status['plan_wait'] is None,'last physical exit must keep exact deferred protection immediately runnable'
        assert rig.scheduler.run_due(),'real service scheduler must continue at unchanged business time'
        rig.pump(protection_entered.is_set)
        rig.pump(lambda:rig.live.status()['native_inflight']==0)
        assert rig.seconds==stamp
        assert sum(batch==('old-J',) for batch in rig.sdk.cancels)==1
        outcomes=[o.handle.result(0) for result in (trial,income) for o in result.rotation_outcomes]
        assert len(outcomes)==8 and len({o.identity.request_id for o in outcomes})==8
        assert all(o.status.value=='unknown_error' for o in outcomes)
        snapshot=rig.live.state.snapshot()
        assert all(o.terminal_proof is None and o.presence=='present' for o in snapshot.orders if o.key[3]!='old-J')
        partial=next(o for o in snapshot.orders if o.key[3]=='old-J')
        assert partial.remaining.state=='unknown' and partial.terminal_proof.matched.state=='unknown'
        for _ in range(3):rig.scheduler.run_due()
        assert sum(batch==('old-J',) for batch in rig.sdk.cancels)==1
        assert rig.sdk.posts==rig.sdk.market_orders==[]
        http_release.set();rig.http_gate=None
        rig.tick()
        rig.converge_current(lambda:next(o for o in rig.live.state.snapshot().orders if o.key[3]=='old-J').remaining.state=='known')
        final=next(o for o in rig.live.state.snapshot().orders if o.key[3]=='old-J')
        assert final.terminal_proof.matched.value==Decimal('.5') and final.remaining.value==2
        assert sum(batch==('old-J',) for batch in rig.sdk.cancels)==1
    finally:
        http_release.set();callback_release.set();native_release.set()
        for group in groups:group._thread.join(3)
        rig.close()


@pytest.mark.parametrize('case',['running_with_income','paused_with_income','paused_unknown_facts','paused_clear'])
def test_observed_pool_mode_exit_preserves_trading_guard_and_versions(tmp_path,monkeypatch,case,pool_async_watchdog):
    from tests.test_lp_d2_integration import settle_control
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        with_income=case in ('running_with_income','paused_with_income')
        if with_income:
            for name,capital in [('A','50'),('B','30'),('C','20')]:
                rig.market(name);rig.add_order(name,capital)
            rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
                roles={rig.cid(n):dict(pool='income',episode='old-'+n) for n in 'ABC'})
        if case!='running_with_income':
            rig.execution.lp_auto_set_desired_running(False);settle_control(rig.live)
        rig.cycle(pool='income',qualified=True)
        if case=='paused_unknown_facts':
            failures=[]
            def unavailable(**kwargs):
                failures.append(rig.seconds)
                raise OSError('offline current balance unavailable')
            rig.sdk.get_balance_allowance=unavailable
            rig.tick()
            rig.pump(lambda:failures and rig.live.state.snapshot().observation.accepted_at==rig.utc()
                and not rig.live.state.snapshot().observation.source_frame.sources['balance'].complete
                and rig.execution.lp_auto_state()['new_capacity_usd'] is None)
        before=rig.execution.lp_auto_state();saved=rig.store.lp_load_current_settings(rig.account)
        snapshot=rig.live.state.snapshot()
        assert before['allocation_mode']=='observed_pools'
        assert before['budget_usd']==before['trial_budget_usd']=='20' and before['income_budget_usd']=='100'
        assert before['desired_running'] is before['effective_running'] is (case=='running_with_income')
        assert rig.live.status()['native_inflight']==0 and snapshot.openings=={}
        assert {o.key[3] for o in snapshot.active_orders}==({'old-A','old-B','old-C'} if with_income else set())
        assert Decimal(before['buy_reserved_usd'])==snapshot.buy_reserved.value
        assert snapshot.buy_reserved.value==Decimal('100' if with_income else '0')
        assert snapshot.new_capacity.state==('unknown' if case=='paused_unknown_facts' else 'known')
        orders=tuple(dict(o) for o in rig.sdk.orders)
        protected_keys=('config_version','trading_config_version','allocation_mode','budget_usd','trial_budget_usd',
            'income_budget_usd','target_buy_count','trial_target_buy_count','income_target_buy_count','income_weights',
            'desired_running','effective_running','requested_control_version','saved_control_version',
            'buy_reserved_usd','inventory_cost_usd','current_exposure_usd','new_capacity_usd')
        if case!='paused_clear':
            reason='financial_facts_unknown' if case=='paused_unknown_facts' else 'pause_and_finish_automatic_buys_before_configuring'
            with pytest.raises(ValueError,match=reason):
                rig.execution.lp_auto_configure(dict(allocation_mode='minimum',expected_config_version=before['config_version']))
            after=rig.execution.lp_auto_state()
            assert not after['settings_pending'] and not after['settings_native_inflight']
            assert rig.store.lp_load_current_settings(rig.account)==saved
            for key in protected_keys:assert after[key]==before[key]
            assert tuple(dict(o) for o in rig.sdk.orders)==orders
            assert rig.sdk.posts==rig.sdk.cancels==rig.sdk.market_orders==[]
            if case=='running_with_income':
                # An actual subsequent ordinary cycle must keep the income100
                # obligations, rather than cancel them under legacy budget20.
                rig.tick();rig.pump(lambda:rig.live.state.snapshot().observation.accepted_at==rig.utc())
                state=rig.execution.lp_auto_state()
                assert state['allocation_mode']=='observed_pools' and Decimal(state['buy_reserved_usd'])==100
                assert tuple(dict(o) for o in rig.sdk.orders)==orders
                assert rig.sdk.posts==rig.sdk.cancels==rig.sdk.market_orders==[]
        else:
            assert snapshot.inventory_cost.value==snapshot.current_exposure.value==0
            def publish(mode,trading_delta):
                old=rig.execution.lp_auto_state()
                rig.execution.lp_auto_configure(dict(allocation_mode=mode,expected_config_version=old['config_version']))
                rig.pump(lambda:rig.execution.lp_auto_state()['config_version']==old['config_version']+1
                    and not rig.execution.lp_auto_state()['settings_pending'])
                current=rig.execution.lp_auto_state();document=rig.store.lp_load_current_settings(rig.account)
                assert current['allocation_mode']==document['allocation_mode']==mode
                assert current['config_version']==document['config_version']==old['config_version']+1
                assert current['trading_config_version']==document['trading_config_version']==old['trading_config_version']+trading_delta
                for key in protected_keys:
                    if key not in ('config_version','trading_config_version','allocation_mode','new_capacity_usd'):
                        assert current[key]==old[key]
                for _ in range(3):
                    observed=rig.execution.lp_auto_state()
                    assert (observed['config_version'],observed['trading_config_version'])==(current['config_version'],current['trading_config_version'])
                assert current['desired_running'] is current['effective_running'] is False
                assert rig.sdk.posts==rig.sdk.cancels==rig.sdk.market_orders==[]
            publish('minimum',1)
            publish('minimum',0)
            publish('observed_pools',1)
            # Direct Store callers receive the same effective-mode version
            # classification, including legacy absence and same-mode writes.
            store=PredictionArbitrageStore(tmp_path/'direct-store');account='b'*64
            document=store.lp_save_current_settings(account,expected_config_version=0,changes=dict(budget_usd='20',target_buy_count=3))
            assert document['config_version']==document['trading_config_version']==1
            for patch,trading_version in [(dict(allocation_mode='minimum'),1),
                    (dict(allocation_mode='minimum',allocation_shadow_enabled=True,allocation_weights=['0.5','0.3','0.2']),1),
                    (dict(allocation_mode='observed_pools',income_budget_usd='100'),2),
                    (dict(allocation_mode='minimum'),3),(dict(allocation_mode='minimum'),3)]:
                version=document['config_version']
                document=store.lp_save_current_settings(account,expected_config_version=version,changes=patch)
                assert document['config_version']==version+1 and document['trading_config_version']==trading_version
                assert document['allocation_mode']==patch['allocation_mode']
                assert document['budget_usd']=='20' and document['target_buy_count']==3
    finally:rig.close()


@pytest.mark.parametrize('case,source',[
    ('group','open_orders'),('early','open_orders'),('last','open_orders'),
    ('manual-A','open_orders'),('manual-C','open_orders'),('manual-D','open_orders'),
    ('group','order'),('early','order'),('last','order'),
])
def test_pool_postreceipt_observation_waits_for_external_publication(
        tmp_path,monkeypatch,case,source,pool_async_watchdog,request):
    """Delay actual HTTP bytes; keep the original six assertion sequences."""
    import asyncio,sys
    from threading import Event
    target='old-J' if case=='last' else 'old-E'
    rigs=[]
    class ScheduledRig(AsyncPoolRig):
        def __init__(self,*args,**kwargs):
            self.proof_entered,self.proof_release=Event(),Event()
            self.proof_checks=[]
            super().__init__(*args,**kwargs)
            rigs.append(self)

        async def http(self,req):
            response=await super().http(req)
            canceled=(target in self.history and self.history[target]['status']=='CANCELED')
            after_receipt=bool(self.sdk.posts) if case.startswith('manual-') else canceled
            if (after_receipt and req.headers['x-lp-source']==source
                    and (source!='order' or req.headers['x-lp-order']==target)
                    and not self.proof_release.is_set()):
                self.proof_entered.set()
                assert await asyncio.get_running_loop().run_in_executor(
                    None,self.proof_release.wait,4),'external proof publication watchdog'
            return response

        def converge_current(self,predicate,*,timeout=4):
            stamp=self.seconds
            self.pump(self.proof_entered.is_set)
            snapshot=self.live.state.snapshot()
            if source=='order':
                partial=next(o for o in snapshot.orders if o.key[3]==target)
                assert partial.remaining.state=='unknown'
                assert partial.terminal_proof.matched.state=='unknown'
            else:
                observation=snapshot.observation
                assert (observation.components['open_orders'].checked_at!=self.utc()
                    or observation.read_ticket.trade_version_at_start!=snapshot.trade_version)
                assert snapshot.new_capacity.state=='unknown'
            assert self.seconds==stamp  # held proof cannot authorize finality/funds
            self.proof_release.set()
            def published_or_next_read():
                status=self.live.read_runtime.status()
                due=status['next_dependency_monotonic']
                return (predicate() or (status['ready_count']==0
                    and not any(scope['active'] for scope in status['scopes'])
                    and status['observed_monotonic']>=self.seconds
                    and due is not None and due>self.seconds))
            self.pump(published_or_next_read)
            status=self.live.read_runtime.status()
            # If the result is stale, a fixed-clock pump has no runnable read.
            self.proof_checks.append((source,stamp,predicate(),status['next_dependency_monotonic']))
            super().converge_current(predicate,timeout=timeout)

        def close(self):
            self.proof_release.set()
            super().close()

    monkeypatch.setattr(sys.modules[__name__],'AsyncPoolRig',ScheduledRig)
    if case.startswith('manual-'):
        test_income_rebuild_preserves_unclassified_same_market_orders(
            tmp_path,monkeypatch,case[-1],pool_async_watchdog)
    else:
        original={
            'group':test_pool_group_cancel_wakes_deferred_async_protection,
            'early':test_pool_group_completion_preserves_early_wake_until_physical_exit,
            'last':test_last_pool_group_exit_keeps_deferred_protection_runnable,
        }[case]
        original(tmp_path,monkeypatch,pool_async_watchdog)
    assert len(rigs)==1 and len(rigs[0].proof_checks)==1
    request.node.user_properties.append(('external_proof_boundary',repr(rigs[0].proof_checks)))


def test_manual_income_rebuild_waits_for_candidate_publication(tmp_path,monkeypatch,pool_async_watchdog,request):
    """Current account alone cannot replace an unpublished D candidate book."""
    import asyncio,sys,time
    from threading import Event,Thread
    rigs=[]
    class CandidateRig(AsyncPoolRig):
        def __init__(self,*args,**kwargs):
            self.candidate_entered,self.account_ready,self.candidate_release=Event(),Event(),Event()
            self.removed_at=None;self.observer=None;self.observer_errors=[]
            self.account_capture=None;self.boundaries=[]
            super().__init__(*args,**kwargs);rigs.append(self)

        def release(self,*ids):
            super().release(*ids)
            if 'manual-extra-D' not in ids:return
            self.removed_at=self.utc()
            def release_candidate_after_account():
                end=time.monotonic()+4
                try:
                    assert self.candidate_entered.wait(max(0,end-time.monotonic())),'D-candidate admission watchdog'
                    assert self.account_ready.wait(max(0,end-time.monotonic())),'current-account response watchdog'
                    assert not self.candidate_release.is_set()
                    assert all(p['token_id']!='tD' for p in self.sdk.posts)
                    published=self.live.state.snapshot().observation.source_frame.sources.get(
                        'market_income:'+self.cid('D')+':tD')
                    assert published is None or published.checked_at<self.removed_at
                    self.boundaries.append((self.account_capture,self.seconds,
                        str(published.checked_at if published else None)))
                except BaseException as exc:self.observer_errors.append(exc)
                finally:self.candidate_release.set()
            self.observer=Thread(target=release_candidate_after_account,name='test-D-candidate-publication')
            self.observer.start()

        async def http(self,req):
            response=await super().http(req)
            if self.removed_at is None:return response
            source=req.headers['x-lp-source']
            if (source=='open_orders' and not self.account_ready.is_set() and self.utc()>self.removed_at
                    and all(row['id']!='manual-extra-D' for row in response.json()['data'])):
                self.account_capture=(self.seconds,tuple(row['id'] for row in response.json()['data']))
                self.account_ready.set()
            if (source=='book' and req.headers['x-lp-order'].startswith('income:')
                    and req.url.params['token_id']=='tD' and not self.candidate_release.is_set()):
                self.candidate_entered.set()
                assert await asyncio.get_running_loop().run_in_executor(
                    None,self.candidate_release.wait,4),'external D-candidate publication watchdog'
            return response

        def close(self):
            self.candidate_entered.set();self.account_ready.set()
            if self.observer is not None:
                self.observer.join(3);assert not self.observer.is_alive()
            self.candidate_release.set()
            super().close()

    monkeypatch.setattr(sys.modules[__name__],'AsyncPoolRig',CandidateRig)
    test_income_rebuild_preserves_unclassified_same_market_orders(tmp_path,monkeypatch,'D',pool_async_watchdog)
    assert rigs[0].observer_errors==[]
    assert len(rigs)==1 and len(rigs[0].boundaries)==1
    request.node.user_properties.append(('account_before_D_candidate',repr(rigs[0].boundaries)))
