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


@pytest.mark.parametrize('scenario', ['initially_known', 'recovers_unknown'])
def test_current_buy_yield_uses_completed_market_read_time(runtime, scenario):
    from copy import deepcopy

    store, account, lp, engine, public, order = _managed_buy(runtime, 'manual')
    sid = next(s['session_id'] for s in store.lp_sessions() if order.id in s['owned_order_ids'])
    original_audits = deepcopy(store.lp_actions(sid))
    started = runtime.clock[0]
    if scenario == 'recovers_unknown':
        rewards = _unknown_rewards(public)
        engine.lp_tick()
        timer = dict(_timer(store, order.id))
        started = datetime.fromisoformat(timer['unknown_since'].replace('Z', '+00:00'))
        public.list_market_rewards = rewards

    read = public.get_order_book
    progresses = []
    def advancing_book(*, token_id):
        before = runtime.clock[0]
        runtime.clock[0] += timedelta(seconds=1)
        book = read(token_id=token_id)
        progresses.append((token_id, before, runtime.clock[0], book))
        return book
    public.get_order_book = advancing_book
    engine.lp_tick()
    assert progresses and all(after > before for _, before, after, _ in progresses)
    assert any(token == order.token_id for token, _, _, _ in progresses)
    assert _timer(store, order.id) is None, 'A healthy book received after read start has known current yield'
    runtime.clock[0] = started + timedelta(seconds=601)
    engine.lp_tick()
    assert _timer(store, order.id) is None
    assert account.cancels == account.posts == account.market_orders == []
    assert account.orders == (order,)
    current = store.lp_session(sid)
    owned = current['order_history'][order.id]
    assert owned['status'] == 'LIVE' and owned['side'] == 'BUY' and owned['token_id'] == order.token_id
    assert Decimal(owned['price']) == Decimal('.40') and Decimal(owned['size_matched']) == 0
    assert Decimal(owned.get('original_size', owned.get('quantity'))) == 20
    assert Decimal(owned['price']) * 20 == 8
    assert store.lp_actions(sid) == original_audits
    lp.drain_submissions()


@pytest.mark.parametrize('scenario', ['normal', 'interrupted_after_rejection'])
def test_expired_buy_yield_recovers_after_definitely_unsent_cancel(runtime, tmp_path, monkeypatch, scenario):
    import json
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing
    from copy import deepcopy
    from pathlib import Path
    from threading import get_ident
    from open_trader import polymarket_trading, prediction_arbitrage_execution
    from open_trader.notifications import CompositeNotifier
    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    from tests.test_prediction_arbitrage_execution import ChannelNotifier

    store, account, lp, _, public, order = _managed_buy(runtime, 'manual')
    # Startup and adapter facts share isolated business time; Event watchdogs
    # keep the real threading/time primitives used by the existing runtime.
    monkeypatch.setattr(prediction_arbitrage_execution, 'datetime', polymarket_trading.datetime)

    def public_owner(current_store, current_account, current_lp):
        current_account.is_gasless_ready = lambda: True
        def unused_merge(**kwargs):
            raise AssertionError('The exact unfilled account has no inventory to merge')
        current_account.merge_positions = unused_merge
        return PredictionExecutionService(store=current_store, monitor=SimpleNamespace(),
            trading=current_lp.exchange, lp=current_lp, lock_path=tmp_path / 'public-startup.lock',
            notifier=CompositeNotifier([ChannelNotifier('macos'), ChannelNotifier('feishu')]))

    engine = public_owner(store, account, lp)
    ready = engine.reconcile_startup()
    assert ready['state'] == 'ready' and engine.lp_mutation_allowed() is True, ready
    sid = next(s['session_id'] for s in store.lp_sessions() if order.id in s['owned_order_ids'])
    original_entry = deepcopy(store.lp_actions(sid))
    controls = ('stop_requested', 'loss_requested', 'entry_cancel_requested', 'queue_protection')
    original_controls = {key: deepcopy(store.lp_session(sid).get(key)) for key in controls}
    _unknown_rewards(public)
    engine.lp_tick()
    original_timer = deepcopy(_timer(store, order.id))
    since = datetime.fromisoformat(original_timer['unknown_since'].replace('Z', '+00:00'))
    runtime.clock[0] = since + timedelta(seconds=600)

    entered, release = Event(), Event()
    startup_thread, startup_jobs, commits, errors, rejections = [], [], [], [], []
    class NativeInterruption(BaseException):
        pass
    read_orders = account.list_open_orders
    def startup_orders(**kwargs):
        if startup_thread and get_ident() == startup_thread[0]:
            entered.set()
            assert release.wait(5), 'Genuine startup account SDK read watchdog'
        return read_orders(**kwargs)
    account.list_open_orders = startup_orders
    native_connect = sqlite3.connect
    with ThreadPoolExecutor(2) as workers:
        def startup():
            startup_thread.append(get_ident())
            return engine.reconcile_startup()

        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                result = super().execute(sql, parameters)
                if sql.strip().upper() != 'COMMIT':
                    return result
                row = super().execute('SELECT payload FROM lp_sessions WHERE session_id=?', (sid,)).fetchone()
                if row is None:
                    return result
                timer = (json.loads(row[0]).get('buy_yield_unknown') or {}).get(order.id) or {}
                key = timer.get('cancel_request_key')
                if not key:
                    return result
                action = super().execute('SELECT * FROM lp_actions WHERE session_id=? AND action_key=?',
                    (sid, key)).fetchone()
                if action is None:
                    return result
                payload = json.loads(action['payload'])
                if (commits and key == commits[0]['action_key'] and action['state'] == 'rejected'
                        and payload.get('reason') == 'buy_yield_unknown_timeout' and not rejections):
                    assert not self.in_transaction
                    # Read the first real committed rejection independently,
                    # before returning to the caller or allowing another writer.
                    with closing(native_connect(store.path)) as reader:
                        reader.row_factory = sqlite3.Row
                        committed = dict(reader.execute('SELECT * FROM lp_actions WHERE session_id=? AND action_key=?',
                            (sid, key)).fetchone())
                        committed_session = json.loads(reader.execute('SELECT payload FROM lp_sessions WHERE session_id=?',
                            (sid,)).fetchone()[0])
                    assert committed == dict(action)
                    assert committed_session['buy_yield_unknown'][order.id]['unknown_since'] == original_timer['unknown_since']
                    assert committed_session['buy_yield_unknown'][order.id]['cancel_request_key'] == key
                    assert committed['action_id'] == commits[0]['action_id'] and account.cancels == []
                    rejections.append(dict(row=committed, payload=json.loads(committed['payload']),
                        native_commit_returned=True, in_transaction=False))
                    if scenario == 'interrupted_after_rejection':
                        raise NativeInterruption('Original caller ends after first real rejection COMMIT')
                if commits:
                    return result
                if (action['state'] != 'pending' or payload.get('role') != 'reconciliation_cancel'
                        or payload.get('reason') != 'buy_yield_unknown_timeout'
                        or payload.get('order_id') != order.id):
                    return result
                try:
                    assert not self.in_transaction, 'Actual risk timer/key COMMIT completed before permission change'
                    assert timer['unknown_since'] == original_timer['unknown_since']
                    original_row = dict(action)
                    commits.append(dict(action_key=key, action_id=action['action_id'],
                        session_id=sid, order_id=order.id, timer=deepcopy(timer),
                        native_row=original_row, native_commit_returned=True, in_transaction=False))
                    startup_jobs.append(workers.submit(startup))
                    assert entered.wait(5), 'Public startup reached genuine account SDK read'
                    assert engine.lp_mutation_allowed() is False
                    assert dict(super().execute('SELECT * FROM lp_actions WHERE action_key=?',
                        (key,)).fetchone()) == original_row, 'Observer never changes native request bytes'
                    assert account.cancels == []
                except BaseException as exc:
                    errors.append(repr(exc))
                    raise
                return result

        def observed_connect(database, *args, **kwargs):
            if Path(database).resolve() == store.path.resolve():
                kwargs['factory'] = ObservedConnection
            return native_connect(database, *args, **kwargs)
        monkeypatch.setattr(sqlite3, 'connect', observed_connect)
        monitoring = workers.submit(engine.lp_tick)
        try:
            if scenario == 'interrupted_after_rejection':
                with pytest.raises(NativeInterruption, match='first real rejection COMMIT'):
                    monitoring.result(timeout=5)
                assert len(rejections) == 1
            else:
                monitoring.result(timeout=5)
            assert len(commits) == len(startup_jobs) == 1 and not errors, (commits, errors)
            assert entered.is_set() and not startup_jobs[0].done()
            first, = [a for a in store.lp_actions(sid) if a.get('reason') == 'buy_yield_unknown_timeout']
            assert first['action_key'] == commits[0]['action_key'] and first['action_id'] == commits[0]['action_id']
            assert first['state'] == 'rejected' and first.get('post_started') is not True
            assert first['order_id'] == order.id and first['session_id'] == sid
            assert _timer(store, order.id)['unknown_since'] == original_timer['unknown_since']
            assert account.cancels == [] and account.orders == (order,)
        finally:
            release.set()
            if startup_jobs:
                ready = startup_jobs[0].result(timeout=5)
        assert ready['state'] == 'ready' and engine.lp_mutation_allowed() is True, ready

    rejected = deepcopy(first)
    if scenario == 'interrupted_after_rejection':
        lp.drain_submissions()
        lp.exchange.close()
        balance_read = account.get_balance_allowance
        store, adapter, account, lp, _ = runtime(public_client=public, orders=(order,))
        account.get_order = lambda *, order_id: order
        account.get_balance_allowance = balance_read
        engine = public_owner(store, account, lp)
        cold_ready = engine.reconcile_startup()
        assert cold_ready['state'] == 'ready' and engine.lp_mutation_allowed() is True, cold_ready
        assert next(a for a in store.lp_actions(sid) if a['action_key'] == rejected['action_key']) == rejected
        assert _timer(store, order.id)['unknown_since'] == original_timer['unknown_since']
    engine.lp_tick()
    assert account.cancels == [(order.id,)], 'Restored actual permission creates one distinct authorized risk request'
    assert rejected['post_started'] is False and rejected['submit_stage'] == 'pre_send_rejected'
    assert len(rejections) == 1 and rejections[0]['payload']['post_started'] is False
    assert rejections[0]['payload']['submit_stage'] == 'pre_send_rejected'
    risks = [a for a in store.lp_actions(sid) if a.get('reason') == 'buy_yield_unknown_timeout']
    assert len(risks) == 2
    accepted, = [a for a in risks if a['state'] == 'accepted']
    assert accepted['action_id'] != rejected['action_id'] and accepted['action_key'] != rejected['action_key']
    assert next(a for a in risks if a['action_key'] == rejected['action_key']) == rejected
    assert accepted['order_id'] == order.id and accepted['session_id'] == sid
    assert _timer(store, order.id)['cancel_request_key'] == accepted['action_key']
    assert _timer(store, order.id)['unknown_since'] == original_timer['unknown_since']
    current = store.lp_session(sid)
    exact = current['order_history'][order.id]
    assert current['account_id'] == WALLET and current['token_id'] == order.token_id
    assert exact['status'] == 'LIVE' and exact['side'] == 'BUY' and exact['token_id'] == order.token_id
    assert Decimal(exact['price']) == Decimal('.40') and Decimal(exact['size_matched']) == 0
    assert Decimal(exact.get('original_size', exact.get('quantity'))) == 20
    assert Decimal(exact['price']) * Decimal(exact.get('original_size', exact.get('quantity'))) == 8
    assert Decimal(current['buy_filled_quantity']) == Decimal(current['residual_quantity']) == 0
    (tmp_path / 'native-control-observation.json').write_text(json.dumps(dict(
        original=original_controls, current={key: current.get(key) for key in controls},
        first_rejection=rejections[0], scenario=scenario), default=str, indent=2))
    if scenario == 'normal':
        assert {key: current.get(key) for key in controls} == original_controls
    else:
        assert {key: current.get(key) for key in controls if key != 'queue_protection'} == {
            key: original_controls[key] for key in controls if key != 'queue_protection'}
        original_queue = original_controls['queue_protection']
        current_queue = current['queue_protection']
        assert {key: value for key, value in current_queue.items() if key != 'levels'} == {
            key: value for key, value in original_queue.items() if key != 'levels'}
        assert current_queue['levels'].keys() == original_queue['levels'].keys()
        observed_fields = {'state', 'data_time', 'reason_codes', 'front_estimate', 'level_total', 'ratio'}
        for level, baseline in original_queue['levels'].items():
            observed = current_queue['levels'][level]
            assert {key: value for key, value in observed.items() if key not in observed_fields} == {
                key: value for key, value in baseline.items() if key not in observed_fields}
            # A genuine cold monitor acquired the healthy SDK book:1000 at
            # .40 contains our20, leaving980 ahead. Only observation changes.
            assert observed['state'] == 'monitoring' and observed['reason_codes'] == []
            assert Decimal(level) == Decimal('.40') and Decimal(observed['level_total']) == 1000
            assert Decimal(observed['front_estimate']) == 980 and Decimal(observed['ratio']) == Decimal('.98')
            assert datetime.fromisoformat(observed['data_time'].replace('Z', '+00:00')) == runtime.clock[0]
    assert all(a in store.lp_actions(sid) for a in original_entry)
    assert account.posts == account.market_orders == [] and account.orders == (order,)
    original_risks = deepcopy(risks)
    engine.lp_tick()
    assert account.cancels == [(order.id,)]
    assert [a for a in store.lp_actions(sid) if a.get('reason') == 'buy_yield_unknown_timeout'] == original_risks
    lp.drain_submissions()
    lp.exchange.close()
    cold_store, adapter, cold_account, cold_lp, _ = runtime(public_client=public, orders=(order,))
    cold_account.get_order = lambda *, order_id: order
    cold_account.get_balance_allowance = account.get_balance_allowance
    cold_engine = public_owner(cold_store, cold_account, cold_lp)
    cold_ready = cold_engine.reconcile_startup()
    assert cold_ready['state'] == 'ready' and cold_engine.lp_mutation_allowed() is True, cold_ready
    cold_engine.lp_tick()
    assert cold_account.cancels == cold_account.posts == cold_account.market_orders == []
    assert cold_account.orders == (order,)
    assert [a for a in cold_store.lp_actions(sid) if a.get('reason') == 'buy_yield_unknown_timeout'] == original_risks
    assert _timer(cold_store, order.id)['unknown_since'] == original_timer['unknown_since']
    assert _timer(cold_store, order.id)['cancel_request_key'] == accepted['action_key']
    cold_lp.drain_submissions()


@pytest.mark.parametrize('ordering', ['publication_first', 'registration_first', 'persistent_unknown'])
def test_known_yield_and_unknown_cancel_claim_are_ordered(request, runtime, monkeypatch, tmp_path, ordering):
    from timing_support import run_test_in_subprocess
    if run_test_in_subprocess(request):
        return
    import json
    import sqlite3
    from copy import deepcopy
    from pathlib import Path
    from threading import Thread, get_ident
    from concurrent.futures import Future
    store, account, lp, engine, public, order = _managed_buy(runtime, 'manual')
    instances = [lp]
    rewards = _unknown_rewards(public)
    engine.lp_tick()
    original_timer = deepcopy(_timer(store, order.id))
    since = datetime.fromisoformat(original_timer['unknown_since'].replace('Z', '+00:00'))
    sid = next(s['session_id'] for s in store.lp_sessions() if order.id in s['owned_order_ids'])
    original_audits = deepcopy(store.lp_actions(sid))
    original_controls = {k: deepcopy(store.lp_session(sid).get(k)) for k in
        ('stop_requested', 'loss_requested', 'entry_cancel_requested', 'queue_protection')}
    runtime.clock[0] = since + timedelta(seconds=600)
    expired_at = runtime.clock[0]
    entered, release = Event(), Event()
    monitor_threads, owner_threads, reads, commits = [], [], [], []
    native_connect = sqlite3.connect
    class NativeCursor(sqlite3.Cursor):
        def execute(self, sql, parameters=(), /):
            self.statement, self.parameters = sql, parameters
            return super().execute(sql, parameters)
        def fetchone(self):
            row = super().fetchone()
            if (ordering == 'publication_first' and not reads and monitor_threads
                    and owner_threads and get_ident() == owner_threads[0] and not self.connection.in_transaction
                    and self.statement == 'SELECT * FROM lp_sessions WHERE session_id=?'
                    and tuple(self.parameters) == (sid,) and row is not None):
                payload = json.loads(row['payload'])
                observation = (payload.get('buy_yield_observations') or {}).get(order.id) or {}
                if observation.get('attempted_at') == expired_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ'):
                    assert payload['buy_yield_unknown'][order.id]['unknown_since'] == original_timer['unknown_since']
                    self.close()  # Release the native read cursor before another writer proceeds.
                    reads.append(dict(payload=payload, in_transaction=False, thread=get_ident()))
                    entered.set()
                    assert release.wait(5), 'Independent post-read/preclaim watchdog'
            return row
    class NativeConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            result = self.cursor(factory=NativeCursor).execute(sql, parameters)
            if sql == 'COMMIT':
                row = self.cursor().execute('SELECT payload FROM lp_sessions WHERE session_id=?', (sid,)).fetchone()
                if row:
                    payload = json.loads(row[0])
                    timer = (payload.get('buy_yield_unknown') or {}).get(order.id)
                    observation = (payload.get('buy_yield_observations') or {}).get(order.id) or {}
                    if (not owner_threads and observation.get('state') == 'unknown'
                            and observation.get('attempted_at') == expired_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ')):
                        owner_threads.append(get_ident())
                    actions = [dict(a) for a in self.cursor().execute(
                        'SELECT * FROM lp_actions WHERE session_id=?', (sid,)).fetchall()]
                    risks = [a for a in actions if json.loads(a['payload']).get('reason') == 'buy_yield_unknown_timeout']
                    if observation.get('state') == 'known' or risks:
                        commits.append(dict(timer=timer, observation=observation, risks=risks,
                            in_transaction=self.in_transaction, thread=get_ident()))
            return result
    def connect(database, *args, **kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs['factory'] = NativeConnection
        return native_connect(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    def cancel(*, order_ids):
        account.cancels.append(order_ids)
        assert order_ids == (order.id,)
        if ordering == 'registration_first':
            entered.set()
            assert release.wait(5), 'Independent registered external cancel watchdog'
        return CancelOrdersResponse(canceled=order_ids, not_canceled={})
    account.cancel_orders = cancel
    result = Future()
    def monitor():
        monitor_threads.append(get_ident())
        try:
            result.set_result(engine.lp_tick())
        except BaseException as exc:
            result.set_exception(exc)
    thread = Thread(target=monitor, daemon=True)
    thread.start()
    try:
        if ordering != 'persistent_unknown':
            assert entered.wait(5), 'Actual monitor reaches the selected native/external boundary'
            if ordering == 'publication_first':
                assert len(reads) == 1 and account.cancels == []
            else:
                assert any(c['risks'] and c['timer'] and c['timer'].get('cancel_request_key') for c in commits)
            public.list_market_rewards = rewards
            public.rates[6] = '0'  # Known zero is still known current yield.
            runtime.clock[0] += timedelta(seconds=1)
            published = engine.lp_auto_run_once(round_id='known-before-release')
            assert published['last_round']['round_id'] == 'known-before-release'
            assert _timer(store, order.id) is None
            assert any(c['observation'].get('state') == 'known' and not c['timer'] and not c['in_transaction'] for c in commits)
            if ordering == 'publication_first':
                assert not any(c['risks'] for c in commits), 'Known committed before any timeout claim'
        release.set()
        result.result(timeout=5)
        thread.join(5)
        assert not thread.is_alive()
        lp.drain_submissions()
        risks = [a for a in store.lp_actions(sid) if a.get('reason') == 'buy_yield_unknown_timeout']
        if ordering == 'publication_first':
            assert risks == [] and account.cancels == [], 'Committed known yield must prevent stale UNKNOWN cancellation'
            assert _timer(store, order.id) is None
            assert store.lp_actions(sid) == original_audits
        else:
            risk, = risks
            assert risk['state'] == 'accepted' and risk['order_id'] == order.id and risk['session_id'] == sid
            assert account.cancels == [(order.id,)] and risk['action_id'] and risk['action_key']
            if ordering == 'registration_first':
                assert _timer(store, order.id) is None
        current = store.lp_session(sid)
        assert {k: current.get(k) for k in original_controls if k != 'queue_protection'} == {
            k: v for k, v in original_controls.items() if k != 'queue_protection'}
        for level, bucket in original_controls['queue_protection']['levels'].items():
            observed = current['queue_protection']['levels'][level]
            for key in ('order_id', 'baseline_front', 'baseline_price', 'baseline_book_hash',
                    'baseline_book_received_at', 'baseline_source_timestamp', 'baseline_version', 'cancel_scope', 'threshold'):
                assert observed.get(key) == bucket.get(key)
        assert account.posts == account.market_orders == [] and account.orders == (order,)
        frozen = deepcopy(store.lp_actions(sid))
        engine.lp_tick()
        lp.drain_submissions()
        assert store.lp_actions(sid) == frozen and len(account.cancels) == (0 if ordering == 'publication_first' else 1)
        lp.exchange.close()
        store, adapter, cold_account, lp, engine = runtime(public_client=public, orders=(order,))
        instances.append(lp)
        cold_account.get_order = lambda *, order_id: order
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        engine.lp_tick()
        lp.drain_submissions()
        assert cold_account.cancels == cold_account.posts == cold_account.market_orders == []
        assert store.lp_actions(sid) == frozen
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        for service in instances:
            service.drain_submissions()
        (tmp_path / 'review-F2-native-ordering.json').write_text(json.dumps(
            dict(ordering=ordering, reads=reads, commits=commits), default=str, indent=2))
