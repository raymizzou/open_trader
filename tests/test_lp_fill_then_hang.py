"""#469 public exit behavior with offline exchange boundaries."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_polymarket_lp import _Exchange, _snapshot, _request, _inventory_snapshot
from tests.test_lp_d2_integration import _d2_filled_opening


def legacy_holding(tmp_path, *, cost='30.5', ask='.29'):
    now = datetime(2026, 10, 26, 12, tzinfo=UTC)
    exchange = _Exchange()
    exchange.snapshot_value = _snapshot(now)
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: now)
    request = dict(_request(now), quantity=Decimal('100'))
    service.store.lp_save_price_history(request['condition_id'], request['token_id'], [], dict(
        state='known', amplitude=Decimal('.005'), checked_at=now, valid_until=now+timedelta(hours=24)))
    preview = service.preview(request)
    service.start(preview['preview_id'], 'fill-hang')
    exchange.snapshot_value = _inventory_snapshot(now, buy_quantity=Decimal('100'), buy_cost=Decimal(cost),
        residual=Decimal('100'), residual_value=Decimal('28'))
    exchange.snapshot_value['book']['asks'] = [dict(price=Decimal(ask), size=Decimal('1000'))]
    exchange.snapshot_value['trades'][0]['maker_orders'][0]['fee'] = Decimal('.1')
    return service, exchange


def d2_tick(live):
    live.run_once()
    for handle in live.run_protection_once():
        if handle is not None:
            handle.result(3)
            handle._thread.join(3)
            assert handle.done()


@pytest.mark.parametrize('path', ['manual', 'auto'])
@pytest.mark.parametrize(('ask', 'expected'), [('.29', '.31'), ('.34', '.34')])
def test_passive_exit_never_below_cost(tmp_path, monkeypatch, path, ask, expected):
    if path == 'manual':
        service, exchange = legacy_holding(tmp_path, ask=ask)
        service.tick()
        sells = [o for o in exchange.limit_orders if o['side'] == 'SELL']
        assert len(sells) == 1
        assert sells[0]['price'] == Decimal(expected)
        assert sells[0]['quantity'] == 100 and sells[0]['post_only'] is True
        assert exchange.protected_sells == []
    else:
        live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
        sdk.positions = tuple(dict(p, average_price='.305') for p in sdk.positions)
        public.ask, public.bid = Decimal(ask), Decimal('.28')
        try:
            d2_tick(live)  # exact own-fill evidence
            d2_tick(live)  # a fresh current account frame can authorize SELL
            sells = [o for o in sdk.posts if o['side'] == 'SELL']
            assert len(sells) == 1
            assert sells[0]['price'] == Decimal(expected)
            assert sells[0]['size'] == 100 and sells[0]['post_only'] is True
            assert sells[0]['order_type'] == 'GTC' and sdk.market_orders == []
        finally:
            assert live.close(3)
            live.trading.close()


@pytest.mark.parametrize('path', ['manual', 'auto'])
@pytest.mark.parametrize('cost', [None, 'NaN', '.995'])
def test_passive_exit_blocks_unknown_or_unquotable_cost(tmp_path, monkeypatch, path, cost):
    if path == 'manual':
        service, exchange = legacy_holding(tmp_path, cost='99.5' if cost == '.995' else '30')
        snapshot = exchange.snapshot_value
        if cost != '.995':
            snapshot['trades'][0]['maker_orders'][0]['price'] = cost
            snapshot['orders'][0]['price'] = cost
        else:
            snapshot['book']['bids'] = [dict(price=Decimal('.99'), size=Decimal('100'))]
        service.tick()
        assert not [o for o in exchange.posts if o['side'] == 'SELL']
        assert exchange.protected_sells == []
        session = service.store.lp_sessions()[0]
        assert session.get('reconciliation') in ('trade_price_unknown', 'owned_fill_price_unknown', 'passive_cost_unknown', 'passive_cost_above_maximum')
    else:
        live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
        sdk.positions = tuple(dict(p, average_price=cost) for p in sdk.positions)
        try:
            d2_tick(live)
            d2_tick(live)
            assert not [o for o in sdk.posts if o['side'] == 'SELL']
            assert sdk.market_orders == []
            sell, = live.state.snapshot().sells.values()
            assert set(sell.inventory_cost_usd.reason_codes) & {'passive_cost_unknown', 'passive_cost_above_maximum'}
        finally:
            assert live.close(3)
            live.trading.close()


@pytest.mark.parametrize('full_history', [False, True])
def test_passive_exit_blocks_incomplete_current_cost_history(tmp_path, full_history):
    service, exchange = legacy_holding(tmp_path, cost='30', ask='.29')
    snapshot = exchange.snapshot_value
    filled = Decimal('100') if full_history else Decimal('50')
    snapshot['orders'][0].update(status='CANCELED', original_size=Decimal('100'),
        size_matched=filled, remaining_size=Decimal('100')-filled)
    snapshot['trades'][0]['maker_orders'][0]['matched_amount'] = filled
    assert all('average_price' not in p and 'cost_usd' not in p and 'inventory_cost_usd' not in p
        for p in snapshot['account']['positions'])
    service.tick()
    session, = service.store.lp_sessions()
    assert Decimal(session['buy_filled_quantity']) == filled
    assert Decimal(session['buy_cost']) == filled*Decimal('.30')
    assert Decimal(session['residual_quantity']) == 100
    assert session['position_reconciled'] is True
    sells = [o for o in exchange.posts if o['side']=='SELL']
    if full_history:
        assert len(sells)==1 and sells[0]['quantity']==100 and sells[0]['price']==Decimal('.30')
        assert sells[0]['post_only'] is True
    else:
        assert sells == []
        assert session['reconciliation']=='passive_cost_unknown'
    assert exchange.protected_sells == []


@pytest.mark.parametrize('path', ['manual', 'auto'])
def test_existing_passive_exit_does_not_chase_book(tmp_path, monkeypatch, path):
    if path == 'manual':
        service, exchange = legacy_holding(tmp_path, ask='.34')
        service.tick()
        exchange.snapshot_value = _inventory_snapshot(service.clock(), buy_quantity=Decimal('100'),
            buy_cost=Decimal('30.5'), residual=Decimal('100'), residual_value=Decimal('28'),
            passive_status='LIVE', orders_terminal=False)
        exchange.snapshot_value['orders'][1]['price'] = Decimal('.34')
        for ask in ('.32', '.36'):
            exchange.snapshot_value['book']['asks'] = [dict(price=Decimal(ask), size=Decimal('1000'))]
            service.tick()
        assert exchange.cancels == []
        assert len([o for o in exchange.posts if o['side'] == 'SELL']) == 1
    else:
        live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
        public.ask = Decimal('.34')
        try:
            d2_tick(live)
            d2_tick(live)
            for ask in ('.32', '.36'):
                public.ask = Decimal(ask)
                d2_tick(live)
            assert sdk.cancels == []
            assert len([o for o in sdk.posts if o['side'] == 'SELL']) == 1
            assert sdk.posts[-1]['price'] == Decimal('.34')
        finally:
            assert live.close(3)
            live.trading.close()


@pytest.mark.parametrize('path', ['manual', 'auto'])
@pytest.mark.parametrize('latched', [False, True])
@pytest.mark.parametrize('unknown_fee', [False, True])
@pytest.mark.parametrize('pending_protected', [False, True])
def test_loss_threshold_does_not_dump_position(tmp_path, monkeypatch, path, latched, unknown_fee, pending_protected):
    if path == 'manual':
        service, exchange = legacy_holding(tmp_path, cost='30', ask='.29')
        exchange.snapshot_value['book']['bids'] = [dict(price=Decimal('.20'), size=Decimal('100'))]
        if unknown_fee:
            exchange.snapshot_value['trades'][0]['maker_orders'][0]['fee'] = None
            exchange.snapshot_value['market']['fee'] = None
        if latched:
            session, = service.store.lp_sessions()
            service.store.lp_update_session(session['session_id'], state='stop_loss_exit', patch=dict(
                stop_loss_latched=True, stop_loss_triggered_loss=Decimal('10'), stop_loss_triggered_at=service.clock()))
        if pending_protected:
            session, = service.store.lp_sessions()
            service.store.lp_update_session(session['session_id'], patch=dict(
                protected_exit_attempt_key='historical-unknown', protected_exit_attempt_state='unknown'))
        service.tick()
        if pending_protected:
            service.tick()
            assert not [o for o in exchange.posts if o['side'] == 'SELL']
            assert exchange.protected_sells == [] and exchange.cancels == []
            assert service.store.lp_sessions()[0]['protected_exit_attempt_state'] == 'unknown'
            return
        sells = [o for o in exchange.posts if o['side'] == 'SELL']
        assert len(sells) == 1 and sells[0]['price'] == Decimal('.30')
        assert sells[0]['post_only'] is True and exchange.protected_sells == [] and exchange.cancels == []
    else:
        from dataclasses import replace
        from open_trader.polymarket_lp_current_state import ProtectionGuard, OpeningProtectionUpdate, Fact
        live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
        try:
            public.bid, public.ask = Decimal('.20'), Decimal('.29')
            if unknown_fee:
                sdk.trades = tuple(t.model_copy(update={'maker_orders': tuple(m.model_copy(
                    update={'fee_rate_bps': None}) for m in t.maker_orders)}) for t in sdk.trades)
            if latched:
                snapshot = live.state.snapshot()
                scope, opening = next(iter(snapshot.openings.items()))
                guard = ProtectionGuard(scope, snapshot.current.cycle_generation, snapshot.scope_versions.get(scope, 0),
                    tuple((o.key, o.terms_version) for o in snapshot.orders if o.key[:3] == scope[:3]),
                    None, 'historical-stop-loss')
                assert live.state.apply_protection(OpeningProtectionUpdate(guard, replace(opening,
                    scope_version=guard.expected_scope_version + 1, stop_loss_latched=Fact('known', True), stop_loss_triggered_loss_usd=Fact('known', Decimal(10)),
                    stop_loss_triggered_at=live.state.snapshot().observation.accepted_at))) == 'applied'
            if pending_protected:
                from open_trader.polymarket_lp_current_state import ExitEvidenceUpdate, ExitAttempt
                positions = sdk.positions
                sdk.positions = tuple(dict(p, average_price=None) for p in positions)
                d2_tick(live)
                d2_tick(live)
                snapshot = live.state.snapshot()
                scope, sell = next(iter(snapshot.sells.items()))
                guard = ProtectionGuard(scope, snapshot.current.cycle_generation, snapshot.scope_versions[scope],
                    tuple((o.key, o.terms_version) for o in snapshot.orders if o.key[:3] == scope[:3]),
                    None, 'historical-unknown-request')
                fact = lambda value: Fact('known', value, (sell.positions_source,))
                old = ExitAttempt('historical-unknown', 'protected_exit', None, 'unknown', False,
                    fact(Decimal(100)), fact(Decimal(0)), fact(Decimal(100)), fact(Decimal('.20')),
                    fact('FOK'), fact(None), None, None, 'ended')
                assert live.state.apply_protection(ExitEvidenceUpdate(guard, replace(sell, protected=old))) == 'applied'
                sdk.positions = positions
                d2_tick(live)
                d2_tick(live)
                assert not [o for o in sdk.posts if o['side'] == 'SELL']
                assert sdk.market_orders == [] and sdk.cancels == []
                assert live.state.snapshot().sells[scope].protected.request_id == old.request_id
                return
            d2_tick(live)
            d2_tick(live)
            sells = [o for o in sdk.posts if o['side'] == 'SELL']
            assert len(sells) == 1 and sells[0]['price'] == Decimal('.30')
            assert sells[0]['post_only'] is True and sdk.market_orders == [] and sdk.cancels == []
            opening, = live.state.snapshot().openings.values()
            assert opening.stop_loss_latched.value is latched
            if unknown_fee:
                assert opening.economics.opening_loss_usd.state == 'unknown'
        finally:
            assert live.close(3)
            live.trading.close()


@pytest.mark.parametrize('initial_quantity', [0, 40])
def test_position_observation_starts_sell_without_slow_cadence(tmp_path, monkeypatch, initial_quantity):
    from tests.test_lp_async_live_contract import sdk_boundary, pump_live, LPReadRuntime
    from tests.test_lp_order_registration_contract import _maker_order, _trade, CONDITION_ID, TOKEN_ID
    live, sdk, public, values = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
    try:
        # Confirm actual own fills before the slower positions endpoint catches up.
        # A missing/stale account quantity must still block the first sale.
        public.failed = True
        d2_tick(live)
        d2_tick(live)
        assert next(iter(live.state.snapshot().own_fills.values())).quantity.value == 100
        sdk.positions = (() if not initial_quantity else (dict(condition_id=CONDITION_ID,
            token_id=TOKEN_ID, size='40', average_price='.305'),))
        live.run_once()  # publish the initial account quantity before low-lane book completion
        public.failed = False
        boundary, clock = sdk_boundary(live, sdk, public, values)
        live.read_runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
        def settled():
            observation = live.state.snapshot().observation
            return bool(observation and observation.order_view.orders_source.checked_at == clock.utc()
                and not any(s['active'] for s in live.read_runtime.status()['scopes']))
        pump_live(live, boundary, lambda:settled() and (not initial_quantity or any(
            e[0] == 'entered' and e[1].startswith('position:') and e[2] == 'book' for e in boundary.events)))
        if initial_quantity:
            assert any(e[0] == 'entered' and e[1].startswith('position:') and e[2] == 'book' for e in boundary.events)
        assert not [o for o in sdk.posts if o['side'] == 'SELL']
        values[0] += 5
        sdk.terminal_orders['entry-A'] = sdk.orders[0]
        sdk.allow_exact = True
        sdk.orders = ()  # disappearance of every BUY leaves only holdings as read candidates
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='100', average_price='.305'),)
        try:
            pump_live(live, boundary, lambda:bool([o for o in sdk.posts if o['side'] == 'SELL']) and settled())
        except AssertionError:
            snapshot = live.state.snapshot()
            raise AssertionError(repr(dict(positions=snapshot.positions, sells=snapshot.sells,
                openings=snapshot.openings, fills=snapshot.own_fills,
                complete=(snapshot.observation.orders_complete,snapshot.observation.positions_complete),
                last=live.status()['last_current_evidence']))) from None
        assert any(e[0] == 'entered' and e[1].startswith('position:') and e[2] == 'book'
            and e[3] == clock() for e in boundary.events)
        sells = [o for o in sdk.posts if o['side'] == 'SELL']
        assert len(sells) == 1 and sells[0]['size'] == 100 and sells[0]['price'] >= Decimal('.31')
        assert sells[0]['post_only'] is True and sdk.market_orders == []
        assert values[0] == 6  # no 30/60-second clock advance
    finally:
        assert live.close(4)
        live.trading.close()


@pytest.mark.parametrize('path', ['manual', 'auto'])
@pytest.mark.parametrize('partial_sale', [False, True, 'current_cost'])
def test_quantity_change_replaces_only_after_finality(tmp_path, monkeypatch, path, partial_sale):
    from copy import deepcopy
    if partial_sale == 'current_cost':
        return current_cost_replacement(tmp_path, monkeypatch, path)
    if path == 'manual':
        service, exchange = legacy_holding(tmp_path, cost='30', ask='.32')
        if not partial_sale:
            first = _inventory_snapshot(service.clock(), buy_quantity=Decimal(40), buy_cost=Decimal(12),
                residual=Decimal(40), residual_value=Decimal('11.2'))
            first['orders'][0]['original_size'] = Decimal(100)
            exchange.snapshot_value = first
        service.tick()
        initial = Decimal(100 if partial_sale else 40)
        assert exchange.posts[-1]['quantity'] == initial
        sold = Decimal(40 if partial_sale else 0)
        residual = Decimal(60 if partial_sale else 100)
        current = _inventory_snapshot(service.clock(), buy_quantity=initial, buy_cost=initial*Decimal('.30'),
            sold_quantity=sold, sold_revenue=sold*Decimal('.31'), residual=residual,
            residual_value=residual*Decimal('.28'), passive_status='LIVE', orders_terminal=False)
        current['orders'][0]['original_size'] = Decimal(100)
        current['orders'][1].update(original_size=initial, size_matched=sold, remaining_size=initial-sold,
            price=exchange.posts[-1]['price'])
        if not partial_sale:
            session, = service.store.lp_sessions()
            history = deepcopy(session['order_history'])
            history['new-buy'] = dict(history['order-1'], order_id='new-buy', quantity=Decimal(60),
                size_matched=Decimal(60), price=Decimal('.31'), status='CANCELED')
            service.store.lp_update_session(session['session_id'], patch=dict(
                augment_order_ids=['new-buy'], order_history=history))
            current['orders'].append(dict(order_id='new-buy', status='CANCELED', side='BUY',
                token_id=session['token_id'], original_size=Decimal(60), size_matched=Decimal(60), price=Decimal('.31')))
            current['trades'].append(dict(trade_id='new-buy-fill', token_id=session['token_id'],
                side='BUY', status='CONFIRMED', maker_orders=[dict(order_id='new-buy', token_id=session['token_id'],
                    side='BUY', matched_amount=Decimal(60), price=Decimal('.31'), fee=Decimal(0))]))
        exchange.snapshot_value = current
        service.tick()
        assert len([o for o in exchange.posts if o['side']=='SELL']) == 1
        if partial_sale:
            assert exchange.cancels == []
            assert current['orders'][1]['remaining_size'] == 60
            return
        assert exchange.cancels == ['order-2']
        service.tick()  # cancellation ACK is not a final venue receipt
        assert len([o for o in exchange.posts if o['side']=='SELL']) == 1
        current['orders'][1]['status'] = 'CANCELED'
        service.tick()
        sells = [o for o in exchange.posts if o['side']=='SELL']
        assert [o['quantity'] for o in sells] == [40, 100]
        assert sells[-1]['price'] == Decimal('.31') and sells[-1]['post_only'] is True
        assert exchange.protected_sells == []
    else:
        from tests.test_lp_order_registration_contract import _maker_order, _trade, CONDITION_ID, TOKEN_ID
        live, sdk, public, values = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
        try:
            if not partial_sale:
                sdk.orders = tuple(o.model_copy(update={'size_matched': Decimal(40), 'status': 'LIVE'}) for o in sdk.orders)
                sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='40', average_price='.30'),)
                sdk.trades = (_trade('buy40', _maker_order('entry-A', 'BUY', '40', '.30'), size='40'),)
            d2_tick(live)
            d2_tick(live)
            sells = [o for o in sdk.posts if o['side']=='SELL']
            assert len(sells)==1 and sells[0]['size']==(100 if partial_sale else 40)
            if partial_sale:
                sale = _trade('sale40', _maker_order('passive-exit','SELL','40','.32'), size='40')
                sdk.orders = tuple(o.model_copy(update={'size_matched': Decimal(40)}) if o.id=='passive-exit' else o for o in sdk.orders)
                sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='60', average_price='.30'),)
                sdk.trades += (sale,)
            else:
                public.ask = Decimal('.29')
                sdk.orders = tuple(o.model_copy(update={'size_matched': Decimal(100), 'status':'FILLED'}) if o.id=='entry-A' else o for o in sdk.orders)
                sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='100', average_price='.305'),)
                sdk.trades += (_trade('buy60',_maker_order('entry-A','BUY','60','.30'),size='60'),)
            d2_tick(live)
            d2_tick(live)
            assert len([o for o in sdk.posts if o['side']=='SELL']) == 1
            if partial_sale:
                assert sdk.cancels == [] and sdk.market_orders == []
                sell, = live.state.snapshot().sells.values()
                assert sell.residual_quantity.value==60 and sell.current_sells[0].remaining.value==60
                return
            assert sdk.cancels == [('passive-exit',)]
            d2_tick(live)
            assert len([o for o in sdk.posts if o['side']=='SELL'])==1
            sdk.allow_exact = True
            d2_tick(live)
            d2_tick(live)
            sells = [o for o in sdk.posts if o['side']=='SELL']
            assert [o['size'] for o in sells]==[40,100]
            assert sells[-1]['price'] == Decimal('.31') and sells[-1]['post_only'] is True
            assert sdk.market_orders == []
        finally:
            assert live.close(3)
            live.trading.close()


@pytest.mark.parametrize('failure', ['failed', 'stale', 'wrong_token'])
def test_holding_read_failure_retries_without_replay(tmp_path, monkeypatch, failure):
    from tests.test_lp_async_live_contract import sdk_boundary, pump_live, LPReadRuntime
    from tests.test_lp_order_registration_contract import TOKEN_ID
    from datetime import timedelta
    live, sdk, public, values = _d2_filled_opening(tmp_path/'auto', monkeypatch, manual_sale=False)
    try:
        public.failed = True
        d2_tick(live)
        d2_tick(live)  # prove actual fills, with no usable book or submission
        public.failed = False
        original_book = public.get_order_book
        bad = [True]
        def book(**kwargs):
            if bad[0] and failure == 'failed':
                raise TimeoutError('offline book failure')
            result = original_book(**kwargs)
            if bad[0] and failure == 'stale':
                return result.model_copy(update={'timestamp': result.timestamp-timedelta(seconds=11)})
            if bad[0] and failure == 'wrong_token':
                return result.model_copy(update={'token_id': 'wrong-token'})
            return result
        public.get_order_book = book
        boundary, clock = sdk_boundary(live, sdk, public, values)
        live.read_runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
        def settled():
            status = live.read_runtime.status()
            return bool(status['scopes'] and not any(s['active'] for s in status['scopes'])
                and status['next_dependency_monotonic'] is not None and status['next_dependency_monotonic'] > clock())
        pump_live(live,boundary,lambda:settled() and any(e[0]=='entered' and e[2]=='book' for e in boundary.events))
        assert not [o for o in sdk.posts if o['side']=='SELL']
        reads = len([e for e in boundary.events if e[0]=='entered' and e[2]=='book'])
        values[0] = 5  # before the five-second read cooldown from t=1
        live.read_runtime.wake()
        for _ in range(3):
            live.run_once(); live.run_protection_once()
        assert len([e for e in boundary.events if e[0]=='entered' and e[2]=='book']) == reads
        bad[0] = False
        def unknown_post(signed):
            sdk.posts.append(signed)
            raise TimeoutError('unknown passive receipt')
        sdk.post_order = unknown_post
        values[0] = 6;live.read_runtime.wake()
        pump_live(live,boundary,lambda:bool([o for o in sdk.posts if o['side']=='SELL']) and settled())
        for _ in range(3):
            live.run_once()
            for handle in live.run_protection_once():
                handle.result(3);handle._thread.join(3)
        sells = [o for o in sdk.posts if o['side']=='SELL']
        assert len(sells)==1 and sells[0]['post_only'] is True and sells[0]['token_id']==TOKEN_ID
        assert sdk.market_orders == []
        sell, = live.state.snapshot().sells.values()
        assert sell.passive.state=='unknown' and sell.passive.native_state=='ended'
    finally:
        assert live.close(4)
        live.trading.close()
    # A new legacy service instance sees the durable UNKNOWN and cannot replay.
    service, exchange = legacy_holding(tmp_path/'manual', cost='30')
    exchange.post_failures = [TimeoutError('unknown passive receipt')]
    service.tick()
    assert len([o for o in exchange.posts if o['side']=='SELL'])==1
    restarted = PolymarketLPService(service.store, exchange, clock=service.clock)
    restarted.tick();restarted.tick()
    assert len([o for o in exchange.posts if o['side']=='SELL'])==1
    assert exchange.protected_sells == []


def current_cost_replacement(tmp_path, monkeypatch, path, *, cost='.50'):
    """BUY100@.30, SELL50, BUY50@.70: remaining100 cost50, not65/150."""
    if path == 'manual':
        from copy import deepcopy
        service, exchange = legacy_holding(tmp_path, cost='30', ask='.72')
        service.tick()
        session, = service.store.lp_sessions()
        history = deepcopy(session['order_history'])
        history['new-buy'] = dict(history['order-1'], order_id='new-buy', quantity=Decimal(50),
            size_matched=Decimal(50), price=Decimal('.70'), status='CANCELED')
        service.store.lp_update_session(session['session_id'], patch=dict(augment_order_ids=['new-buy'],order_history=history))
        current = _inventory_snapshot(service.clock(), buy_quantity=Decimal(100), buy_cost=Decimal(30),
            sold_quantity=Decimal(50),sold_revenue=Decimal(36),residual=Decimal(100),residual_value=Decimal(40),
            passive_status='LIVE', orders_terminal=False)
        current['account']['positions'][0]['average_price'] = Decimal(cost) if cost is not None else None
        current['orders'][1].update(original_size=Decimal(100),size_matched=Decimal(50),remaining_size=Decimal(50),price=Decimal('.72'))
        current['orders'].append(dict(order_id='new-buy',status='CANCELED',side='BUY',token_id=session['token_id'],
            original_size=Decimal(50),size_matched=Decimal(50),price=Decimal('.70')))
        current['trades'].append(dict(trade_id='new-buy-fill',token_id=session['token_id'],side='BUY',status='CONFIRMED',
            maker_orders=[dict(order_id='new-buy',token_id=session['token_id'],side='BUY',matched_amount=Decimal(50),price=Decimal('.70'),fee=Decimal(0))]))
        current['book']['asks']=[dict(price=Decimal('.49'),size=Decimal(1000))]
        exchange.snapshot_value=current
        service.tick()
        assert exchange.cancels==['order-2']
        service.tick()
        assert len([o for o in exchange.posts if o['side']=='SELL'])==1
        assert exchange.cancels==['order-2']
        current['orders'][1]['status']='CANCELED'
        service.tick()
        if cost != '.50':
            assert len([o for o in exchange.posts if o['side']=='SELL'])==1
            assert exchange.cancels==['order-2'] and exchange.protected_sells==[]
            assert service.store.lp_sessions()[0]['reconciliation']==(
                'passive_cost_unknown' if cost is None else 'passive_cost_above_maximum')
            current['account']['positions'][0]['average_price'] = Decimal('.50')
            service.tick()
        sells=[o for o in exchange.posts if o['side']=='SELL']
        assert len(sells)==2 and sells[-1]['quantity']==100 and sells[-1]['price']==Decimal('.50')
        assert sells[-1]['post_only'] is True and exchange.protected_sells==[]
        assert Decimal(service.store.lp_sessions()[0]['buy_cost'])==65
        service.tick()
        assert len([o for o in exchange.posts if o['side']=='SELL'])==2
        assert exchange.cancels==['order-2']
    else:
        from polymarket.models.clob.order_response import AcceptedOrder
        from tests.test_lp_d2_integration import _d2_protection_runtime
        from tests.test_lp_order_registration_contract import _open_order,_maker_order,_trade,CONDITION_ID,TOKEN_ID
        live,sdk,public,values=_d2_protection_runtime(tmp_path,monkeypatch,quantity='150',bid='.70',ask='.72')
        terminal={};allow=[False]
        values[0]=1
        sdk.list_account_trades=lambda **kwargs:sdk.trades
        try:
            sdk.orders=tuple(o.model_copy(update={'size_matched':Decimal(100)}) for o in sdk.orders)
            sdk.positions=(dict(condition_id=CONDITION_ID,token_id=TOKEN_ID,size='100',average_price='.30'),)
            sdk.trades=(_trade('buy100',_maker_order('entry-A','BUY','100','.30'),size='100'),)
            def post(signed):
                sdk.posts.append(signed)
                oid='passive-'+str(len([p for p in sdk.posts if p['side']=='SELL']))
                sdk.orders+=(_open_order(oid,'SELL',price=str(signed['price']),original=str(signed['size'])),)
                return AcceptedOrder(order_id=oid,status='live',making_amount=Decimal(0),taking_amount=Decimal(0),trade_ids=(),transactions_hashes=())
            def cancel(*,order_ids):
                sdk.cancels.append(order_ids)
                terminal.update({o.id:o.model_copy(update={'status':'CANCELED'}) for o in sdk.orders if o.id in order_ids})
                sdk.orders=tuple(o for o in sdk.orders if o.id not in order_ids)
                return dict(canceled=order_ids,not_canceled={})
            sdk.post_order=post;sdk.cancel_orders=cancel
            sdk.get_order=lambda *,order_id:terminal.get(order_id) if allow[0] else None
            d2_tick(live);d2_tick(live)
            assert len([o for o in sdk.posts if o['side']=='SELL'])==1
            sdk.orders=tuple(o.model_copy(update={'size_matched':Decimal(50)}) if o.id=='passive-1' else
                o.model_copy(update={'size_matched':Decimal(150),'status':'FILLED'}) for o in sdk.orders)
            sdk.trades+=(_trade('sale50',_maker_order('passive-1','SELL','50','.72'),size='50'),
                _trade('buy50',_maker_order('entry-A','BUY','50','.70'),size='50'))
            sdk.positions=(dict(condition_id=CONDITION_ID,token_id=TOKEN_ID,size='100',average_price=cost),)
            public.ask=Decimal('.49')
            d2_tick(live);d2_tick(live)
            assert sdk.cancels==[('passive-1',)]
            d2_tick(live)
            assert len([o for o in sdk.posts if o['side']=='SELL'])==1
            assert sdk.cancels==[('passive-1',)]
            allow[0]=True
            d2_tick(live);d2_tick(live)
            if cost != '.50':
                assert len([o for o in sdk.posts if o['side']=='SELL'])==1
                assert sdk.cancels==[('passive-1',)] and sdk.market_orders==[]
                sell, = live.state.snapshot().sells.values()
                assert sell.passive.state=='terminal'
                assert ('passive_cost_unknown' if cost is None else 'passive_cost_above_maximum') in sell.inventory_cost_usd.reason_codes
                sdk.positions=(dict(condition_id=CONDITION_ID,token_id=TOKEN_ID,size='100',average_price='.50'),)
                d2_tick(live);d2_tick(live)
            sells=[o for o in sdk.posts if o['side']=='SELL']
            assert len(sells)==2 and sells[-1]['size']==100 and sells[-1]['price']==Decimal('.50')
            assert sells[-1]['post_only'] is True and sdk.market_orders==[]
            d2_tick(live)
            assert len([o for o in sdk.posts if o['side']=='SELL'])==2
            assert sdk.cancels==[('passive-1',)]
        finally:
            assert live.close(3)
            live.trading.close()


@pytest.mark.parametrize('path', ['manual', 'auto'])
@pytest.mark.parametrize('cost', [None, '.995'])
def test_quantity_mismatch_cancels_before_new_cost_validation(tmp_path, monkeypatch, path, cost):
    current_cost_replacement(tmp_path, monkeypatch, path, cost=cost)


def test_historical_protected_fill_waits_for_position_convergence(tmp_path):
    from tests.test_polymarket_lp import explicit_legacy_protected_tick
    service, exchange = legacy_holding(tmp_path, cost='30', ask='.31')
    service.tick()
    historical = _inventory_snapshot(service.clock(), buy_quantity=Decimal('100'), buy_cost=Decimal('30'),
        residual=Decimal('100'), residual_value=Decimal('10'), passive_status='LIVE',
        bids=[dict(price=Decimal('.25'), size=Decimal('40'))])
    historical['orders'][1]['price'] = Decimal('.31')
    exchange.snapshot_value = historical
    explicit_legacy_protected_tick(service)
    assert exchange.cancels==['order-2'] and exchange.protected_sells==[]
    historical['orders'][1]['status'] = 'CANCELED'
    accepted = explicit_legacy_protected_tick(service)
    assert exchange.protected_sells == [dict(token_id=accepted['token_id'],
        quantity=Decimal('40'), min_price=Decimal('.25'))]
    assert accepted['protected_exit_order_id']=='stop-1'
    assert accepted['protected_exit_attempt_state']=='accepted'
    assert Decimal(accepted['protected_exit_submit_quantity'])==40
    assert Decimal(accepted['protected_exit_submit_residual_quantity'])==100
    assert len([o for o in exchange.posts if o['side']=='SELL'])==1

    settled = _inventory_snapshot(service.clock(), buy_quantity=Decimal('100'), buy_cost=Decimal('30'),
        sold_quantity=Decimal('40'), sold_revenue=Decimal('10'), residual=Decimal('100'),
        residual_value=Decimal('25'), passive_status='CANCELED', protected_status='FILLED')
    settled['orders'][1].update(original_size=Decimal('100'), size_matched=Decimal('0'),
        remaining_size=Decimal('100'), price=Decimal('.31'))
    settled['orders'][-1].update(original_size=Decimal('40'), size_matched=Decimal('40'),
        remaining_size=Decimal('0'), price=Decimal('.25'), average_price=Decimal('.25'))
    settled['trades'][1]['maker_orders'][0]['order_id'] = 'stop-1'
    settled['account']['positions'][0]['average_price'] = Decimal('.30')
    exchange.snapshot_value = settled
    restarted = PolymarketLPService(service.store, exchange, clock=service.clock)
    for _ in range(3):
        pending = restarted.tick()
        assert pending['protected_exit_order_id']=='stop-1'
        assert pending['protected_exit_attempt_state']=='terminal'
        assert pending['residual_quantity']==100 and pending['sold_quantity']==40
        assert len([o for o in exchange.posts if o['side']=='SELL'])==1
        assert len(exchange.protected_sells)==1

    settled['account']['positions'][0]['size'] = Decimal('60')
    for _ in range(3):
        converged = restarted.tick()
    assert converged['protected_exit_order_id'] is None
    sells = [o for o in exchange.posts if o['side']=='SELL']
    assert [o['quantity'] for o in sells]==[100,60] and sells[-1]['price']==Decimal('.31')
    assert sells[-1]['post_only'] is True and len(exchange.protected_sells)==1
    assert converged['residual_quantity']==60 and converged['sold_quantity']==40
