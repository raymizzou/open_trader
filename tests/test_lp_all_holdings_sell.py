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
        assert row['state'] in ('account_unknown', 'admission_changed')
    finally:
        assert live.close(3)
        live.trading.close()
