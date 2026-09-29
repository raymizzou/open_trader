"""Shared LP facts must recover without weakening the automatic funds fence."""

import pytest

from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

from tests.test_lp_auto_pool import setup


def test_reward_refresh_during_account_read_does_not_invalidate_funds(tmp_path):
    execution, exchange, lp, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd="100", target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    session_id = execution.lp_auto_run_once()["intents"][0]["session_id"]
    read = exchange.lp_snapshot
    exchange.lp_reward_snapshot = lambda *args: {}

    def snapshot(request):
        result = read(request)
        lp.refresh_rewards(session_id)
        return result

    exchange.lp_snapshot = snapshot
    result = execution.lp_auto_reconcile_unknown()

    assert result["funds"]["status"] == "known"
    assert Decimal(result["funds"]["buy_reserved_usd"]) == 8
    assert Decimal(result["funds"]["available_usd"]) == 92
    assert len(exchange.posts) == 1


@pytest.mark.parametrize("auto_first", [False, True])
def test_tick_and_auto_share_one_inflight_venue_read(tmp_path, auto_first):
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd="100", target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    entered, release, duplicate = Event(), Event(), Event()
    read = exchange.lp_snapshot

    def delayed(request):
        if entered.is_set():
            duplicate.set()
        entered.set()
        assert release.wait(5)
        return read(request)

    exchange.lp_snapshot = delayed
    scored = []
    exchange.get_order_scoring = lambda order_id: scored.append(order_id) or True
    first, second = (execution.lp_auto_reconcile_unknown, execution.lp_tick) if auto_first else (execution.lp_tick, execution.lp_auto_reconcile_unknown)
    with ThreadPoolExecutor(2) as workers:
        tick = workers.submit(first)
        try:
            assert entered.wait(2)
            automatic = workers.submit(second)
            assert not duplicate.wait(.2), "two independent account reads raced"
        finally:
            release.set()
        tick.result(timeout=5)
        automatic.result(timeout=5)
    assert execution.lp_auto_state()["funds"]["status"] == "known"
    assert len(exchange.posts) == 1
    assert scored == ["o1"], "joining tick must still apply its monitoring work"


def test_manual_cancel_invalidates_funds_before_network_and_fences_old_read(tmp_path):
    execution, exchange, lp, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    entered, release = Event(), Event()
    read = exchange.lp_snapshot

    def delayed(request):
        from copy import deepcopy
        result = deepcopy(read(request))
        entered.set()
        assert release.wait(5)
        return result

    def cancel(ids):
        state = execution.lp_auto_state()
        assert state['funds']['status'] == 'unknown'
        assert Decimal(state['funds']['buy_reserved_usd']) == 8
        return dict(canceled=list(ids), not_canceled={})

    exchange.lp_snapshot = delayed
    exchange.cancel_orders_detailed = cancel
    with ThreadPoolExecutor(1) as workers:
        reading = workers.submit(execution.lp_auto_reconcile_unknown)
        try:
            assert entered.wait(2)
            execution.lp_cancel_orders(dict(confirm=True, order_ids=['o1']))
        finally:
            release.set()
        reading.result(timeout=5)
    assert execution.lp_auto_state()['funds']['status'] == 'unknown'
    assert len(exchange.posts) == 1
    exchange.lp_snapshot = read
    exchange.orders[0]['status'] = 'CANCELED'
    execution.lp_tick()
    assert store.lp_session(sid)['state'] == 'complete'
    assert execution.lp_auto_state()['funds']['status'] == 'known'
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 0


def test_fresh_funds_survive_refresh_but_expire_and_recovery_wakes_scheduler(tmp_path, monkeypatch):
    from datetime import timedelta
    from tests import test_lp_auto_pool as venue
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler

    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    execution.lp_auto_set_desired_running(False)
    scheduler_now = venue.NOW
    scheduler = LPAutoScheduler(execution, clock=lambda: scheduler_now)
    assert scheduler.run_due()
    assert not scheduler.run_due()
    entered, release = Event(), Event()
    read = exchange.lp_snapshot

    def delayed(request):
        entered.set()
        assert release.wait(5)
        return read(request)

    exchange.lp_snapshot = delayed
    with ThreadPoolExecutor(1) as workers:
        tick = workers.submit(execution.lp_tick)
        try:
            assert entered.wait(2)
            assert execution.lp_auto_state()['funds']['status'] == 'known'
            monkeypatch.setattr(venue, 'NOW', venue.NOW + timedelta(seconds=61))
            assert execution.lp_auto_state()['funds']['status'] == 'unknown'
            exchange.orders[0]['status'] = 'CANCELED'
        finally:
            release.set()
        tick.result(timeout=5)
    assert execution.lp_auto_state()['funds']['status'] == 'known'
    assert scheduler.run_due(), 'recovery should not wait another polling interval'
    assert not scheduler.run_due(), 'one recovery produces a coalesced wake'
    assert len(exchange.posts) == 1


def test_slow_session_does_not_delay_other_session_publication(tmp_path):
    import time
    execution, exchange, _, _ = setup(tmp_path, 3)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=3))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    execution.lp_auto_set_desired_running(False)
    entered, release = Event(), Event()
    read = exchange.lp_snapshot

    def delayed(request):
        # The store returns newest first. Stall that first session.
        if request['token_id'] == 'm02':
            entered.set()
            assert release.wait(5)
        return read(request)

    exchange.lp_snapshot = delayed
    for order in exchange.orders:
        order['status'] = 'CANCELED'
    with ThreadPoolExecutor(1) as workers:
        tick = workers.submit(execution.lp_tick)
        try:
            assert entered.wait(2)
            deadline = time.monotonic() + 2
            while execution.lp_auto_state()['slots']['occupied'] > 1 and time.monotonic() < deadline:
                time.sleep(.01)
            assert execution.lp_auto_state()['slots']['occupied'] == 1
            assert not tick.done(), 'slow session is still being read'
        finally:
            release.set()
        tick.result(timeout=5)
    assert execution.lp_auto_state()['slots']['occupied'] == 0


def test_incomplete_account_cannot_release_a_canceled_order(tmp_path):
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    exchange.orders[0]['status'] = 'CANCELED'
    read = exchange.lp_snapshot

    def incomplete(request):
        snapshot = read(request)
        snapshot['account']['positions_complete'] = False
        return snapshot

    exchange.lp_snapshot = incomplete
    result = execution.lp_auto_reconcile_unknown()
    assert result['funds']['status'] == 'unknown'
    assert Decimal(result['funds']['buy_reserved_usd']) == 8
    assert result['slots']['occupied'] == 1
    exchange.lp_snapshot = read
    assert execution.lp_auto_reconcile_unknown()['funds']['status'] == 'known'


def test_slow_scoring_does_not_discard_another_sessions_fresh_funds(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from tests import test_lp_auto_pool as venue

    execution, exchange, lp, store = setup(tmp_path, 2)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=2))
    execution.lp_auto_set_desired_running(True)
    intents = execution.lp_auto_run_once()['intents']
    execution.lp_auto_set_desired_running(False)
    first, second = [store.lp_session(intent['session_id']) for intent in intents]
    entered, release = Event(), Event()

    def scoring(order_id):
        if order_id == first['entry_order_id']:
            entered.set()
            assert release.wait(5)
        return True

    exchange.get_order_scoring = scoring
    apply_lock = (execution._acquire_global_lock, execution._release_global_lock)
    with ThreadPoolExecutor(1) as workers:
        monitoring = workers.submit(lp._tick_session, (first, 0), apply_lock=apply_lock)
        try:
            assert entered.wait(2)
            monkeypatch.setattr(venue, 'NOW', venue.NOW + timedelta(seconds=61))
            result = lp.reconcile_facts(second['session_id'], apply_lock=apply_lock)
            assert result[3] is None, 'scoring must not occupy the facts apply lock'
            refreshed = next(row for row in execution.lp_auto_state()['intents']
                             if row['session_id'] == second['session_id'])
            assert refreshed['financial_status'] == 'known'
            assert datetime.fromisoformat(str(refreshed['checked_at'])) == venue.NOW
            assert not monitoring.done()
        finally:
            release.set()
        monitoring.result(timeout=5)
    assert len(exchange.posts) == 2


def test_partial_fill_collects_remaining_buy_before_scoring(tmp_path):
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    exchange.orders[0]['size_matched'] = '5'
    exchange.positions = [dict(token_id='m00', condition_id='m00', size='5')]
    calls = []

    def cancel(order_id):
        calls.append('cancel')
        return dict(canceled=[order_id], status='CANCELED')

    exchange.cancel_order = cancel
    exchange.get_order_scoring = lambda order_id: calls.append('scoring') or True
    execution.lp_tick()
    assert calls == ['cancel'], 'fill cleanup must not wait on a scoring read'
    assert len(exchange.posts) == 1


def test_scheduler_reuses_published_facts_and_leaves_settled_history_alone(tmp_path):
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    execution.lp_auto_set_desired_running(False)
    execution.lp_tick()
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_auto_pool import NOW
    scheduler = LPAutoScheduler(execution, clock=lambda: NOW)
    read = exchange.lp_snapshot
    calls = []
    exchange.lp_snapshot = lambda request: calls.append(request['token_id']) or read(request)
    assert scheduler.run_due()
    assert calls == [], 'scheduler must reuse the facts tick just published'
    exchange.orders[0]['status'] = 'CANCELED'
    execution.lp_tick()
    calls.clear()
    assert scheduler.run_due()
    scheduler.request_check()
    assert scheduler.run_due()
    assert calls == [], 'settled history must leave high-frequency account reads'
    assert execution.lp_auto_state()['funds']['status'] == 'known'


def test_session_and_funds_publication_roll_back_together(tmp_path):
    import sqlite3
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    before = store.lp_session(sid)
    exchange.orders[0]['status'] = 'CANCELED'
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TRIGGER fail_funds BEFORE UPDATE ON lp_auto_pool BEGIN SELECT RAISE(ABORT, 'disk fault'); END")
    assert execution.lp_tick()['state'] == 'error'
    assert store.lp_session(sid) == before
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 8
    with sqlite3.connect(store.path) as connection:
        connection.execute('DROP TRIGGER fail_funds')
    execution.lp_tick()
    assert store.lp_session(sid)['state'] == 'complete'
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 0


def test_missing_submitted_session_keeps_reservation(tmp_path):
    import sqlite3
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    with sqlite3.connect(store.path) as connection:
        connection.execute('DELETE FROM lp_sessions WHERE session_id=?', (sid,))
    result = execution.lp_auto_run_once()
    assert result['funds']['status'] == 'unknown'
    assert Decimal(result['funds']['buy_reserved_usd']) == 8
    assert result['slots']['occupied'] == 1
    assert len(exchange.posts) == 1


def test_production_snapshot_preserves_unknown_position_lists():
    import pytest
    from tests.test_polymarket_lp import _SDKAccountClient, _SDKPublicClient
    from tests.test_lp_auto_pool import NOW
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
    account = _SDKAccountClient(NOW)
    adapter = PolymarketTradingClient(TradingConfig('0x'+'1'*40, '0x'+'2'*40), account,
                                    public_client_factory=lambda: _SDKPublicClient(NOW))
    request = dict(market_id='market-1', condition_id='0x'+'c'*64, token_id='0x'+'1'*64, outcome='YES')
    assert adapter.lp_snapshot(request)['account']['positions_complete'] is True
    account.list_positions = lambda: [object()]
    assert adapter.lp_snapshot(request)['account']['positions_complete'] is False
    account.list_positions = lambda: None
    with pytest.raises(ValueError, match='external_snapshot_unknown'):
        adapter.lp_snapshot(request)
    account.list_positions = lambda: []
    account.list_open_orders = lambda: [dict(order_id='o', token_id=request['token_id'],
        condition_id=request['condition_id'], side='BUY', status='LIVE', price='.4', original_size='20')]
    snapshot = adapter.lp_snapshot(request)
    assert snapshot['account']['open_orders_complete'] is False
    assert snapshot['orders'][0]['fill_quantity_known'] is False
    account.list_open_orders = lambda: []
    account.list_account_trades = lambda **kwargs: None
    with pytest.raises(ValueError, match='external_snapshot_unknown'):
        adapter.lp_snapshot(request)
    account.list_account_trades = lambda **kwargs: [object()]
    with pytest.raises(ValueError, match='external_snapshot_unknown'):
        adapter.lp_snapshot(request)


@pytest.mark.parametrize('missing_order', [None, 'extra'])
def test_production_snapshot_queries_every_owned_order(missing_order):
    from tests.test_polymarket_lp import _SDKAccountClient, _SDKPublicClient, _request
    from tests.test_lp_auto_pool import NOW
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig

    account = _SDKAccountClient(NOW)
    account.list_open_orders = lambda: []
    account.list_account_trades = lambda **kwargs: []
    request = dict(_request(NOW), entry_order_id='entry', augment_order_ids=['augment'],
                   owned_order_ids=['entry', 'augment', 'extra'])
    queried = []
    def get_order(*, order_id):
        queried.append(order_id)
        if order_id == missing_order:
            raise TimeoutError('receipt unavailable')
        return dict(order_id=order_id, token_id=request['token_id'],
                    condition_id=request['condition_id'], side='BUY', status='CANCELED',
                    price='.3', original_size='10', size_matched='0')
    account.get_order = get_order
    adapter = PolymarketTradingClient(TradingConfig('0x'+'1'*40, '0x'+'2'*40), account,
                                    public_client_factory=lambda: _SDKPublicClient(NOW))
    snapshot = adapter.lp_snapshot(request)
    assert sorted(queried) == ['augment', 'entry', 'extra']
    assert {row['order_id'] for row in snapshot['orders']} == {'entry', 'augment', 'extra'} - {missing_order}
    assert snapshot['orders_terminal'] is (missing_order is None)


@pytest.mark.parametrize('raw_orders', [None, []])
def test_share_watch_distinguishes_missing_orders_from_empty_orders(tmp_path, raw_orders):
    from types import SimpleNamespace
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig

    execution, _, _, _ = setup(tmp_path)
    adapter = PolymarketTradingClient(TradingConfig('signer', 'test-wallet'),
        SimpleNamespace(list_open_orders=lambda: raw_orders))
    execution._trading = adapter
    assert adapter.lp_open_orders_snapshot()['open_orders_complete'] is (raw_orders is not None)
    result = execution.refresh_lp_share_watch()
    assert result['state'] == ('unknown' if raw_orders is None else 'ready')
    if raw_orders is None:
        assert result['reason'] == 'account_facts_unknown'


@pytest.mark.parametrize('trade_kind', ['missing_status', 'missing_attribution', 'bad_maker', 'mined', 'failed'])
def test_production_trade_structure_preserves_funds_until_reconciled(tmp_path, monkeypatch, trade_kind):
    from datetime import datetime
    from types import SimpleNamespace
    from tests.test_lp_auto_pool import NOW
    from open_trader import polymarket_trading
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(polymarket_trading, 'datetime', Clock)
    execution, exchange, lp, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    exchange.orders[0]['status'] = 'CANCELED'
    trade = dict(id='t1', token_id='m00', market='m00', taker_order_id='o1',
                 side='BUY', status='CONFIRMED', price='.4', size='8')
    if trade_kind == 'missing_status':
        trade = dict(id='t1', token_id='m00')
    elif trade_kind == 'missing_attribution':
        trade.pop('taker_order_id')
    elif trade_kind == 'bad_maker':
        trade.update(taker_order_id='foreign', maker_orders=[
            dict(order_id='o1', side='BUY', price='.4', matched_amount='8')])
    else:
        trade['status'] = trade_kind.upper()
    public = SimpleNamespace(
        get_market=lambda **kwargs: dict(id='m00', condition_id='m00', state={'accepting_orders': True},
            outcomes={'yes': {'token_id': 'm00', 'label': 'YES'}}, trading={'fees_enabled': False},
            rewards={'rewards_min_size': '20', 'rewards_max_spread': '10'}),
        get_order_book=lambda **kwargs: {**exchange.direction('m00')['book'],
                                        'tick_size': '.01', 'min_order_size': '20'})
    sdk = SimpleNamespace(list_account_trades=lambda **kwargs: [trade],
                          get_order=lambda **kwargs: exchange.orders[0])
    adapter = PolymarketTradingClient(TradingConfig('signer', 'test-wallet'), sdk,
                                    public_client_factory=lambda: public)
    adapter._account_read_facts = lambda: (
        Decimal(1000), Decimal(1000), [], [], NOW, (trade,), True
    )
    lp.exchange = adapter
    result = execution.lp_auto_reconcile_unknown()
    if trade_kind != 'failed':
        assert store.lp_session(sid)['state'] != 'complete'
        assert result['funds']['status'] == 'unknown'
        assert result['funds']['available_usd'] is None
        assert Decimal(result['funds']['buy_reserved_usd']) == 8
        if trade_kind == 'mined':
            assert store.lp_session(sid)['facts_error'] == 'trade_not_confirmed'
    # A reliable failed trade and zero-fill terminal receipt can settle safely.
    trade.clear()
    trade.update(id='t1', token_id='m00', taker_order_id='o1', status='FAILED')
    result = execution.lp_auto_reconcile_unknown()
    assert store.lp_session(sid)['state'] == 'complete'
    assert result['funds']['status'] == 'known'
    assert Decimal(result['funds']['buy_reserved_usd']) == 0
    assert Decimal(result['funds']['inventory_cost_usd']) == 0
    assert len(exchange.posts) == 1


@pytest.mark.parametrize('incomplete', ['positions_complete', 'open_orders_complete'])
def test_manual_session_rejects_explicitly_incomplete_facts(tmp_path, incomplete):
    from tests.test_polymarket_lp import _Exchange, _request, _snapshot
    from tests.test_lp_auto_pool import NOW
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    store = PredictionArbitrageStore(tmp_path)
    exchange = _Exchange()
    exchange.snapshot_value = snapshot = _snapshot(NOW)
    snapshot['account'][incomplete] = False
    snapshot['orders'] = [dict(order_id='entry', token_id=_request(NOW)['token_id'],
        side='BUY', status='CANCELED', price='.3', original_size='10', size_matched='0')]
    store.lp_create_session('manual', 'manual', state='entry_open', payload=dict(
        _request(NOW), entry_order_id='entry', owned_order_ids=['entry'], submit_status='accepted'))
    service = PolymarketLPService(store, exchange, clock=lambda: NOW)
    service.tick()
    assert store.lp_session('manual')['state'] != 'complete'
    assert store.lp_session('manual')['facts_error'] == 'account_facts_incomplete'
    snapshot['account'][incomplete] = True
    service.tick()
    assert store.lp_session('manual')['state'] == 'complete'


def test_failed_account_read_blocks_funds_even_while_execution_lock_is_busy(tmp_path):
    import fcntl
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()

    def unavailable(request):
        raise OSError('account unavailable')

    exchange.lp_snapshot = unavailable
    with (tmp_path / 'execution.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = execution.lp_auto_reconcile_unknown()
    assert result['funds']['status'] == 'unknown'
    assert Decimal(result['funds']['buy_reserved_usd']) == 8
    assert len(exchange.posts) == 1


def test_tick_recovers_saved_accepted_id_after_receipt_apply_was_busy(tmp_path):
    import fcntl
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    original = exchange.lp_post_order
    with (tmp_path / 'execution.lock').open('a+') as lock:
        def posted(signed):
            result = original(signed)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return result
        exchange.lp_post_order = posted
        result = execution.lp_auto_run_once()
        sid = result['intents'][0]['session_id']
        assert not store.lp_session(sid).get('entry_order_id')
        assert result['funds']['status'] == 'unknown'
    exchange.orders.append(dict(order_id='extra', token_id='m00', condition_id='m00', side='BUY',
        status='CANCELED', original_size='1', size_matched='0', price='.4'))
    store.lp_update_session(sid, patch=dict(owned_order_ids=['extra']))
    execution.lp_tick()
    assert set(store.lp_session(sid)['owned_order_ids']) == {'extra', 'o1'}
    assert store.lp_session(sid)['entry_order_id'] == 'o1'
    assert execution.lp_auto_state()['funds']['status'] == 'known'
    assert len(exchange.posts) == 1


def test_fresh_read_during_manual_cancel_cannot_restore_known_funds(tmp_path):
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    entered, release = Event(), Event()

    def cancel(ids):
        entered.set()
        assert release.wait(5)
        return dict(canceled=list(ids), not_canceled={})

    exchange.cancel_orders_detailed = cancel
    with ThreadPoolExecutor(1) as workers:
        canceling = workers.submit(execution.lp_cancel_orders, dict(confirm=True, order_ids=['o1']))
        try:
            assert entered.wait(2)
            result = execution.lp_auto_reconcile_unknown()
            assert result['funds']['status'] == 'unknown'
            assert Decimal(result['funds']['buy_reserved_usd']) == 8
        finally:
            release.set()
        canceling.result(timeout=5)
    exchange.orders[0]['status'] = 'CANCELED'
    execution.lp_tick()
    assert execution.lp_auto_state()['funds']['status'] == 'known'
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 0
    assert len(exchange.posts) == 1


def test_session_lane_stays_owned_through_post_read_monitoring(tmp_path):
    execution, exchange, _, _ = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    entered, release, duplicate = Event(), Event(), Event()
    read = exchange.lp_snapshot
    calls = []

    def snapshot(request):
        calls.append(request['token_id'])
        if len(calls) > 1:
            duplicate.set()
        return read(request)

    def scoring(order_id):
        entered.set()
        assert release.wait(5)
        return True

    exchange.lp_snapshot = snapshot
    exchange.get_order_scoring = scoring
    with ThreadPoolExecutor(2) as workers:
        tick = workers.submit(execution.lp_tick)
        try:
            assert entered.wait(2)
            automatic = workers.submit(execution.lp_auto_reconcile_unknown)
            assert not duplicate.wait(.2), 'a second owner started before monitoring finished'
        finally:
            release.set()
        tick.result(timeout=5)
        automatic.result(timeout=5)
    assert len(exchange.posts) == 1


def test_flat_session_with_unknown_fees_stays_in_tick_reconciliation(tmp_path):
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    exchange.orders[0].update(status='FILLED', size_matched='20')
    exchange.orders.append(dict(order_id='sell', token_id='m00', condition_id='m00', side='SELL',
        status='FILLED', original_size='20', size_matched='20', price='.4'))
    store.lp_update_session(sid, patch=dict(passive_exit_order_id='sell', owned_order_ids=['o1','sell']))
    read = exchange.lp_snapshot

    def unknown_fees(request):
        snapshot = read(request)
        snapshot['market'].update(fees_enabled=True, fee=None)
        return snapshot

    exchange.lp_snapshot = unknown_fees
    execution.lp_tick()
    assert store.lp_session(sid)['state'] != 'complete'
    assert execution.lp_auto_state()['funds']['status'] == 'unknown'
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 8
    assert execution.lp_auto_state()['slots']['occupied'] == 1
    exchange.lp_snapshot = read
    execution.lp_tick()
    assert store.lp_session(sid)['state'] == 'complete'
    assert execution.lp_auto_state()['funds']['status'] == 'known'


def test_publish_compare_and_swap_conflict_is_a_retry_not_a_failed_round(tmp_path):
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    publish = store.lp_publish_facts
    raced = []

    def competing_writer(session_id, revision, **kwargs):
        if not raced:
            raced.append(True)
            store.lp_register_trade_change(session_id)
        return publish(session_id, revision, **kwargs)

    store.lp_publish_facts = competing_writer
    result = execution.lp_auto_reconcile_unknown()
    assert result['funds']['status'] == 'unknown'
    assert Decimal(result['funds']['buy_reserved_usd']) == 8
    assert execution.lp_auto_reconcile_unknown()['funds']['status'] == 'known'
    assert len(exchange.posts) == 1


def test_filled_submit_receipt_without_account_fill_facts_keeps_reservation(tmp_path):
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    post = exchange.lp_post_order
    def matched(signed):
        receipt = dict(post(signed), status='MATCHED')
        receipt.pop('size_matched')
        exchange.orders.clear()
        return receipt
    exchange.lp_post_order = matched
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    result = execution.lp_auto_reconcile_unknown()
    assert store.lp_session(sid)['state'] != 'complete'
    assert result['funds']['status'] == 'unknown'
    assert Decimal(result['funds']['buy_reserved_usd']) == 8
    assert result['slots']['occupied'] == 1
    assert len(exchange.posts) == 1


def test_slow_settled_report_does_not_block_active_recovery_or_new_buy(tmp_path):
    from datetime import timedelta
    from tests.test_lp_auto_pool import NOW
    execution, exchange, _, store = setup(tmp_path, 2)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    exchange.orders[0].update(status='FILLED', size_matched='20')
    exchange.orders.append(dict(order_id='sell', token_id='m00', condition_id='m00', side='SELL',
        status='FILLED', original_size='20', size_matched='20', price='.4'))
    store.lp_update_session(sid, patch=dict(passive_exit_order_id='sell', owned_order_ids=['o1','sell']))
    settled = execution.lp_auto_reconcile_unknown()['intents'][0]
    assert settled['settled'] and settled['report_pending']
    store.lp_update_session(sid, patch=dict(facts_checked_at=NOW-timedelta(seconds=301)))
    entered, release = Event(), Event()
    read = exchange.lp_snapshot
    def delayed(request):
        if request.get('entry_order_id') == 'o1':
            entered.set()
            assert release.wait(5)
        return read(request)
    exchange.lp_snapshot = delayed
    with ThreadPoolExecutor(2) as workers:
        assert execution.lp_generate_due_auto_reports() == []
        assert not entered.is_set()
        # Exercise the retained offline primitive without re-enabling its worker.
        report = workers.submit(execution._lp_auto_pool().reconcile_reports)
        try:
            assert entered.wait(2), 'offline report reconciliation must own the low-priority read'
            active = workers.submit(execution.lp_auto_run_once)
            result = active.result(timeout=2)
            assert len(exchange.posts) == 2
            assert result['funds']['status'] == 'known'
        finally:
            release.set()
        report.result(timeout=5)
    assert store.lp_session(sid)['state'] == 'complete'


def test_unknown_cancel_recovers_from_exact_terminal_receipt_after_restart(tmp_path):
    from types import SimpleNamespace
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    from tests.test_lp_auto_pool import NOW
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    cancels = []
    def lost_reply(ids):
        cancels.extend(ids)
        exchange.orders[0]['status'] = 'CANCELED'
        raise TimeoutError('cancel reply lost')
    exchange.cancel_orders_detailed = lost_reply
    with pytest.raises(TimeoutError):
        execution.lp_cancel_orders(dict(confirm=True, order_ids=['o1']))
    assert execution.lp_auto_state()['funds']['status'] == 'unknown'
    restarted = PredictionExecutionService(store=store, monitor=SimpleNamespace(), trading=exchange,
        notifier=SimpleNamespace(), lock_path=tmp_path/'execution.lock',
        lp=PolymarketLPService(store, exchange, clock=lambda: NOW))
    restarted._breaker_open = False
    restarted.lp_tick()
    assert store.lp_session(sid)['state'] == 'complete'
    assert restarted.lp_auto_state()['funds']['status'] == 'known'
    assert restarted.lp_auto_state()['slots']['occupied'] == 0
    assert cancels == ['o1'] and len(exchange.posts) == 1


def test_manual_cancel_during_scoring_fences_monitor_apply(tmp_path):
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    scoring, release, canceled = Event(), Event(), Event()
    def score(order_id):
        scoring.set()
        assert release.wait(5)
        return True
    def cancel(ids):
        assert execution.lp_auto_state()['funds']['status'] == 'unknown'
        canceled.set()
        return dict(canceled=list(ids), not_canceled={})
    exchange.get_order_scoring = score
    exchange.cancel_orders_detailed = cancel
    with ThreadPoolExecutor(2) as workers:
        tick = workers.submit(execution.lp_tick)
        try:
            assert scoring.wait(2)
            manual = workers.submit(execution.lp_cancel_orders, dict(confirm=True, order_ids=['o1']))
            assert canceled.wait(2), 'read-only scoring must not block manual cancellation'
        finally:
            release.set()
        tick.result(timeout=5)
        manual.result(timeout=5)
    assert canceled.is_set()
    assert store.lp_session(sid).get('scoring_status') != 'true'
    assert execution.lp_auto_state()['funds']['status'] == 'unknown'
    assert len(exchange.posts) == 1


def test_slow_dashboard_does_not_block_session_and_funds_publication(tmp_path):
    execution, exchange, _, store = setup(tmp_path)
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    execution.lp_auto_set_desired_running(True)
    sid = execution.lp_auto_run_once()['intents'][0]['session_id']
    entered, release = Event(), Event()
    def dashboard_account():
        entered.set()
        assert release.wait(5)
        return exchange.lp_account_snapshot()
    exchange.lp_account_snapshot_shared = dashboard_account
    with ThreadPoolExecutor(2) as workers:
        dashboard = workers.submit(execution.refresh_lp_dashboard_snapshot)
        try:
            assert entered.wait(2)
            exchange.orders[0]['status'] = 'CANCELED'
            result = workers.submit(execution.lp_auto_reconcile_unknown).result(timeout=2)
            assert result['funds']['status'] == 'known'
            assert result['slots']['occupied'] == 0
            assert store.lp_session(sid)['state'] == 'complete'
        finally:
            release.set()
        dashboard.result(timeout=5)


def test_slow_session_does_not_block_another_market_buy(tmp_path):
    execution, exchange, lp, _ = setup(tmp_path, 2)
    second = lp._candidate_pool.pop('m01')
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=2))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    lp._candidate_pool['m01'] = second
    entered, release = Event(), Event()
    read = exchange.lp_snapshot
    calls = []

    def delayed(request):
        if request['token_id'] == 'm00':
            calls.append('m00')
            entered.set()
            assert release.wait(5)
        return read(request)

    exchange.lp_snapshot = delayed
    with ThreadPoolExecutor(1) as workers:
        result = workers.submit(execution.lp_auto_run_once)
        try:
            assert entered.wait(2)
            state = result.result(timeout=2)
            assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01']
            assert Decimal(state['funds']['buy_reserved_usd']) >= 16
            execution.lp_auto_run_once()
            assert calls == ['m00'], 'do not stack reads behind the stalled session'
        finally:
            release.set()
        result.result(timeout=5)


def test_failed_session_keeps_capital_but_allows_other_market_buy(tmp_path):
    execution, exchange, lp, _ = setup(tmp_path, 2)
    second = lp._candidate_pool.pop('m01')
    execution.lp_auto_configure(dict(budget_usd='16', target_buy_count=2))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    lp._candidate_pool['m01'] = second
    read = exchange.lp_snapshot

    def failed(request):
        if request['token_id'] == 'm00':
            raise ValueError('order_schema_unknown')
        return read(request)

    exchange.lp_snapshot = failed
    state = execution.lp_auto_run_once()
    assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01']
    assert Decimal(state['funds']['buy_reserved_usd']) >= 16
    assert state['funds']['status'] == 'unknown', 'do not present partial facts as complete'
    assert Decimal(state['funds']['spendable_usd']) == 0


def test_market_read_capacity_remains_fail_fast_for_shared_snapshot_callers(tmp_path):
    _, exchange, lp, _ = setup(tmp_path, 2)
    first_entered, second_entered = Event(), Event()
    release = Event()
    snapshot = exchange.lp_snapshot
    def blocked(request):
        event = first_entered if request['token_id'] == 'm00' else second_entered
        event.set()
        assert release.wait(5)
        return snapshot(request)
    exchange.lp_snapshot = blocked
    def shared_read(identity):
        lp._facts_owner.session_id = 'test-session'
        try:
            lp._read_snapshot(identity)
        finally:
            lp._facts_owner.session_id = None
    reads = [Thread(target=shared_read, args=(identity,))
             for identity in (dict(condition_id='m00', token_id='m00'),
                              dict(condition_id='m01', token_id='m01'))]
    for read in reads:
        read.start()
    assert first_entered.wait(2) and second_entered.wait(2)
    try:
        lp._facts_owner.session_id = 'test-session'
        with pytest.raises(ValueError, match='market_read_capacity'):
            lp._read_snapshot(dict(condition_id='m02', token_id='m02'))
    finally:
        lp._facts_owner.session_id = None
        release.set()
        for read in reads:
            read.join(2)


def test_market_read_timeout_discards_late_result_and_preserves_other_capacity(tmp_path):
    _, exchange, lp, _ = setup(tmp_path, 2)
    lp._market_read_timeout = .02
    lp._facts_owner.session_id = 'test-session'
    entered, release, finished = Event(), Event(), Event()
    original = exchange.lp_snapshot
    calls = []

    def delayed(request):
        if request['token_id'] == 'm00':
            calls.append('m00')
            entered.set()
            assert release.wait(5)
            finished.set()
        return original(request)

    exchange.lp_snapshot = delayed
    slow = dict(condition_id='m00', token_id='m00')
    try:
        with pytest.raises(ValueError, match='market_read_timeout'):
            lp._read_snapshot(slow)
        assert entered.is_set()
        with pytest.raises(ValueError, match='market_read_in_progress'):
            lp._read_snapshot(slow)
        assert lp._read_snapshot(dict(condition_id='m01', token_id='m01'))['market']['token_id'] == 'm01'
        assert calls == ['m00']
    finally:
        release.set()
    assert finished.wait(2)
    with pytest.raises(ValueError, match='market_read_cooling_down'):
        lp._read_snapshot(slow)
    lp._market_read_retry['m00'] = 0
    assert lp._read_snapshot(slow)['market']['token_id'] == 'm00'
    assert calls == ['m00', 'm00'], 'late abandoned result must not become fresh facts'


def test_order_identity_mismatch_cannot_use_isolated_funds(tmp_path):
    execution, exchange, lp, _ = setup(tmp_path, 2)
    second = lp._candidate_pool.pop('m01')
    execution.lp_auto_configure(dict(budget_usd='100', target_buy_count=2))
    execution.lp_auto_set_desired_running(True)
    execution.lp_auto_run_once()
    lp._candidate_pool['m01'] = second
    read = exchange.lp_snapshot

    def conflict(request):
        snapshot = read(request)
        snapshot['orders'] = [{**order, 'token_id': 'wrong-token'} for order in snapshot['orders']]
        return snapshot

    exchange.lp_snapshot = conflict
    state = execution.lp_auto_run_once()
    assert len(exchange.posts) == 1
    assert 'unbounded_financial_uncertainty' in state['admission_block_reasons']
    assert state['funds']['spendable_usd'] is None
