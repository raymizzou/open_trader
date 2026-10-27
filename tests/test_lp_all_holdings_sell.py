"""All-account holding exits through the real runtime and offline SDK ports."""
from decimal import Decimal
import pytest

from polymarket.models.clob.order_response import AcceptedOrder

from tests.test_lp_d2_integration import _d2_protection_runtime
from tests.test_lp_fill_then_hang import d2_tick
from tests.test_lp_order_registration_contract import CONDITION_ID, TOKEN_ID, WALLET, _open_order


def monitored(live, tmp_path):
    from open_trader.polymarket_lp import PolymarketLPService
    from tests.test_lp_d2_execution_bridge import attach
    producer = PolymarketLPService(live.store, live.trading, clock=live._utc_now)
    return attach(live, producer, tmp_path)


def imported_holding(tmp_path, monkeypatch, *, ask='.29'):
    live, sdk, public, clock = _d2_protection_runtime(
        tmp_path, monkeypatch, submit=False, ask=ask, bid='.28')
    sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID,
        size='10', average_price='.305'),)

    def post(signed):
        sdk.posts.append(signed)
        order_id = 'holding-' + str(len(sdk.posts))
        sdk.orders += (_open_order(order_id, 'SELL', price=str(signed['price']),
            original=str(signed['size'])),)
        return AcceptedOrder(order_id=order_id, status='live', making_amount=Decimal('0'),
            taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())

    sdk.post_order = post
    return live, sdk, public, clock


def test_imported_position_quotes_account_cost_floor(tmp_path, monkeypatch):
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch)
    try:
        assert live.store.lp_sessions() == []
        d2_tick(live)
        assert len(sdk.posts) == 1
        order, = sdk.posts
        assert order['token_id'] == TOKEN_ID and order['side'] == 'SELL'
        assert order['size'] == Decimal('10') and order['price'] == Decimal('.31')
        assert order['order_type'] == 'GTC' and order['post_only'] is True
        assert sdk.market_orders == [] and sdk.cancels == []
        ledger, = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
        assert ledger['order_id'] == 'holding-1'
        assert ledger['condition_id'] == CONDITION_ID and ledger['token_id'] == TOKEN_ID
        assert ledger['quantity'] == '10.00' and ledger['price'] == '0.31'
        assert ledger['state'] == 'accepted' and ledger['request_id']
    finally:
        assert live.close(3)
        live.trading.close()


def test_imported_position_quotes_external_ask(tmp_path, monkeypatch):
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch, ask='.34')
    try:
        d2_tick(live)
        order, = sdk.posts
        assert order['side'] == 'SELL' and order['token_id'] == TOKEN_ID
        assert order['size'] == Decimal('10') and order['price'] == Decimal('.34')
        assert order['order_type'] == 'GTC' and order['post_only'] is True
        assert sdk.market_orders == [] and sdk.cancels == []
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize(('account_cost', 'ask', 'expected'), [('.305', '.29', '.30'),
    (None, '.29', '.30'), ('.305', '.34', '.34')])
def test_cost_source_preserves_session_priority(tmp_path, monkeypatch, account_cost, ask, expected):
    from tests.test_lp_d2_integration import _d2_filled_opening
    live, sdk, public, clock = _d2_filled_opening(tmp_path / 'session', monkeypatch, manual_sale=False)
    sdk.positions = tuple(dict(p, average_price=account_cost) for p in sdk.positions)
    public.ask = Decimal(ask)
    try:
        execution = monitored(live, tmp_path / 'session')
        d2_tick(live)
        d2_tick(live)
        row, = execution.lp_monitor_summary(now=live._utc_now())['holdings']['items']
        assert row['cost_source'] == 'session_ledger'
        assert Decimal(row['cost_floor']) == Decimal('.30')
        sells = [o for o in sdk.posts if o['side'] == 'SELL']
        assert len(sells) == 1 and sells[0]['price'] == Decimal(expected)
        assert sells[0]['size'] == 100
        public.ask = Decimal('.40')
        d2_tick(live)
        assert len([o for o in sdk.posts if o['side'] == 'SELL']) == 1
        assert sdk.cancels == []
    finally:
        assert live.close(3)
        live.trading.close()
    imported, account, _, _ = imported_holding(tmp_path / 'imported', monkeypatch)
    try:
        execution = monitored(imported, tmp_path / 'imported')
        d2_tick(imported)
        row, = execution.lp_monitor_summary(now=imported._utc_now())['holdings']['items']
        assert row['cost_source'] == 'account_average' and Decimal(row['cost_floor']) == Decimal('.31')
        assert account.posts[0]['price'] == Decimal('.31')
    finally:
        assert imported.close(3)
        imported.trading.close()


def mixed_holding(tmp_path, monkeypatch, *, size='110', average='.305', local='.30', ask='.29'):
    from tests.test_lp_d2_integration import _d2_filled_opening
    if Decimal(local) > Decimal('.30'):
        from tests.test_lp_order_registration_contract import _maker_order, _trade
        live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch, quantity='100',
            bid=local, ask=str(Decimal(local) + Decimal('.02')))
        sdk.orders = tuple(o.model_copy(update={'status': 'FILLED', 'size_matched': Decimal(100)}) for o in sdk.orders)
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size=size, average_price=average),)
        sdk.trades = (_trade('buy-fill', _maker_order('entry-A', 'BUY', '100', local), size='100'),)
        sdk.list_account_trades = lambda **kw: sdk.trades
    else:
        live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
    sdk.positions = tuple(dict(p, size=size, average_price=average) for p in sdk.positions)
    sdk.trades = tuple(t.model_copy(update={'maker_orders': tuple(m.model_copy(update={
        'price': Decimal(local)}) for m in t.maker_orders)}) for t in sdk.trades)
    public.ask = Decimal(ask)
    terminals = {}
    sdk.get_order = lambda *, order_id: terminals.get(order_id) or next((o for o in sdk.orders if o.id == order_id), None)
    def post(signed):
        sdk.posts.append(signed)
        oid = 'sell-' + str(sum(o['side'] == 'SELL' for o in sdk.posts))
        sdk.orders += (_open_order(oid, 'SELL', price=str(signed['price']), original=str(signed['size'])).model_copy(
            update={'created_at': live._utc_now()}),)
        return AcceptedOrder(order_id=oid, status='live', making_amount=Decimal(0), taking_amount=Decimal(0),
            trade_ids=(), transactions_hashes=())
    def cancel(*, order_ids):
        sdk.cancels.append(order_ids)
        terminals.update({o.id: o.model_copy(update={'status': 'CANCELED'}) for o in sdk.orders if o.id in order_ids})
        sdk.orders = tuple(o for o in sdk.orders if o.id not in order_ids)
        return dict(canceled=order_ids, not_canceled={})
    sdk.post_order, sdk.cancel_orders = post, cancel
    return live, sdk, public, clock, terminals


def active_sells(sdk):
    return {o.id: o for o in sdk.orders if o.side == 'SELL' and o.status == 'LIVE'}


@pytest.mark.parametrize('imported_before_first_sell', [True, False])
def test_mixed_session_and_imported_quantity_is_fully_covered(tmp_path, monkeypatch, imported_before_first_sell):
    live, sdk, public, clock, terminals = mixed_holding(tmp_path, monkeypatch,
        size='110' if imported_before_first_sell else '100')
    try:
        execution = monitored(live, tmp_path)
        if not imported_before_first_sell:
            d2_tick(live)
            d2_tick(live)
            old, = active_sells(sdk).values()
            assert old.original_size == 100 and old.price == Decimal('.30')
            sdk.positions = tuple(dict(p, size='110') for p in sdk.positions)
            d2_tick(live)
            assert sdk.cancels == [(old.id,)]
            assert sum(o.original_size - o.size_matched for o in active_sells(sdk).values()) <= 110
        for _ in range(6):
            d2_tick(live)
        orders = active_sells(sdk)
        assert sum(o.original_size - o.size_matched for o in orders.values()) == 110
        assert all(o.price >= Decimal('.31') for o in orders.values())
        assert all(p['order_type'] == 'GTC' and p['post_only'] for p in sdk.posts if p['side'] == 'SELL')
        before = set(orders)
        d2_tick(live)
        assert set(active_sells(sdk)) == before and sdk.market_orders == []
        row, = execution.lp_monitor_summary(now=live._utc_now())['holdings']['items']
        assert row['cost_source'] == 'mixed_max' and Decimal(row['cost_floor']) == Decimal('.31')
        assert Decimal(row['covered_quantity']) == 110 and Decimal(row['uncovered_quantity']) == 0
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize(('local', 'account', 'ask', 'floor', 'price', 'state'), [
    ('.30', '.305', '.29', '.31', '.31', 'covered'),
    ('.40', '.305', '.29', '.40', '.40', 'covered'),
    ('.40', '.305', '.45', '.40', '.45', 'covered'),
    ('.30', None, '.29', None, None, 'cost_unknown'),
    ('.30', '0', '.29', None, None, 'cost_unknown'),
    ('.30', 'NaN', '.29', None, None, 'cost_unknown'),
    ('.30', '.995', '.29', None, None, 'cost_above_maximum')])
def test_mixed_cost_uses_higher_known_basis(tmp_path, monkeypatch, local, account, ask, floor, price, state):
    live, sdk, public, clock, terminals = mixed_holding(tmp_path, monkeypatch, local=local, average=account, ask=ask)
    try:
        execution = monitored(live, tmp_path)
        for _ in range(5):
            d2_tick(live)
        row, = execution.lp_monitor_summary(now=live._utc_now())['holdings']['items']
        assert row['state'] == state
        assert row['cost_floor'] is None if floor is None else Decimal(row['cost_floor']) == Decimal(floor)
        if price is not None:
            assert sum(o.original_size-o.size_matched for o in active_sells(sdk).values()) == 110
            assert all(o.price == Decimal(price) for o in active_sells(sdk).values())
            assert row['cost_source'] == 'mixed_max'
        else:
            assert not [o for o in sdk.posts if o['side'] == 'SELL']
            assert sdk.cancels == [] and row['reason']
        assert sdk.market_orders == []
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('mode', ['fills', 'pending_restart', 'unknown_restart', 'holding_receipt_pending'])
def test_mixed_paths_preserve_fill_convergence_and_unknown_reservations(tmp_path, monkeypatch, mode):
    from threading import Event
    from tests.test_lp_order_registration_contract import _maker_order, _trade
    live, sdk, public, clock, terminals = mixed_holding(tmp_path, monkeypatch)
    reopened = None
    release, entered = Event(), Event()
    try:
        execution = monitored(live, tmp_path)
        if mode == 'holding_receipt_pending':
            d2_tick(live)
            d2_tick(live)
            session, = active_sells(sdk).values()
            post = sdk.post_order
            def held_holding_post(signed):
                receipt = post(signed)
                if signed['side'] == 'SELL' and signed['size'] == 10:
                    entered.set()
                    assert release.wait(3), 'holding receipt release watchdog'
                return receipt
            sdk.post_order = held_holding_post
            live.run_once()
            handles = live.run_protection_once()
            assert entered.wait(3), 'holding visible before receipt watchdog'
            holding = next(o for o in active_sells(sdk).values() if o.original_size == 10)
            d2_tick(live)  # API order is visible while its local POST still owns the receipt.
            sdk.orders = tuple(o.model_copy(update={'status': 'FILLED', 'size_matched': Decimal(10)})
                if o.id == holding.id else o for o in sdk.orders)
            sdk.positions = tuple(dict(p, size='100') for p in sdk.positions)
            sdk.trades += (_trade('holding-before-receipt', _maker_order(holding.id, 'SELL', '10', '.31'), size='10'),)
            for _ in range(3):
                d2_tick(live)
            opening, = live.state.snapshot().openings.values()
            sell, = live.state.snapshot().sells.values()
            assert opening.economics.sold_revenue_usd.value == 0
            assert sell.position_reconciled and sell.residual_quantity.value == 100
            release.set()
            for handle in handles:
                handle.result(3)
                handle._thread.join(3)
                assert handle.done()
            for _ in range(3):
                d2_tick(live)
            assert set(active_sells(sdk)) == {session.id}
            assert len([o for o in sdk.posts if o['side'] == 'SELL']) == 2
            assert sdk.cancels == sdk.market_orders == []
            return
        if mode != 'fills':
            from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
            from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
            class Crash(BaseException):
                pass
            sign, post = sdk.create_limit_order, sdk.post_order
            attempts = []
            def interrupted_sign(**kwargs):
                if mode == 'pending_restart' and kwargs['side'] == 'SELL':
                    attempts.append('sign')
                    raise Crash('offline pre-post process loss')
                return sign(**kwargs)
            def unknown_post(signed):
                attempts.append('post')
                raise TimeoutError('offline native outcome unknown')
            sdk.create_limit_order = interrupted_sign
            sdk.post_order = unknown_post if mode == 'unknown_restart' else post
            d2_tick(live)
            live.run_once()
            handles = live.run_protection_once()
            for handle in handles:
                if mode == 'pending_restart':
                    with pytest.raises(Crash):
                        handle.result(3)
                else:
                    handle.result(3)
                handle._thread.join(3)
                assert handle.done()
            assert len(attempts) == 1
            records = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
            assert any(r.get('kind') == 'session' and Decimal(r['quantity']) == 100
                and r['state'] in ('pending', 'unknown') for r in records)
            assert live.close(3)
            sdk.create_limit_order, sdk.post_order = sign, post
            reopened = LPD2LiveExecution(live.store, live.trading, account_id=WALLET, venue='polymarket',
                process_epoch='mixed-reopen', policy=live.policy, clock=lambda: clock[0], utc_now=live._utc_now)
            reopened.initialize(ValidatedStartAdmission('mixed-reopen', 'offline-permit'))
            for _ in range(3):
                d2_tick(reopened)
            assert len(attempts) == 1 and not active_sells(sdk)
            row, = reopened.holdings_summary(now=live._utc_now())['items']
            assert row['state'] == 'order_unknown' and row['reason']
            return
        d2_tick(live)
        sign = sdk.create_limit_order
        def held_sign(**kwargs):
            if kwargs['side'] == 'SELL' and not entered.is_set():
                entered.set()
                assert release.wait(3), 'controlled mixed signing watchdog'
            return sign(**kwargs)
        sdk.create_limit_order = held_sign
        live.run_once()
        handles = live.run_protection_once()
        assert entered.wait(3)
        live.run_once()
        assert live.run_protection_once() == ()
        assert not active_sells(sdk)
        release.set()
        for handle in handles:
            handle.result(3)
            handle._thread.join(3)
            assert handle.done()
        for _ in range(5):
            d2_tick(live)
        session = next(o for o in active_sells(sdk).values() if o.original_size == 100)
        imported = next(o for o in active_sells(sdk).values() if o.original_size == 10)
        sdk.orders = tuple(o.model_copy(update={'size_matched': Decimal(40)}) if o.id == session.id else o for o in sdk.orders)
        sdk.trades += (_trade('session-sell-40', _maker_order(session.id, 'SELL', '40', '.31'), size='40'),)
        posts_before = len(sdk.posts)
        for _ in range(3):
            d2_tick(live)  # stale API110 must not become invented imported50
        assert len(sdk.posts) == posts_before and sdk.cancels == []
        sdk.positions = tuple(dict(p, size='70') for p in sdk.positions)
        for _ in range(3):
            d2_tick(live)
        assert set(active_sells(sdk)) == {session.id, imported.id}
        assert sum(o.original_size-o.size_matched for o in active_sells(sdk).values()) == 70
        sdk.orders = tuple(o.model_copy(update={'status': 'FILLED', 'size_matched': Decimal(10)}) if o.id == imported.id else o for o in sdk.orders)
        sdk.trades += (_trade('holding-sell-10', _maker_order(imported.id, 'SELL', '10', '.31'), size='10'),)
        d2_tick(live)
        assert len(sdk.posts) == posts_before
        sdk.positions = tuple(dict(p, size='60') for p in sdk.positions)
        for _ in range(3):
            d2_tick(live)
        assert set(active_sells(sdk)) == {session.id}
        assert len(sdk.posts) == posts_before and sdk.cancels == sdk.market_orders == []
        sell, = live.state.snapshot().sells.values()
        assert sell.residual_quantity.value == 60  # holding fill was not a session sale
    finally:
        release.set()
        if reopened is not None:
            assert reopened.close(3)
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('cost', [None, '0', 'NaN'])
def test_unknown_cost_is_visible_and_never_submits(tmp_path, monkeypatch, cost):
    live, sdk, _, _ = imported_holding(tmp_path, monkeypatch)
    sdk.positions = tuple(dict(p, average_price=cost) for p in sdk.positions)
    try:
        execution = monitored(live, tmp_path)
        for _ in range(3):
            d2_tick(live)
            row, = execution.lp_monitor_summary(now=live._utc_now())['holdings']['items']
            assert row['state'] == 'cost_unknown' and row['cost_source'] == 'unknown'
            assert row['cost_floor'] is None and row['uncovered_quantity'] == '10'
        assert sdk.posts == sdk.cancels == sdk.market_orders == []
        assert live.store.lp_holding_sell_orders(WALLET, 'polymarket') == ()
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize(('quantity', 'submitted', 'residual', 'state'), [
    ('2.074', None, '2.074', 'below_minimum'), ('.009', None, '.009', 'dust'),
    ('19.9991', '19.99', '.0091', 'dust')])
def test_quantity_minimum_and_precision_have_reasons(tmp_path, monkeypatch, quantity, submitted, residual, state):
    live, sdk, public, _ = imported_holding(tmp_path, monkeypatch)
    sdk.positions = tuple(dict(p, size=quantity) for p in sdk.positions)
    original = public.get_order_book
    public.get_order_book = lambda **kw: original(**kw).model_copy(update={'min_order_size': Decimal(5)})
    try:
        execution = monitored(live, tmp_path)
        d2_tick(live)
        row, = execution.lp_monitor_summary(now=live._utc_now())['holdings']['items']
        assert row['state'] == state and row['reason']
        assert Decimal(row['quantity']) == Decimal(quantity)
        assert Decimal(row['uncovered_quantity']) == Decimal(residual)
        assert Decimal(row['minimum_quantity']) == 5
        if submitted is None:
            assert sdk.posts == []
        else:
            assert len(sdk.posts) == 1 and sdk.posts[0]['size'] == Decimal(submitted)
            assert sdk.posts[0]['size'] < Decimal(quantity)
        assert sdk.cancels == sdk.market_orders == []
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('market_state', ['active', 'active_empty_ask', 'closed', 'unknown'])
def test_market_status_distinguishes_zero_value(tmp_path, monkeypatch, market_state):
    live, sdk, public, _ = imported_holding(tmp_path, monkeypatch)
    sdk.positions = tuple(dict(p, current_value='0', current_price='0') for p in sdk.positions)
    book = public.get_order_book
    public.get_order_book = lambda **kw: book(**kw).model_copy(update={
        'bids': (), **({'asks': ()} if market_state == 'active_empty_ask' else {})})
    market = public.get_market
    if market_state == 'closed':
        def ended(**kw):
            value = market(**kw)
            return value.model_copy(update={'state': value.state.model_copy(update={'closed': True, 'accepting_orders': False})})
        public.get_market = ended
    elif market_state == 'unknown':
        public.get_market = lambda **kw: (_ for _ in ()).throw(OSError('offline market unavailable'))
    try:
        execution = monitored(live, tmp_path)
        d2_tick(live)
        row, = execution.lp_monitor_summary(now=live._utc_now())['holdings']['items']
        assert row['current_value'] == '0'
        if market_state.startswith('active'):
            assert len(sdk.posts) == 1 and sdk.posts[0]['price'] == Decimal('.31')
            assert row['state'] == 'covered'
        else:
            assert row['state'] == ('market_ended' if market_state == 'closed' else 'market_unknown')
            assert sdk.posts == []
        assert row['reason'] and sdk.cancels == sdk.market_orders == []
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('fully_covered', [False, True])
def test_existing_sells_cover_only_remaining_quantity(tmp_path, monkeypatch, fully_covered):
    from polymarket.models.clob.order_book import OrderBookLevel
    live, sdk, public, _ = imported_holding(tmp_path, monkeypatch)
    sdk.positions = tuple(dict(p, size='100') for p in sdk.positions)
    sdk.orders = (_open_order('manual', 'SELL', price='.31', original='100' if fully_covered else '60',
        matched='0' if fully_covered else '20'),)
    original = public.get_order_book
    def book(**kw):
        return original(**kw).model_copy(update={'asks': (
            OrderBookLevel(price=Decimal('.31'), size=Decimal('100' if fully_covered else '40')),
            OrderBookLevel(price=Decimal('.34'), size=Decimal('1000')))})
    public.get_order_book = book
    try:
        for _ in range(3):
            d2_tick(live)
        sells = [o for o in sdk.posts if o['side'] == 'SELL']
        if fully_covered:
            assert sells == []
        else:
            assert len(sells) == 1 and sells[0]['size'] == 60
            assert sells[0]['price'] == Decimal('.34')  # own .31 liquidity excluded
        assert sdk.cancels == [] and sdk.market_orders == []
    finally:
        assert live.close(3)
        live.trading.close()


def test_session_and_holding_paths_share_one_reservation(tmp_path, monkeypatch):
    from threading import Event
    from tests.test_lp_d2_integration import _d2_filled_opening
    live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
    entered, release = Event(), Event()
    original = sdk.create_limit_order
    d2_tick(live)  # settle exact own-fill receipt before selecting the next observation
    def sign(**kwargs):
        if kwargs['side'] == 'SELL':
            entered.set()
            assert release.wait(3), 'independent signing release watchdog'
        return original(**kwargs)
    sdk.create_limit_order = sign
    handles = []
    try:
        live.run_once()
        handles.extend(live.run_protection_once())
        assert entered.wait(3), 'session sell signing watchdog'
        # A fresh account read and another holding/protection pass occur while
        # the exact session request still owns its native signer and inventory.
        live.run_once()
        extra = live.run_protection_once()
        assert extra == ()
        records = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
        assert len(records) == 1 and records[0].get('kind') == 'session'
        assert records[0]['state'] == 'pending' and Decimal(records[0]['quantity']) == 100
        assert not [o for o in sdk.posts if o['side'] == 'SELL']
        release.set()
        for handle in handles:
            handle.result(3)
            handle._thread.join(3)
            assert handle.done()
        d2_tick(live)
        d2_tick(live)
        sells = [o for o in sdk.posts if o['side'] == 'SELL']
        assert len(sells) == 1 and sum(o['size'] for o in sells) == 100
        assert sdk.cancels == sdk.market_orders == []
    finally:
        release.set()
        assert live.close(3)
        live.trading.close()
    historical, sdk, _, _ = imported_holding(tmp_path / 'historical', monkeypatch)
    historical.store.lp_create_session('prior-protected', 'prior-protected', state='passive_exit',
        payload=dict(account_id=WALLET, condition_id=CONDITION_ID, token_id=TOKEN_ID,
            residual_quantity='10', protected_exit_attempt_key='historical-unknown',
            protected_exit_attempt_state='unknown'))
    # Legacy obligations are imported at startup, never read as current
    # trading authority by each account/protection loop.
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    assert historical.close(3)
    previous = historical
    historical = LPD2LiveExecution(previous.store, previous.trading, account_id=WALLET, venue='polymarket',
        process_epoch='legacy-obligation-startup', policy=previous.policy, clock=previous._clock, utc_now=previous._utc_now)
    historical.initialize(ValidatedStartAdmission('legacy-obligation-startup', 'offline-permit'))
    try:
        for _ in range(3):
            d2_tick(historical)
        assert sdk.posts == sdk.cancels == sdk.market_orders == []
        assert historical.store.lp_session('prior-protected')['protected_exit_attempt_state'] == 'unknown'
    finally:
        assert historical.close(3)
        historical.trading.close()


def test_below_cost_manual_sell_waits_for_exact_cancel_finality(tmp_path, monkeypatch):
    from tests.test_lp_order_registration_contract import NO_TOKEN_ID
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch, ask='.18')
    sdk.positions = tuple(dict(p, size='25', average_price='.19') for p in sdk.positions)
    bad = _open_order('below-floor', 'SELL', price='.18', original='20')
    sdk.orders = (bad, _open_order('at-floor', 'SELL', price='.19', original='3'),
        _open_order('above-floor', 'SELL', price='.20', original='2'),
        _open_order('buy', 'BUY', price='.17', original='1'),
        _open_order('other-token', 'SELL', price='.10', original='2', token_id=NO_TOKEN_ID))
    terminal = {}
    sdk.get_order = lambda *, order_id: terminal.get(order_id) or next((o for o in sdk.orders if o.id == order_id), None)
    def cancel(*, order_ids):
        sdk.cancels.append(order_ids)
        sdk.orders = tuple(o for o in sdk.orders if o.id not in order_ids)
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    try:
        d2_tick(live)
        assert sdk.cancels == [('below-floor',)]
        assert sdk.posts == []
        for _ in range(2):
            d2_tick(live)  # ACK plus absence is NOT a terminal matched quantity
        assert sdk.posts == [] and sdk.cancels == [('below-floor',)]
        # Five shares fill while cancellation is in flight. Replacement needs
        # exact final matched quantity and a subsequent financial observation.
        terminal['below-floor'] = bad.model_copy(update={'status': 'CANCELED', 'size_matched': Decimal(5)})
        sdk.positions = tuple(dict(p, size='20') for p in sdk.positions)
        clock[0] += 1
        d2_tick(live)
        assert sdk.posts == []
        d2_tick(live)
        sell, = sdk.posts
        assert sell['side'] == 'SELL' and sell['token_id'] == TOKEN_ID
        assert sell['size'] == 15 and sell['price'] == Decimal('.19')
        assert sell['post_only'] is True and sell['order_type'] == 'GTC'
        assert sdk.cancels == [('below-floor',)] and sdk.market_orders == []
        assert {o.id for o in sdk.orders} >= {'at-floor', 'above-floor', 'buy', 'other-token'}
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('cost', ['.995', None])
def test_unquotable_cost_still_cancels_below_cost_manual_sell(tmp_path, monkeypatch, cost):
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch, ask='.99')
    sdk.positions = tuple(dict(p, average_price=cost) for p in sdk.positions)
    old = _open_order('unquotable-below-floor', 'SELL', price='.99', original='10')
    sdk.orders = (old,)
    terminal = {}
    sdk.get_order = lambda *, order_id: terminal.get(order_id)
    def cancel(*, order_ids):
        sdk.cancels.append(order_ids)
        sdk.orders = tuple(o for o in sdk.orders if o.id not in order_ids)
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    try:
        d2_tick(live)
        if cost is None:
            assert sdk.cancels == sdk.posts == []
            row, = live.holdings_summary(now=live._utc_now())['items']
            assert row['state'] == 'cost_unknown' and row['cost_floor'] is None
            return
        assert sdk.cancels == [(old.id,)] and sdk.posts == []
        for _ in range(2):
            d2_tick(live)
        assert sdk.cancels == [(old.id,)] and sdk.posts == []  # no ACK/absence replay
        terminal[old.id] = old.model_copy(update={'status': 'CANCELED'})
        clock[0] += 1
        d2_tick(live)
        assert sdk.posts == []
        d2_tick(live)
        row, = live.holdings_summary(now=live._utc_now())['items']
        assert row['state'] == 'cost_above_maximum' and row['reason']
        assert sdk.cancels == [(old.id,)] and sdk.posts == sdk.market_orders == []
    finally:
        assert live.close(3)
        live.trading.close()


def test_registered_holding_sell_reprices_without_claiming_manual_order(tmp_path, monkeypatch):
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch, ask='.34')
    sdk.orders = (_open_order('manual-safe', 'SELL', price='.35', original='2'),)
    terminal = {}
    sdk.get_order = lambda *, order_id: terminal.get(order_id) or next((o for o in sdk.orders if o.id == order_id), None)
    def cancel(*, order_ids):
        sdk.cancels.append(order_ids)
        sdk.orders = tuple(o for o in sdk.orders if o.id not in order_ids)
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    try:
        d2_tick(live)
        assert len(sdk.posts) == 1 and sdk.posts[0]['size'] == 8
        for ask, expected, old_id in [('.36', '.36', 'holding-1'), ('.29', '.31', 'holding-2')]:
            old = next(o for o in sdk.orders if o.id == old_id)
            public.ask = Decimal(ask)
            clock[0] += 1
            count = len(sdk.posts)
            d2_tick(live)
            assert sdk.cancels[-1] == (old_id,) and len(sdk.posts) == count
            d2_tick(live)
            assert len(sdk.posts) == count  # ACK/absence cannot unlock replacement
            if old_id == 'holding-1':
                from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
                from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
                utc_now = live._utc_now
                assert live.close(3)
                live = LPD2LiveExecution(live.store, live.trading, account_id=WALLET, venue='polymarket',
                    process_epoch='holding-restart', policy=live.policy, clock=lambda: clock[0], utc_now=utc_now)
                live.initialize(ValidatedStartAdmission('holding-restart', 'offline-permit'))
                d2_tick(live)
                assert len(sdk.posts) == count and sdk.cancels == [('holding-1',)]
            terminal[old_id] = old.model_copy(update={'status': 'CANCELED'})
            clock[0] += 1
            d2_tick(live)
            assert len(sdk.posts) == count
            d2_tick(live)
            assert len(sdk.posts) == count + 1
            assert sdk.posts[-1]['size'] == 8 and sdk.posts[-1]['price'] == Decimal(expected)
        assert sdk.cancels == [('holding-1',), ('holding-2',)]
        assert next(o for o in sdk.orders if o.id == 'manual-safe').price == Decimal('.35')
        assert sdk.market_orders == []
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('boundary', ['before_post', 'unknown_post', 'unknown_post_case_restart'])
def test_unknown_submit_and_restart_do_not_replay(tmp_path, monkeypatch, boundary):
    from tests.test_lp_instance_ownership_contract import _isolated_manual_runtime, _restart
    from tests.test_lp_d2_integration import settle_control
    execution, live, sdk, public, clock = _isolated_manual_runtime(tmp_path, monkeypatch)
    sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='10', average_price='.305'),)
    public.ask = Decimal('.29')
    posts, signs = [], []
    original = sdk.create_limit_order
    def sign(**kwargs):
        signs.append(None)
        if boundary == 'before_post':
            raise TimeoutError('offline signer outcome unavailable')
        return original(**kwargs)
    sdk.create_limit_order = sign
    def post(signed):
        intent, = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
        provenance, = live.store.lp_order_provenance(WALLET, 'polymarket')
        assert intent['request_id'] == provenance['request_id']
        assert intent['order_id'] == provenance['order_id'] and intent['state'] == 'unknown'
        posts.append(None)
        raise TimeoutError('offline post outcome unavailable')
    sdk.post_order = post
    restarted = None
    try:
        assert live.request_running(True).status == 'accepted'
        settle_control(live)
        for _ in range(3):
            d2_tick(live)
        intent, = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
        assert intent['state'] == 'unknown' and len(signs) == 1
        assert len(posts) == (1 if boundary.startswith('unknown_post') else 0)
        if boundary == 'unknown_post_case_restart':
            from dataclasses import replace
            from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
            from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
            assert live.close(3)
            wallet = WALLET[:2] + WALLET[2:].upper()
            live.trading.config = replace(live.trading.config, wallet_address=wallet)
            restarted = LPD2LiveExecution(live.store, live.trading, account_id=wallet, venue='polymarket',
                process_epoch='case-restart', policy=live.policy, utc_now=lambda: clock[0])
            restarted.initialize(ValidatedStartAdmission('case-restart', 'offline-permit'))
        else:
            restarted = _restart(live, clock)
        for _ in range(3):
            d2_tick(restarted)
        assert len(signs) == 1 and len(posts) == (1 if boundary.startswith('unknown_post') else 0)
        assert restarted.store.lp_holding_sell_orders(WALLET, 'polymarket') == (intent,)
        assert sdk.cancels == sdk.market_orders == []
    finally:
        if restarted is not None:
            assert restarted.close(3)
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('boundary', ['stale', 'positions_incomplete', 'orders_incomplete',
    'foreign_position', 'foreign_order', 'signing_changed', 'durable_write_changed'])
def test_stale_incomplete_or_foreign_account_blocks_holding_actions(tmp_path, monkeypatch, boundary):
    import sqlite3
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch)
    execution = monitored(live, tmp_path)
    live.run_once()  # establish a real complete financial frame without trading
    try:
        if boundary == 'stale':
            clock[0] += 61
        elif boundary == 'positions_incomplete':
            sdk.positions = None
            live.run_once()
        elif boundary == 'orders_incomplete':
            sdk.orders = None
            live.run_once()
        elif boundary == 'foreign_position':
            sdk.positions = tuple(dict(p, proxy_wallet='0x' + 'b' * 40) for p in sdk.positions)
            live.run_once()
        elif boundary == 'foreign_order':
            sdk.orders = (_open_order('foreign', 'SELL', price='.18', original='1').model_copy(
                update={'maker_address': '0x' + 'b' * 40}),)
            live.run_once()
        elif boundary == 'signing_changed':
            original = sdk.create_limit_order
            def sign(**kwargs):
                live.run_once()
                return original(**kwargs)
            sdk.create_limit_order = sign
        elif boundary == 'durable_write_changed':
            connect = sqlite3.connect
            class Boundary(sqlite3.Connection):
                changed = False
                def execute(self, sql, *args, **kwargs):
                    if sql.startswith('INSERT INTO lp_holding_sells') and args and '"state":"unknown"' in str(args[0]):
                        self.changed = True
                    result = super().execute(sql, *args, **kwargs)
                    if sql == 'COMMIT' and self.changed:
                        self.changed = False
                        live.run_once()
                    return result
            monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: connect(*a, **(kw | dict(factory=Boundary))))
        for handle in live.run_protection_once():
            handle.result(3)
            handle._thread.join(3)
            assert handle.done()
        assert sdk.posts == sdk.cancels == sdk.market_orders == []
        row, = execution.lp_monitor_summary(now=live._utc_now())['holdings']['items']
        assert row['reason']
        if boundary in ('signing_changed', 'durable_write_changed'):
            rejected, = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
            assert rejected['state'] == 'rejected' and rejected['reason'] == 'admission_changed'
            assert rejected['observation_id'] != live.state.snapshot().observation.observation_id
            assert row['state'] == 'pending' and row['reason'] == '等待当前挂卖准入'
        else:
            assert row['state'] == 'account_unknown'
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('mode', ['in_process', 'reopen', 'tokyo'])
def test_balance_rebound_does_not_reuse_unproven_sale_credit(tmp_path, monkeypatch, mode):
    from datetime import timedelta
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from tests.test_lp_order_registration_contract import _maker_order, _trade
    real = None
    if mode == 'tokyo':
        from tests.test_lp_instance_ownership_contract import _real_sdk_exit_runtime, _set_real_sdk_exit_fills, _observe_real_sdk_exit
        from tests.test_lp_d2_manual_bridge import entry
        execution, live, sdk, public, clock, token, posts, real = _real_sdk_exit_runtime(tmp_path, monkeypatch)
        assert execution.lp_submit_entry(entry(clock, token_id=token, quantity='10', price='.29'))['submit_status'] == 'accepted'
        _set_real_sdk_exit_fills(live, sdk, clock, token, bought='10')
        _observe_real_sdk_exit(live)
        total, remainder, filled = '10', '6', '4'
        sell, = [o for o in sdk.orders if o.side == 'SELL']
        wallet = live.account_id
        def step():
            clock[0] += timedelta(seconds=1)
            d2_tick(live)
    else:
        live, sdk, public, clock, _ = mixed_holding(tmp_path, monkeypatch, size='100', average='.30')
        posts = sdk.posts
        token, wallet = TOKEN_ID, WALLET
        total, remainder, filled = '100', '60', '40'
        d2_tick(live)
        d2_tick(live)
        sell, = active_sells(sdk).values()
        def step():
            clock[0] += 1
            d2_tick(live)
    try:
        before = len(posts)
        for quantity in (remainder, total):
            sdk.positions = tuple(dict(p, size=quantity) for p in sdk.positions)
            step()
        if mode == 'reopen':
            assert live.close(3)
            previous = live
            live = LPD2LiveExecution(previous.store, previous.trading, account_id=wallet, venue='polymarket',
                process_epoch='rebound-reopen', policy=previous.policy, clock=lambda: clock[0], utc_now=previous._utc_now)
            live.initialize(ValidatedStartAdmission('rebound-reopen', 'offline-permit'))
        sdk.orders = tuple(o.model_copy(update={'size_matched': Decimal(filled)}) if o.id == sell.id else o for o in sdk.orders)
        maker = _maker_order(sell.id, 'SELL', filled, str(sell.price)).model_copy(update={
            'token_id': token, 'maker_address': wallet, 'owner': wallet})
        sdk.trades += (_trade('rebound-sale', maker, size=filled).model_copy(update={
            'token_id': token, 'maker_address': wallet, 'owner': wallet,
            'matched_at': live._utc_now(), 'updated_at': live._utc_now()}),)
        for _ in range(3):
            step()
        assert len(posts) == before, 'a reversed unproved balance decrease is not sale credit or imported inventory'
        row, = live.holdings_summary(now=live._utc_now())['items']
        assert row['state'] == 'position_unreconciled'
        assert sdk.cancels == []
        sdk.positions = tuple(dict(p, size=remainder) for p in sdk.positions)
        for _ in range(3):
            step()
        assert len(posts) == before and sdk.cancels == []
        assert sum(o.original_size-o.size_matched for o in active_sells(sdk).values()) == Decimal(remainder)
    finally:
        assert live.close(3)
        live.trading.close()
        if real is not None:
            real.close()


@pytest.mark.parametrize('mode', ['present_zero', 'absent_zero', 'reopen', 'missing', 'foreign', 'conflicting', 'incomplete', 'stale'])
def test_natural_holding_fill_retires_only_after_exact_convergence(tmp_path, monkeypatch, mode):
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch)
    reads = []
    try:
        d2_tick(live)
        d2_tick(live)
        old, = sdk.orders
        request, = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
        exact = old.model_copy(update={'status': 'FILLED', 'size_matched': Decimal(10)})
        answer = [None if mode == 'missing' else exact.model_copy(update={'token_id': 'foreign-token'})
            if mode == 'foreign' else exact.model_copy(update={'original_size': Decimal(11)}) if mode == 'conflicting' else exact]
        def get_order(*, order_id):
            reads.append(order_id)
            return answer[0]
        sdk.get_order = get_order
        sdk.orders = ()
        if mode == 'reopen':
            assert live.close(3)
            previous = live
            live = LPD2LiveExecution(previous.store, previous.trading, account_id=WALLET, venue='polymarket',
                process_epoch='natural-fill-reopen', policy=previous.policy, clock=lambda: clock[0], utc_now=previous._utc_now)
            live.initialize(ValidatedStartAdmission('natural-fill-reopen', 'offline-permit'))
        # Exact terminal evidence alone cannot release the old inventory.
        for _ in range(2):
            d2_tick(live)
        assert reads and set(reads) == {old.id}
        row, = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
        assert row['state'] != 'terminal' and len(sdk.posts) == 1
        if mode in ('missing', 'foreign', 'conflicting'):
            sdk.positions = ()
            d2_tick(live)
            assert live.store.lp_holding_sell_orders(WALLET, 'polymarket')[0]['state'] != 'terminal'
            assert len(sdk.posts) == 1 and sdk.cancels == []
            answer[0] = exact
            clock[0] += 61
        if mode == 'incomplete':
            sdk.positions = None
            d2_tick(live)
            assert live.store.lp_holding_sell_orders(WALLET, 'polymarket')[0]['state'] != 'terminal'
        if mode == 'stale':
            clock[0] += 300
            for handle in live.run_protection_once():
                handle.result(3); handle._thread.join(3)
            assert live.store.lp_holding_sell_orders(WALLET, 'polymarket')[0]['state'] != 'terminal'
        sdk.positions = () if mode == 'absent_zero' else (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='0', average_price='.305'),)
        for _ in range(3):
            d2_tick(live)
        row, = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
        assert row['request_id'] == request['request_id'] and row['state'] == 'terminal'
        assert len(sdk.posts) == 1 and sdk.cancels == []
        if mode == 'absent_zero':
            assert live.state.snapshot().positions == ()
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='2', average_price='.305'),)
        for _ in range(3):
            d2_tick(live)
        assert len(sdk.posts) == 2 and sdk.posts[-1]['size'] == 2
        assert sdk.posts[-1]['price'] == Decimal('.31') and sdk.market_orders == sdk.cancels == []
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('other_fill', [False, True])
def test_concurrent_holding_cancels_reconcile_all_token_fills(tmp_path, monkeypatch, other_fill):
    from threading import Event, Lock
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch, ask='.18')
    sdk.positions = tuple(dict(p, average_price='.19') for p in sdk.positions)
    orders = [_open_order(name, 'SELL', price='.18', original='5') for name in ('cancel-A', 'cancel-B')]
    peer = _open_order('sufficient-other', 'SELL', price='.20', original='1')
    sdk.orders = tuple(orders + ([peer] if other_fill else []))
    terminal, entered, release, lock = {}, [Event(), Event()], Event(), Lock()
    sdk.get_order = lambda *, order_id: terminal.get(order_id)
    def cancel(*, order_ids):
        with lock:
            index = len(sdk.cancels)
            sdk.cancels.append(order_ids)
        entered[index].set()
        assert release.wait(3), 'overlapping cancel SDK release watchdog'
        with lock:
            sdk.orders = tuple(o for o in sdk.orders if o.id not in order_ids)
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    handles = []
    try:
        live.run_once()
        handles = live.run_protection_once()
        assert all(event.wait(3) for event in entered)
        assert len(handles) == 2 and sdk.posts == []
        terminal.update({o.id: o.model_copy(update={'status': 'CANCELED', 'size_matched': Decimal(2)}) for o in orders})
        if other_fill:
            sdk.orders = tuple(o.model_copy(update={'status': 'FILLED', 'size_matched': Decimal(1)}) if o.id == peer.id else o for o in sdk.orders)
        release.set()
        for handle in handles:
            handle.result(3); handle._thread.join(3)
            assert handle.done()
        for quantity in ('10', '8'):
            sdk.positions = tuple(dict(p, size=quantity) for p in sdk.positions)
            for _ in range(2):
                d2_tick(live)
            assert sdk.posts == []
            cancels = [r for r in live.store.lp_holding_sell_orders(WALLET, 'polymarket') if r.get('kind') == 'cancel']
            assert len(cancels) == 2 and all(r['state'] != 'terminal' for r in cancels)
        expected = 5 if other_fill else 6
        sdk.positions = tuple(dict(p, size=str(expected)) for p in sdk.positions)
        for _ in range(4):
            d2_tick(live)
        sell, = sdk.posts
        assert sell['side'] == 'SELL' and sell['size'] == expected and sell['price'] == Decimal('.19')
        assert {c for batch in sdk.cancels for c in batch} == {'cancel-A', 'cancel-B'} and len(sdk.cancels) == 2
        cancels = [r for r in live.store.lp_holding_sell_orders(WALLET, 'polymarket') if r.get('kind') == 'cancel']
        assert all(r['state'] == 'terminal' for r in cancels) and sdk.market_orders == []
    finally:
        release.set()
        for handle in handles:
            handle._thread.join(3)
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('action', ['SELL', 'SELL_CANCEL'])
def test_holding_permission_sql_does_not_hold_current_state(tmp_path, monkeypatch, action):
    import sqlite3
    from threading import Event, Thread
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch)
    if action == 'SELL_CANCEL':
        sdk.orders = (_open_order('permission-cancel', 'SELL', price='.18', original='10'),)
    execution = monitored(live, tmp_path)
    live.run_once()
    entered, release, progressed = Event(), Event(), Event()
    connect, queries, handles, errors = sqlite3.connect, [], [], []
    class PermissionRead(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith('SELECT * FROM incidents WHERE acknowledged_at IS NULL'):
                queries.append(sql)
                if len(queries) == 3:
                    entered.set()
                    assert release.wait(5), 'permission SQL release watchdog'
            return super().execute(sql, *args, **kwargs)
    def dispatch():
        try:
            handles.extend(live.run_protection_once())
        except BaseException as exc:
            errors.append(exc)
    def probe():
        live.state.snapshot()
        live.request_running(False)
        progressed.set()
    worker, observer = Thread(target=dispatch), Thread(target=probe)
    try:
        monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: connect(*a, **(k | dict(factory=PermissionRead))))
        worker.start()
        assert entered.wait(3), 'real SQL permission read watchdog'
        observer.start()
        assert progressed.wait(3), 'current snapshot/control blocked behind permission SQL'
        row = live.store.create_recovery_execution(dict(reason='offline permission revoked'), idempotency_key=action)
        live.store.open_incident(row['execution_id'], dict(reason='offline permission revoked'))
        release.set()
        worker.join(3); observer.join(3)
        assert not worker.is_alive() and not observer.is_alive() and errors == []
        for handle in handles:
            handle.result(3); handle._thread.join(3)
            assert handle.done()
        assert sdk.posts == sdk.cancels == sdk.market_orders == []
        assert live.status()['native_inflight'] == 0
        assert live.store.unacknowledged_incident() is not None
    finally:
        release.set()
        if worker.ident: worker.join(3)
        if observer.ident: observer.join(3)
        for handle in handles: handle._thread.join(3)
        assert live.close(3)
        live.trading.close()


def test_blocked_inventory_persistence_preserves_current_reads_and_denies_uncommitted_send(tmp_path, monkeypatch):
    from threading import Event, Thread
    from tests.test_lp_business_writer_contract import blocked_database, wait_status
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch)
    blocker = None
    handles, errors = [], []
    read_done = Event()
    def read():
        try:
            live.run_once()
        except BaseException as exc:
            errors.append(exc)
        finally:
            read_done.set()
    def protection():
        try:
            handles.extend(live.run_protection_once())
        except BaseException as exc:
            errors.append(exc)
    reader, dispatcher = Thread(target=read), Thread(target=protection)
    try:
        live.run_once()
        wait_status(live.writer, lambda state: state['pending_entries'] == state['inflight_entries'] == 0)
        blocker, entered = blocked_database(live.store, monkeypatch)
        sdk.positions = tuple(dict(p, size='12') for p in sdk.positions)
        reader.start()
        assert read_done.wait(3), 'current account reads must finish independently of ordinary business SQL'
        assert errors == [] and live.state.snapshot().positions[0].quantity.value == 12
        dispatcher.start()
        assert entered.wait(3), 'reliable mutation intent / inventory SQL watchdog'
        # The database is still locked: a queued or uncommitted reservation
        # cannot authorize a native send, and control/snapshots remain usable.
        live.state.snapshot()
        live.request_running(False)
        assert sdk.posts == sdk.cancels == []
        assert live.close(0) is False
        blocker.execute('ROLLBACK'); blocker.close(); blocker = None
        dispatcher.join(3); reader.join(3)
        assert not dispatcher.is_alive() and not reader.is_alive() and errors == []
        for handle in handles:
            handle.result(3); handle._thread.join(3)
            assert handle.done()
        assert sdk.posts == sdk.cancels == sdk.market_orders == []
    finally:
        if blocker is not None:
            if blocker.in_transaction: blocker.execute('ROLLBACK')
            blocker.close()
        if reader.ident: reader.join(3)
        if dispatcher.ident: dispatcher.join(3)
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize(('reopen', 'buy_baseline', 'buy_final'), [(False, '0', '1'), (True, '2', '3')])
def test_holding_cancel_convergence_includes_proven_concurrent_buy(tmp_path, monkeypatch, reopen, buy_baseline, buy_final):
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch, ask='.18')
    sdk.positions = tuple(dict(p, average_price='.19') for p in sdk.positions)
    sells = [_open_order(name, 'SELL', price='.18', original='5') for name in ('with-buy-A', 'with-buy-B')]
    buy = _open_order('concurrent-buy', 'BUY', price='.18', original='5', matched=buy_baseline)
    sdk.orders = (*sells, buy)
    terminals, reads = {}, []
    def exact(*, order_id):
        reads.append(order_id)
        return terminals.get(order_id)
    sdk.get_order = exact
    def cancel(*, order_ids):
        sdk.cancels.append(order_ids)
        sdk.orders = tuple(o for o in sdk.orders if o.id not in order_ids)
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    try:
        d2_tick(live)
        assert len(sdk.cancels) == 2 and {x for batch in sdk.cancels for x in batch} == {s.id for s in sells}
        assert sdk.posts == []
        # Reopen with the saved cancellation baseline while final quantities
        # remain unknown. Neither missing BUY nor missing SELL means matched0.
        sdk.orders = ()
        if reopen:
            assert live.close(3)
            previous = live
            live = LPD2LiveExecution(previous.store, previous.trading, account_id=WALLET, venue='polymarket',
                process_epoch='concurrent-buy-reopen', policy=previous.policy, clock=previous._clock, utc_now=previous._utc_now)
            live.initialize(ValidatedStartAdmission('concurrent-buy-reopen', 'offline-permit'))
        terminals.update({s.id: s.model_copy(update={'status': 'CANCELED', 'size_matched': Decimal(2)}) for s in sells})
        for quantity in ('10', '8', '6'):
            sdk.positions = tuple(dict(p, size=quantity) for p in sdk.positions)
            for _ in range(3):
                d2_tick(live)
            assert sdk.posts == [], 'stale balances cannot replace a missing exact BUY quantity'
            assert all(r['state'] != 'terminal' for r in live.store.lp_holding_sell_orders(WALLET, 'polymarket')
                if r.get('kind') == 'cancel')
        terminals[buy.id] = buy.model_copy(update={'status': 'CANCELED', 'size_matched': Decimal(buy_final)})
        for _ in range(2):
            d2_tick(live)  # exact BUY+1 with stale account6 still grants no order
        assert sdk.posts == []
        sdk.positions = tuple(dict(p, size='7') for p in sdk.positions)
        for _ in range(4):
            d2_tick(live)
        sell, = sdk.posts
        assert sell['side'] == 'SELL' and sell['size'] == 7 and sell['price'] == Decimal('.19')
        assert sell['post_only'] is True and sell['order_type'] == 'GTC'
        assert buy.id in reads
        rows = [r for r in live.store.lp_holding_sell_orders(WALLET, 'polymarket') if r.get('kind') == 'cancel']
        assert len(rows) == 2 and all(r['state'] == 'terminal' for r in rows)
        assert len(sdk.cancels) == 2 and all(buy.id not in batch for batch in sdk.cancels)
        assert sdk.market_orders == []
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('mode', ['complete_absent', 'missing', 'stale'])
def test_fully_filled_holding_cancels_accept_complete_absent_zero(tmp_path, monkeypatch, mode):
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch, ask='.18')
    sdk.positions = tuple(dict(p, average_price='.19') for p in sdk.positions)
    orders = [_open_order(name, 'SELL', price='.18', original='5') for name in ('full-cancel-A', 'full-cancel-B')]
    sdk.orders = tuple(orders)
    terminals = {}
    sdk.get_order = lambda *, order_id: terminals.get(order_id)
    def cancel(*, order_ids):
        sdk.cancels.append(order_ids)
        sdk.orders = tuple(o for o in sdk.orders if o.id not in order_ids)
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    try:
        d2_tick(live)
        assert len(sdk.cancels) == 2 and sdk.posts == []
        # Both native cancels have ended, but ACK/absence cannot stand in for
        # the exact FILLED5 quantities of their distinct original orders.
        d2_tick(live)
        assert all(r['state'] != 'terminal' for r in live.store.lp_holding_sell_orders(WALLET, 'polymarket'))
        terminals.update({o.id: o.model_copy(update={'status': 'FILLED', 'size_matched': Decimal(5)}) for o in orders})
        for _ in range(2):
            d2_tick(live)  # exact full fills with stale account10 cannot retire
        assert sdk.posts == []
        assert all(r['state'] != 'terminal' for r in live.store.lp_holding_sell_orders(WALLET, 'polymarket'))
        if mode == 'missing':
            sdk.positions = None  # missing/failed source is not complete empty coverage
            for _ in range(2):
                d2_tick(live)
            assert not live.state.snapshot().observation.positions_complete
            assert all(r['state'] != 'terminal' for r in live.store.lp_holding_sell_orders(WALLET, 'polymarket'))
        if mode == 'stale':
            sdk.positions = ()
            live.run_once()  # complete absent-zero exists but has not authorized retirement
            assert live.state.snapshot().positions == ()
            assert live.state.snapshot().observation.positions_complete
            clock[0] += 300
            for handle in live.run_protection_once():
                handle.result(3); handle._thread.join(3)
            assert all(r['state'] != 'terminal' for r in live.store.lp_holding_sell_orders(WALLET, 'polymarket'))
        sdk.positions = ()  # fresh COMPLETE threshold0 absence proves zero
        for _ in range(3):
            d2_tick(live)
        rows = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
        assert len(rows) == 2 and all(r.get('kind') == 'cancel' and r['state'] == 'terminal' for r in rows)
        assert live.state.snapshot().positions == () and sdk.posts == []
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='2', average_price='.19'),)
        for _ in range(3):
            d2_tick(live)
        sell, = sdk.posts
        assert sell['side'] == 'SELL' and sell['size'] == 2 and sell['price'] == Decimal('.19')
        assert sell['post_only'] is True and sell['order_type'] == 'GTC'
        assert {x for batch in sdk.cancels for x in batch} == {o.id for o in orders} and len(sdk.cancels) == 2
        assert sdk.market_orders == []
    finally:
        assert live.close(3)
        live.trading.close()


def test_balance_rebound_with_proven_buy_does_not_reuse_sale_credit(tmp_path, monkeypatch):
    from datetime import timedelta
    from tests.test_lp_instance_ownership_contract import _real_sdk_exit_runtime, _set_real_sdk_exit_fills, _observe_real_sdk_exit
    from tests.test_lp_d2_manual_bridge import entry
    from tests.test_lp_d2_integration import settle_control
    from tests.test_lp_order_registration_contract import _maker_order, _trade
    execution, live, sdk, public, clock, token, posts, real = _real_sdk_exit_runtime(tmp_path, monkeypatch)
    def step():
        clock[0] += timedelta(seconds=1)
        d2_tick(live)
    def trade(name, order_id, side, quantity, price):
        maker = _maker_order(order_id, side, quantity, price).model_copy(update={
            'token_id': token, 'maker_address': live.account_id, 'owner': live.account_id})
        return _trade(name, maker, size=quantity).model_copy(update={'token_id': token,
            'maker_address': live.account_id, 'owner': live.account_id,
            'matched_at': clock[0], 'updated_at': clock[0]})
    try:
        assert execution.lp_submit_entry(entry(clock, token_id=token, quantity='10'))['submit_status'] == 'accepted'
        _set_real_sdk_exit_fills(live, sdk, clock, token, bought='10')
        _observe_real_sdk_exit(live)
        original, = [o for o in sdk.orders if o.side == 'SELL']
        live.request_running(True); settle_control(live)
        sdk.positions = tuple(dict(p, size='6') for p in sdk.positions)
        step()  # a balance drop alone cannot prove the pending SELL4
        prepared = live.trading.lp_prepare_limit_order(token_id=token, side='BUY', price=Decimal('.29'),
            quantity=Decimal(1), post_only=True, expiration=None)
        live.store.lp_register_order_provenance(prepared.provenance(instance_id='tokyo',
            request_id='independent-proved-buy', condition_id=CONDITION_ID, venue='polymarket'))
        live.trading.lp_post_order(prepared.signed_order)  # real signing/provenance, offline transport
        sdk.orders = tuple(o.model_copy(update={'status': 'FILLED', 'size_matched': Decimal(1)})
            if o.id == prepared.order_id else o for o in sdk.orders)
        sdk.positions = tuple(dict(p, size='11') for p in sdk.positions)
        sdk.trades += (trade('independent-buy-one', prepared.order_id, 'BUY', '1', '.29'),)
        for _ in range(3): step()
        assert sorted(o.original_size for o in sdk.orders if o.side == 'SELL') == [1, 10]
        sdk.orders = tuple(o.model_copy(update={'size_matched': Decimal(4)}) if o.id == original.id else o for o in sdk.orders)
        sdk.trades += (trade('sale-after-proved-buy', original.id, 'SELL', '4', str(original.price)),)
        for _ in range(3): step()
        assert [p['quantity'] for p in posts if p['side'] == 'SELL'] == [10, 1]
        row, = live.holdings_summary(now=live._utc_now())['items']
        assert row['state'] == 'position_unreconciled'
        sdk.positions = tuple(dict(p, size='7') for p in sdk.positions)
        for _ in range(3): step()
        assert sum(o.original_size-o.size_matched for o in sdk.orders if o.side == 'SELL') == 7
        assert [p['quantity'] for p in posts if p['side'] == 'SELL'] == [10, 1]
        assert sdk.cancels == [] and live.status()['native_inflight'] == 0
    finally:
        assert live.close(3)
        live.trading.close(); real.close()


@pytest.mark.parametrize('mode', ['valid', 'reopen', 'missing', 'foreign', 'conflicting'])
def test_cancel_convergence_reads_disappeared_safe_sell(tmp_path, monkeypatch, mode):
    from threading import Event, Lock
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch, ask='.18')
    sdk.positions = tuple(dict(p, average_price='.19') for p in sdk.positions)
    low = [_open_order(name, 'SELL', price='.18', original='5') for name in ('peer-fill-A', 'peer-fill-B')]
    peer = _open_order('safe-disappeared-peer', 'SELL', price='.20', original='1')
    sdk.orders = (*low, peer)
    final_peer = peer.model_copy(update={'status': 'FILLED', 'size_matched': Decimal(1)})
    answers, reads = {}, []
    entered, release, lock, handles = [Event(), Event()], Event(), Lock(), []
    def exact(*, order_id):
        reads.append(order_id)
        return answers.get(order_id)
    sdk.get_order = exact
    def cancel(*, order_ids):
        with lock:
            index = len(sdk.cancels); sdk.cancels.append(order_ids)
        entered[index].set()
        assert release.wait(3), 'overlapping exact cancel completion watchdog'
        with lock:
            sdk.orders = tuple(o for o in sdk.orders if o.id not in order_ids)
        return dict(canceled=order_ids, not_canceled={})
    sdk.cancel_orders = cancel
    try:
        live.run_once(); handles = live.run_protection_once()
        assert len(handles) == 2 and all(e.wait(3) for e in entered)
        release.set()
        for handle in handles:
            handle.result(3); handle._thread.join(3)
            assert handle.done()
        sdk.orders = ()  # all three now truly disappear from the open-order API
        if mode == 'reopen':
            assert live.close(3)
            old = live
            live = LPD2LiveExecution(old.store, old.trading, account_id=WALLET, venue='polymarket',
                process_epoch='safe-peer-reopen', policy=old.policy, clock=old._clock, utc_now=old._utc_now)
            live.initialize(ValidatedStartAdmission('safe-peer-reopen', 'offline-permit'))
        answers.update({o.id: o.model_copy(update={'status': 'CANCELED', 'size_matched': Decimal(2)}) for o in low})
        answers[peer.id] = (None if mode == 'missing' else final_peer.model_copy(update={'token_id': 'foreign-token'})
            if mode == 'foreign' else final_peer.model_copy(update={'original_size': Decimal(2)})
            if mode == 'conflicting' else final_peer)
        for quantity in ('10', '8', '6'):
            sdk.positions = tuple(dict(p, size=quantity) for p in sdk.positions)
            for _ in range(3): d2_tick(live)
            assert sdk.posts == [], 'a disappeared safe SELL cannot be treated as matched0'
        if mode in ('missing', 'foreign', 'conflicting'):
            sdk.positions = tuple(dict(p, size='5') for p in sdk.positions)
            for _ in range(2): d2_tick(live)
            assert sdk.posts == []
            assert all(r['state'] != 'terminal' for r in live.store.lp_holding_sell_orders(WALLET, 'polymarket'))
            answers[peer.id] = final_peer
            clock[0] += 61  # existing exact-read backoff is preserved
        sdk.positions = tuple(dict(p, size='5') for p in sdk.positions)
        for _ in range(4): d2_tick(live)
        sell, = sdk.posts
        assert sell['size'] == 5 and sell['price'] == Decimal('.19') and sell['post_only'] is True
        assert peer.id in reads
        assert len(sdk.cancels) == 2 and {x for batch in sdk.cancels for x in batch} == {o.id for o in low}
        rows = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
        assert all(r.get('order_id') != peer.id for r in rows)
        assert len([r for r in rows if r.get('kind') == 'cancel' and r['state'] == 'terminal']) == 2
        assert sdk.market_orders == []
    finally:
        release.set()
        for handle in handles: handle._thread.join(3)
        assert live.close(3)
        live.trading.close()


def test_startup_legacy_obligation_read_failure_is_visible_and_fail_closed(tmp_path, monkeypatch):
    import sqlite3
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    live, sdk, _, _ = imported_holding(tmp_path, monkeypatch)
    live.store.lp_create_session('prior-unknown', 'prior-unknown', state='passive_exit',
        payload=dict(account_id=WALLET, condition_id=CONDITION_ID, token_id=TOKEN_ID,
            passive_exit_attempt_key='unresolved', passive_exit_attempt_state='unknown'))
    assert live.close(3)
    connect, reads = sqlite3.connect, []
    class UnavailableObligations(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith('SELECT DISTINCT') and 'FROM lp_sessions' in sql:
                reads.append(sql)
                raise sqlite3.OperationalError('offline obligation identity read unavailable')
            return super().execute(sql, *args, **kwargs)
    with monkeypatch.context() as unavailable:
        unavailable.setattr(sqlite3, 'connect', lambda *a, **kw: connect(*a, **(kw | dict(factory=UnavailableObligations))))
        previous = live
        live = LPD2LiveExecution(previous.store, previous.trading, account_id=WALLET, venue='polymarket',
            process_epoch='failed-obligation-read', policy=previous.policy,
            clock=previous._clock, utc_now=previous._utc_now)
        live.initialize(ValidatedStartAdmission('failed-obligation-read', 'offline-permit'))
    try:
        assert len(reads) == 1
        for _ in range(3):
            d2_tick(live)
        row, = live.holdings_summary(now=live._utc_now())['items']
        assert row['quantity'] == '10' and row['uncovered_quantity'] == '10'
        assert row['state'] == 'legacy_obligation_unknown'
        assert '历史卖单义务未确认' in row['reason']
        assert sdk.posts == sdk.cancels == sdk.market_orders == []
        assert live.store.lp_session('prior-unknown')['passive_exit_attempt_state'] == 'unknown'
    finally:
        assert live.close(3)
        live.trading.close()


@pytest.mark.parametrize('case', ['peer', 'unknown_origin', 'stopped', 'unknown_cost',
    'foreign_wallet', 'foreign_token', 'safe_peer_sell', 'peer_buy'])
def test_running_price_correction_rehangs_nonlocal_below_floor_sell(tmp_path, monkeypatch, case):
    from datetime import timedelta
    from tests.test_lp_instance_ownership_contract import (
        _isolated_manual_runtime, _register_local, _order_from_provenance)
    from tests.test_lp_d2_integration import settle_control
    from tests.test_lp_order_registration_contract import NO_TOKEN_ID
    owners, completed, posts, answers, exact_reads = [], [], [], {}, []
    try:
        for instance in ('mini', 'tokyo'):
            owners.append(_isolated_manual_runtime(tmp_path/instance, monkeypatch,
                process='price-correction-'+instance, instance=instance))
        _, peer, _, _, _ = owners[0]
        _, live, sdk, public, clock = owners[1]
        side = 'BUY' if case == 'peer_buy' else 'SELL'
        price = '.20' if case == 'safe_peer_sell' else '.18'
        if case == 'unknown_origin':
            prepared = peer.trading.lp_prepare_limit_order(token_id=TOKEN_ID, side=side,
                quantity=Decimal(10), price=Decimal(price), post_only=True, expiration=None)
            row = prepared.provenance(instance_id='mini', request_id='unknown-origin', condition_id=CONDITION_ID)
        else:
            row = _register_local(peer, side=side, quantity='10', price=price, request_id='peer-price')
        target = _order_from_provenance(row)
        if case == 'foreign_wallet':
            target = target.model_copy(update={'maker_address': '0x'+'b'*40})
        elif case == 'foreign_token':
            target = target.model_copy(update={'token_id': NO_TOKEN_ID})
        sdk.orders = (target,)
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID,
            size='10', average_price=None if case == 'unknown_cost' else '.19'),)
        assert peer.store.path != live.store.path
        assert live.store.lp_order_provenance(WALLET, 'polymarket') == ()
        peer_provenance = peer.store.lp_order_provenance(WALLET, 'polymarket')
        def cancel(*, order_ids):
            sdk.cancels.append(order_ids)
            sdk.orders = tuple(o for o in sdk.orders if o.id not in order_ids)
            return dict(canceled=order_ids, not_canceled={})  # ACK, not final quantity proof
        def exact(*, order_id):
            exact_reads.append(order_id)
            return answers.get(order_id)
        def post(signed):
            registered, = [r for r in live.store.lp_order_provenance(WALLET, 'polymarket')
                if str(r['protocol_fields']['salt']) == str(signed.salt)]
            posts.append(dict(side=signed.side, quantity=Decimal(registered['quantity']),
                price=Decimal(registered['price']), order_type=signed.order_type,
                post_only=signed.post_only, order_id=registered['order_id']))
            sdk.orders += (_order_from_provenance(registered),)
            return dict(accepted=True, order_id=registered['order_id'], status='LIVE', making_amount='0')
        sdk.cancel_orders, sdk.get_order, sdk.post_order = cancel, exact, post
        if case != 'stopped':
            live.request_running(True)
            assert settle_control(live).current.effective_running is True
        def step():
            clock[0] += timedelta(seconds=1)
            live.run_once()
            handles = live.run_protection_once()
            for handle in handles:
                outcome = handle.result(3)
                handle._thread.join(3)
                assert handle.done()
                completed.append((handle, outcome))
            return handles
        initial = step()
        if case in ('peer', 'unknown_origin'):
            cancel_handle, = initial
            assert cancel_handle.identity.kind == 'cancel_exit'
            assert completed[-1][1].status == 'accepted' and completed[-1][1].order_id == target.id
            assert sdk.cancels == [(target.id,)] and posts == []
            for _ in range(2): step()
            assert target.id in exact_reads and posts == []
            cancel_row, = live.store.lp_holding_sell_orders(WALLET, 'polymarket')
            assert cancel_row['request_id'] == cancel_handle.identity.request_id
            assert cancel_row['state'] == 'cancel_unknown'
            answers[target.id] = target.model_copy(update={'status': 'CANCELED', 'size_matched': Decimal(0)})
            step()  # this exact final receipt still cannot replace on the same observation
            assert posts == []
            for _ in range(3): step()  # genuinely fresh complete position10
            replacement, = posts
            assert replacement['side'] == 'SELL' and replacement['quantity'] == 10
            assert replacement['price'] == public.ask == Decimal('.32')
            assert replacement['price'] >= Decimal('.19') and replacement['post_only'] is True
            assert replacement['order_type'] == 'GTC'
            assert sdk.cancels == [(target.id,)]
            assert sum(o.original_size-o.size_matched for o in sdk.orders if o.side == 'SELL') == 10
            final_cancel, = [r for r in live.store.lp_holding_sell_orders(WALLET, 'polymarket')
                if r['request_id'] == cancel_handle.identity.request_id]
            assert final_cancel['state'] == 'terminal'
        else:
            for _ in range(2): step()
            assert sdk.cancels == []
            if case in ('stopped', 'unknown_cost', 'foreign_wallet', 'safe_peer_sell'):
                assert posts == []
            else:
                # Ordinary uncovered local inventory can quote; it grants no
                # generic cancel authority for a peer BUY/other-token SELL.
                assert all(p['side'] == 'SELL' and p['post_only'] and p['price'] >= Decimal('.19') for p in posts)
                assert sum(p['quantity'] for p in posts) <= 10
            assert not any(r.get('kind') == 'cancel' for r in live.store.lp_holding_sell_orders(WALLET, 'polymarket'))
            if case in ('safe_peer_sell', 'peer_buy'):
                generic = live.manual_cancel(order_ids=[target.id])
                assert generic['canceled'] == []
                assert generic['skipped'] == [dict(order_id=target.id, reason='instance_ownership_unknown')]
                assert sdk.cancels == []
        assert all(r['order_id'] != target.id for r in live.store.lp_order_provenance(WALLET, 'polymarket'))
        assert peer.store.lp_order_provenance(WALLET, 'polymarket') == peer_provenance
        assert sdk.market_orders == [] and live.status()['native_inflight'] == 0
    finally:
        for handle, _ in completed: handle._thread.join(3)
        for _, runtime, _, _, _ in owners:
            assert runtime.close(3)
            runtime.trading.close()
