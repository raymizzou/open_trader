"""Expected current BUY rewards have a durable ten-minute unknown timer."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from types import SimpleNamespace
from polymarket.models.clob.cancel import CancelOrdersResponse

from tests.test_lp_account_reservation_reconciliation import runtime, _refill_identity
from tests.test_lp_auto_independent_cancel_contract import _protected_f, _wait_audits
from tests.test_lp_auto_refill_contract import RefillPublic
from tests.test_lp_order_registration_contract import _open_order


def _managed_buy(runtime, origin):
    if origin == 'auto':
        store, account, lp, engine, public, depth, order, first = _protected_f(runtime)
    else:
        public = RefillPublic(runtime.clock)
        order = _open_order('manual-f', 'BUY', price='.40', original='20',
            token_id=_refill_identity(6)[2], outcome='YES').model_copy(update={
                'market': _refill_identity(6)[1], 'condition_id': _refill_identity(6)[1]})
        store, adapter, account, lp, engine = runtime(public_client=public, orders=(order,))
        engine.lp_auto_configure({'budget_usd': '8', 'target_buy_count': 1})
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        account.get_order = lambda *, order_id: order
    # This boundary test controls business time; these external responses do
    # not advance it during a monitoring call.
    account.get_balance_allowance = lambda **kwargs: SimpleNamespace(balance='100000000',
        allowances={account.environment.standard_exchange: '100000000'})
    def ack(*, order_ids):
        account.cancels.append(order_ids)
        return CancelOrdersResponse(canceled=order_ids, not_canceled={})
    account.cancel_orders = ack
    return store, account, lp, engine, public, order


def _unknown_rewards(public):
    original = public.list_market_rewards
    def unavailable(**kwargs):
        raise TimeoutError('Expected current LP reward source unavailable; history unchanged')
    public.list_market_rewards = unavailable
    return original


def _timer(store, oid):
    return next(s for s in store.lp_sessions() if oid in s['owned_order_ids']).get('buy_yield_unknown', {}).get(oid)


@pytest.mark.parametrize('origin', ['auto', 'manual'])
def test_unknown_buy_yield_cancels_after_ten_minutes_without_replacement(runtime, origin):
    store, account, lp, engine, public, order = _managed_buy(runtime, origin)
    _unknown_rewards(public)
    started = runtime.clock[0]
    engine.lp_tick()
    timer = _timer(store, order.id)
    assert timer is not None, 'Normal LP monitoring persists first expected-yield unknown'
    since = datetime.fromisoformat(timer['unknown_since'].replace('Z', '+00:00'))
    assert since >= started and since <= runtime.clock[0]
    runtime.clock[0] = since + timedelta(seconds=599)
    engine.lp_tick()
    assert _timer(store, order.id)['unknown_since'] == timer['unknown_since']
    assert account.cancels == [] and account.market_orders == []
    runtime.clock[0] = since + timedelta(seconds=600)
    engine.lp_tick()
    assert account.cancels == [(order.id,)]
    assert len(account.posts) == (1 if origin == 'auto' else 0)
    assert account.orders == (order,), 'ACK does not prove terminal or release the slot'
    audits = _wait_audits(store, lambda rows: any(a.get('reason') == 'buy_yield_unknown_timeout' for a in rows))
    risk, = [a for a in audits if a.get('reason') == 'buy_yield_unknown_timeout']
    assert risk['order_id'] == order.id and risk['state'] == 'accepted'
    engine.lp_tick()
    assert account.cancels == [(order.id,)], 'Same timed-out risk request is not replayed'


@pytest.mark.parametrize('scenario', ['known_recovery', 'restart', 'new_order_id'])
def test_unknown_yield_clock_resets_and_survives_restart(runtime, scenario):
    store, account, lp, engine, public, order = _managed_buy(runtime, 'manual')
    rewards = _unknown_rewards(public)
    engine.lp_tick()
    timer = dict(_timer(store, order.id))
    since = datetime.fromisoformat(timer['unknown_since'].replace('Z', '+00:00'))
    runtime.clock[0] = since + timedelta(seconds=500)
    if scenario == 'known_recovery':
        public.list_market_rewards = rewards
        public.rates[6] = '0'
        engine.lp_tick()
        assert _timer(store, order.id) is None, 'Valid current zero reward clears unknown timer'
        _unknown_rewards(public)
        runtime.clock[0] += timedelta(seconds=1)
        engine.lp_tick()
        fresh = _timer(store, order.id)
        assert datetime.fromisoformat(fresh['unknown_since'].replace('Z', '+00:00')) > since
        runtime.clock[0] = since + timedelta(seconds=600)
        engine.lp_tick()
        assert account.cancels == []
        runtime.clock[0] = datetime.fromisoformat(fresh['unknown_since'].replace('Z', '+00:00')) + timedelta(seconds=600)
        engine.lp_tick()
        assert account.cancels == [(order.id,)]
    elif scenario == 'restart':
        lp.exchange.close()
        store, adapter, account, lp, engine = runtime(public_client=public, orders=(order,))
        account.get_order = lambda *, order_id: order
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        engine.lp_tick()
        assert _timer(store, order.id)['unknown_since'] == timer['unknown_since']
        runtime.clock[0] = since + timedelta(seconds=600)
        engine.lp_tick()
        assert account.cancels == [(order.id,)]
    else:
        new = order.model_copy(update={'id': 'manual-f-new'})
        account.orders = (new,)
        account.get_order = lambda *, order_id: new if order_id == new.id else order.model_copy(update={'status': 'CANCELED'})
        packet = lp.exchange.lp_account_snapshot_shared(max_age_seconds=-1,
            trade_generation_provider=store.lp_trade_generation)
        registered = lp.register_account_snapshot(packet)
        assert registered['state'] == 'registered', registered
        engine.lp_tick()
        new_timer = _timer(store, new.id)
        assert new_timer is not None and new_timer['unknown_since'] != timer['unknown_since']
        runtime.clock[0] = since + timedelta(seconds=600)
        engine.lp_tick()
        assert account.cancels == []
        runtime.clock[0] = datetime.fromisoformat(new_timer['unknown_since'].replace('Z', '+00:00')) + timedelta(seconds=600)
        engine.lp_tick()
        assert account.cancels == [(new.id,)]


def test_paused_auto_keeps_unknown_buy_yield_protection(runtime):
    store, account, lp, engine, public, order = _managed_buy(runtime, 'auto')
    _unknown_rewards(public)
    engine.lp_tick()
    since = datetime.fromisoformat(_timer(store, order.id)['unknown_since'].replace('Z', '+00:00'))
    posts = list(account.posts)
    engine.lp_auto_set_desired_running(False)
    runtime.clock[0] = since + timedelta(seconds=600)
    engine.lp_tick()
    assert account.cancels == [(order.id,)]
    assert account.posts == posts and account.market_orders == []
    assert engine.lp_auto_state()['desired_running'] is False
    assert account.orders == (order,)

from polymarket.models.clob import SignedOrder
from polymarket.models.data.portfolio import Position
from tests.test_lp_order_registration_contract import WALLET, _maker_order, _trade
from time import monotonic
from threading import Event


def test_sell_and_unknown_historical_pnl_do_not_use_buy_yield_timer(runtime):
    store, account, lp, engine, public, original = _managed_buy(runtime, 'auto')
    maker = _maker_order(original.id, 'BUY', '20', '.40').model_copy(
        update={'token_id': original.token_id})
    fill = _trade('real-f-fill', maker, size='20').model_copy(update={
        'market': _refill_identity(6)[1], 'condition_id': _refill_identity(6)[1], 'token_id': original.token_id,
        'matched_at': runtime.clock[0], 'updated_at': runtime.clock[0]})
    account.trades = (fill,)
    account.positions = (Position(conditionId=_refill_identity(6)[1], proxyWallet=WALLET,
        asset=original.token_id, size=Decimal('20'), avgPrice=Decimal('.40'), outcome='YES'),)
    known_buy = _open_order('known-current-buy', 'BUY', price='.40', original='20',
        token_id=_refill_identity(1)[2], outcome='YES')
    account.orders = (known_buy,)
    terminal = original.model_copy(update={'status': 'FILLED', 'size_matched': Decimal('20'),
        'associate_trades': ('real-f-fill',)})
    account.get_order = lambda *, order_id: terminal if order_id == original.id else next(o for o in account.orders if o.id == order_id)
    def sign(**kwargs):
        assert kwargs['side'] == 'SELL'
        return SignedOrder(builder='0x1', expiration=kwargs['expiration'], maker=WALLET,
            maker_amount=int(kwargs['size'] * 1000000), metadata='0x3',
            order_type='GTC' if kwargs['expiration'] is None else 'GTD', salt=1, side='SELL', signature='0x4',
            signature_type=0, signer=WALLET, taker_amount=int(kwargs['price'] * kwargs['size'] * 1000000),
            timestamp=1, token_id=str(kwargs['token_id']), post_only=True)
    def post(signed):
        account.posts.append(signed)
        order = _open_order('normal-passive-sell', 'SELL', price=str(Decimal(signed.taker_amount) / signed.maker_amount),
            original=str(Decimal(signed.maker_amount) / 1000000), token_id=signed.token_id).model_copy(
                update={'market': _refill_identity(6)[1], 'condition_id': _refill_identity(6)[1]})
        account.orders += (order,)
        return {'order_id': order.id, 'status': 'LIVE', 'accepted': True, 'size_matched': '0'}
    account.create_limit_order, account.post_order = sign, post
    rewards = public.list_market_rewards
    def reward(**kwargs):
        if kwargs['condition_id'] == _refill_identity(6)[1]:
            raise TimeoutError('Expected SELL market reward unknown')
        return rewards(**kwargs)
    public.list_market_rewards = reward
    runtime.clock[0] += timedelta(seconds=1)
    registered = lp.register_account_snapshot(lp.exchange.lp_account_snapshot_shared(max_age_seconds=-1,
        trade_generation_provider=store.lp_trade_generation))
    assert registered['state'] == 'registered', registered
    deadline = monotonic() + 5
    while not any(getattr(p, 'side', '') == 'SELL' for p in account.posts):
        state = engine.lp_tick()
        assert monotonic() < deadline, state
    sell, = [p for p in account.posts if p.side == 'SELL']
    assert sell.token_id == original.token_id and sell.maker_amount == 20000000
    assert account.market_orders == [] and account.cancels == []
    known_session = next(s for s in store.lp_sessions() if known_buy.id in s['owned_order_ids'])
    period_start=runtime.clock[0]-timedelta(hours=1)
    historical=engine.lp_auto_report_facts(period_start=period_start,period_end=runtime.clock[0])['financial_period']
    assert historical['status']=='unknown' and historical['reason']=='account_historical_boundary_unavailable'
    assert historical['realized_pnl_usd'] is None and historical['inventory_cost_usd'] is None
    assert historical['inventory_quantity'] is None
    assert known_session['order_history'][known_buy.id]['status']=='LIVE'
    assert Decimal(known_session['order_history'][known_buy.id]['size_matched'])==0
    assert _timer(store, known_buy.id) is None
    runtime.clock[0] += timedelta(seconds=601)
    engine.lp_tick()
    assert account.cancels == [] and len([p for p in account.posts if p.side == 'SELL']) == 1
    assert _timer(store, 'normal-passive-sell') is None and _timer(store, known_buy.id) is None
    assert account.positions[0].size == 20
    after=engine.lp_auto_report_facts(period_start=period_start,period_end=runtime.clock[0])['financial_period']
    assert after['status']=='unknown' and after['reason']=='account_historical_boundary_unavailable'
    assert after['realized_pnl_usd'] is None and after['inventory_cost_usd'] is None and after['inventory_quantity'] is None
    lp.drain_submissions()
