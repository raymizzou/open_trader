"""One successful financial observation funds one fixed automatic round."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
import sqlite3

import pytest

from open_trader.polymarket_lp_scheduler import LPAutoScheduler
from open_trader.polymarket_trading import PolymarketTradingError
from tests import test_lp_auto_pool as pool
from tests.test_lp_auto_plan_execution import install_order_result_reader
from tests.test_lp_auto_target_convergence import live_orders, plan_setup, publish_candidates
from tests.test_lp_auto_refill_contract import RefillPublic, prepare
from tests.test_lp_account_reservation_reconciliation import (
    runtime, advance_api_wait, _position, _refill_identity,
)
from tests.test_lp_order_registration_contract import _open_order


def pool_document(store):
    with sqlite3.connect(store.path) as connection:
        document = json.loads(connection.execute(
            'SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    return document


def account_packet(store):
    return pool_document(store)['account_financial_facts']


def economics(value):
    if isinstance(value, dict):
        return {key: economics(item) for key, item in value.items()
                if key not in {'checked_at', 'read_started_at', 'read_ended_at', 'snapshot_id'}}
    if isinstance(value, list):
        return [economics(item) for item in value]
    return value


@pytest.mark.parametrize('initial', [(), ('A', 'B', 'C', 'D', 'I')],
                         ids=['refill', 'rotation'])
def test_equivalent_refresh_does_not_discard_fixed_plan(tmp_path, monkeypatch, initial):
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, initial, budget='39')
    install_order_result_reader(exchange, monkeypatch)
    books = exchange.lp_order_books
    publications = []
    triggered = [False]

    def publishing_books(ids, **kwargs):
        result = books(ids, **kwargs)
        if tuple(ids) == ('G',) and not triggered[0]:
            triggered[0] = True
            for _ in range(2 if initial else 1):
                before = account_packet(store)
                assert before['financial_status'] == 'known'
                assert engine.lp_auto_state()['active_plan'] is None
                monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
                published = engine.refresh_lp_dashboard_snapshot()
                after = account_packet(store)
                assert published['state'] == 'ready', published
                assert before['trade_generation'] == after['trade_generation']
                assert before['checked_at'] != after['checked_at']
                assert before['snapshot_id'] != after['snapshot_id']
                assert economics(before) == economics(after)
                publications.append((before, after))
        return result

    monkeypatch.setattr(exchange, 'lp_order_books', publishing_books)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert len(publications) == (2 if initial else 1)
    assert [row['token_id'] for row in state['last_round']['targets']] == ['B', 'C', 'E', 'F', 'G'], state['last_round']
    round_id = state['last_round']['round_id']
    actions = [(action['action_id'], action['kind']) for action in state['last_round']['actions']]
    terms = [(row['token_id'], Decimal(row['price']), Decimal(row['quantity']))
             for row in state['last_round']['targets']]
    assert terms == [(token, Decimal('.39'), Decimal('20')) for token in ('B', 'C', 'E', 'F', 'G')]
    if initial:
        assert exchange.cancels == ['original-A', 'original-D', 'original-I']
        assert exchange.posts == []
        assert state['active_plan']['round_id'] == round_id
        assert state['plan_wait']['kind'] == 'order'
        monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
        assert scheduler.run_due()
        state = engine.lp_auto_state()
    assert state['last_round']['round_id'] == round_id
    assert [(action['action_id'], action['kind']) for action in state['last_round']['actions']] == actions
    assert [(row['token_id'], Decimal(row['price']), Decimal(row['quantity']))
            for row in state['last_round']['targets']] == terms
    assert [post['token_id'] for post in exchange.posts] == (['E', 'F', 'G'] if initial else ['B', 'C', 'E', 'F', 'G'])
    assert all(post['price'] == Decimal('.39') and post['quantity'] == 20 for post in exchange.posts)
    assert exchange.cancels == (['original-A', 'original-D', 'original-I'] if initial else [])
    assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
    if initial:
        assert live_orders(exchange)['B'] == 'original-B'
        assert live_orders(exchange)['C'] == 'original-C'
    assert state['active_plan'] is None and state['last_round']['completed_at']
    assert state['plan_wait']['kind'] == 'round'
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(
        state['last_round']['completed_at']) + timedelta(seconds=60)


@pytest.mark.parametrize('change', ['generation_after_observation',
    'time_after_observation', 'incomplete_background_publication'])
def test_round_observation_survives_later_financial_churn(tmp_path, monkeypatch, change):
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, budget='39')
    install_order_result_reader(exchange, monkeypatch)
    shared = exchange.lp_account_snapshot_shared
    raw_account = exchange.lp_account_snapshot
    metadata = exchange.lp_market_metadata_fresh
    observation, publications, reads = [], [], []
    publishing, incomplete, triggered = [False], [False], [False]

    def account(**kwargs):
        reads.append(('shared', 'dashboard' if publishing[0] else 'automatic'))
        result = shared(**kwargs)
        if incomplete[0]:
            result['open_orders_complete'] = False
            result['pagination_complete'] = False
        return result

    def counted_account():
        reads.append(('raw', 'dashboard' if publishing[0] else 'automatic'))
        return raw_account()

    def changing_metadata(ids, **kwargs):
        if not triggered[0]:
            triggered[0] = True
            initial = account_packet(store)
            assert initial['financial_status'] == 'known'
            assert engine.lp_auto_state()['funds']['status'] == 'known'
            assert engine.lp_auto_state()['active_plan'] is None
            observation.append(deepcopy(initial))
            if change == 'time_after_observation':
                # Every external market source is read after this advance.
                monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=61))
            else:
                publishing[0] = True
                try:
                    if change == 'generation_after_observation':
                        assert store.lp_advance_trade_generation(initial['trade_generation'])
                        exchange.positions = [dict(token_id='H', condition_id='H',
                            size='8', average_price='.39')]
                    else:
                        incomplete[0] = True
                    publications.append(engine.refresh_lp_dashboard_snapshot())
                finally:
                    incomplete[0] = False
                    publishing[0] = False
        return metadata(ids, **kwargs)

    monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', account)
    monkeypatch.setattr(exchange, 'lp_account_snapshot', counted_account)
    monkeypatch.setattr(exchange, 'lp_market_metadata_fresh', changing_metadata)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert len(observation) == 1
    assert reads.count(('shared', 'automatic')) == reads.count(('raw', 'automatic')) == 1
    dashboard_count = 0 if change == 'time_after_observation' else 1
    assert reads.count(('shared', 'dashboard')) == reads.count(('raw', 'dashboard')) == dashboard_count
    if change == 'generation_after_observation':
        assert publications[0]['state'] == 'ready'
        current = account_packet(store)
        assert current['trade_generation'] > observation[0]['trade_generation']
        assert Decimal(current['inventory_cost_usd']) == Decimal('3.12')
    elif change == 'incomplete_background_publication':
        assert publications[0]['reason'] == 'account_order_sync_unknown'
        assert 'account_order_sync_unknown' in state['admission_block_reasons']
        assert account_packet(store) == observation[0]
    else:
        assert account_packet(store) == observation[0]
        assert (pool.NOW - datetime.fromisoformat(observation[0]['checked_at'])).total_seconds() >= 61
        assert state['funds']['as_of'] == observation[0]['checked_at']
    assert [post['token_id'] for post in exchange.posts] == ['B', 'C', 'E', 'F', 'G'], state['last_round']
    assert all(post['price'] == Decimal('.39') and post['quantity'] == 20 for post in exchange.posts)
    assert exchange.cancels == []
    assert [row['token_id'] for row in state['last_round']['targets']] == ['B', 'C', 'E', 'F', 'G']
    assert state['active_plan'] is None and state['last_round']['completed_at']
    assert pool_document(store)['execution_order_facts']['approval_facts_identity'] == [
        observation[0]['snapshot_id'], observation[0]['trade_generation']]
    assert state['plan_wait']['kind'] == 'round'
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(
        state['last_round']['completed_at']) + timedelta(seconds=60)
    if change == 'generation_after_observation':
        first_round = state['last_round']['round_id']
        monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
        publish_candidates(exchange, lp, store, tuple('ABCDEFGHI'))
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        assert state['last_round']['round_id'] != first_round
        assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.12')
        assert [row['token_id'] for row in state['last_round']['targets']] == ['B', 'C', 'E', 'F']


def sdk_planning_setup(runtime):
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24', 3: '12', 4: '6', 5: '3', 6: '1'}
    result = prepare(runtime, budget='39', target=5, count=5, public=public)
    execution = result[4]
    execution.lp_auto_set_desired_running(False)
    execution.lp_auto_configure(dict(budget_usd='39', target_buy_count=5, buy_price_level=2))
    execution.lp_auto_set_desired_running(True)
    return result


def assert_sdk_buys(account, indices):
    assert [post.token_id for post in account.posts] == [_refill_identity(i)[2] for i in indices]
    assert all(post.maker_amount == 7800000 and post.taker_amount == 20000000
               for post in account.posts)


@pytest.mark.parametrize('failure', ['orders_transport', 'orders_pagination',
    'balance_transport', 'position_cost_unknown'])
def test_initial_planning_read_never_invents_current_facts(runtime, failure):
    store, _, account, _, execution, _ = sdk_planning_setup(runtime)
    before = deepcopy(store.lp_sessions())
    sign = account.create_limit_order
    signed, failures = [], []
    def counted_sign(**kwargs):
        signed.append(kwargs)
        return sign(**kwargs)
    account.create_limit_order = counted_sign
    orders, balance = account.list_open_orders, account.get_balance_allowance

    def failed_read(**kwargs):
        runtime.clock[0] += timedelta(seconds=3)
        failures.append(runtime.clock[0])
        raise TimeoutError('initial current account transport unavailable')

    def partial_orders(**kwargs):
        yield _open_order('prefix-only', 'BUY', price='.39', original='20')
        failed_read()

    if failure == 'orders_transport':
        account.list_open_orders = failed_read
    elif failure == 'orders_pagination':
        account.list_open_orders = partial_orders
    elif failure == 'balance_transport':
        account.get_balance_allowance = failed_read
    else:
        account.positions = (_position('8').model_copy(update={'avg_price': None, 'initial_value': None}),)
    scheduler = LPAutoScheduler(execution, clock=lambda: runtime.clock[0])
    assert scheduler.run_due()
    state = execution.lp_auto_state()
    assert signed == account.posts == account.cancels == []
    assert state['active_plan'] is None
    assert store.lp_sessions() == before
    assert state['last_round']['targets'] == []
    assert state['plan_wait']['kind'] == 'api'
    started = datetime.fromisoformat(state['plan_wait']['started_at'])
    deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert deadline == started + timedelta(seconds=60)
    if failure == 'position_cost_unknown':
        assert state['last_round']['reason'] == 'account_position_cost_unknown'
        assert state['funds']['inventory_cost_usd'] is None
    else:
        assert failures == [started]
        assert state['last_round']['reason'] == 'account_order_sync_unknown'
        reads = (account.balance_reads, account.order_reads, account.trade_reads, account.position_reads)
        for elapsed in (1, 59):
            runtime.clock[0] = started + timedelta(seconds=elapsed)
            scheduler.request_check()
            assert not scheduler.run_due()
            assert failures == [started]
            assert reads == (account.balance_reads, account.order_reads, account.trade_reads, account.position_reads)
            assert signed == account.posts == account.cancels == []
    account.list_open_orders, account.get_balance_allowance = orders, balance
    account.positions = ()
    advance_api_wait(runtime, execution)
    assert scheduler.run_due()
    state = execution.lp_auto_state()
    assert_sdk_buys(account, range(1, 6))
    assert len(signed) == 5 and len(store.lp_sessions()) == 5
    assert state['last_round']['completed_at'] and state['active_plan'] is None


@pytest.mark.parametrize('exposure', ['empty', 'inventory', 'orders'])
def test_history_transport_does_not_veto_current_funded_plan(runtime, exposure):
    store, adapter, account, _, execution, _ = sdk_planning_setup(runtime)
    if exposure == 'inventory':
        _, condition, token = _refill_identity(6)
        account.positions = (_position('8').model_copy(update={
            'condition_id': condition, 'token_id': token, 'avg_price': Decimal('.39')}),)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    original = deepcopy(account_packet(store))
    if exposure == 'orders':
        account.orders = tuple(_open_order(f'current-{i}', 'BUY', price='.39', original='20',
            token_id=_refill_identity(i)[2]).model_copy(update={
                'market': _refill_identity(i)[1], 'condition_id': _refill_identity(i)[1]})
            for i in (1, 2))
    history, sign = account.list_account_trades, account.create_limit_order
    history_calls, plans, counts = [], [], []
    baseline = (account.balance_reads, account.order_reads, account.position_reads)

    def unavailable_history(**kwargs):
        history_calls.append(runtime.clock[0])
        raise TimeoutError('only historical trades transport unavailable')

    def observed_sign(**kwargs):
        plans.append(deepcopy(execution.lp_auto_state()['active_plan']))
        counts.append((account.balance_reads, account.order_reads, account.position_reads))
        return sign(**kwargs)

    account.list_account_trades, account.create_limit_order = unavailable_history, observed_sign
    state = execution.lp_auto_run_once(round_id='history-only-round')
    expected = range(1, 5) if exposure == 'inventory' else range(1, 6)
    posted = range(3, 6) if exposure == 'orders' else expected
    assert_sdk_buys(account, posted)
    assert len(history_calls) == 1
    assert (account.balance_reads, account.order_reads, account.position_reads) == tuple(value + 1 for value in baseline)
    assert counts and all(row == tuple(value + 1 for value in baseline) for row in counts)
    assert account.cancels == []
    first = plans[0]
    assert first['round_id'] == state['last_round']['round_id'] == 'history-only-round'
    assert all(plan['round_id'] == first['round_id'] and plan['targets'] == first['targets']
               and [action['action_id'] for action in plan['actions']] == [action['action_id'] for action in first['actions']]
               for plan in plans)
    source = first['approval_account_facts']
    if exposure == 'orders':
        assert [(buy['order_id'], buy['token_id'], buy['side'], Decimal(buy['price']),
                 Decimal(buy['original_quantity']), Decimal(buy['filled_quantity']))
                for buy in source['buys']] == [
            (f'current-{i}', _refill_identity(i)[2], 'BUY', Decimal('.39'), Decimal('20'), Decimal(0))
            for i in (1, 2)]
        assert [row['order_id'] for row in first['targets'][:2]] == ['current-1', 'current-2']
    assert source['report_status'] == 'unknown' and source['realized_pnl_usd'] is None
    assert source['trades_complete'] is False
    assert source['publication_scope'] == 'planning'
    assert source['financial_status'] == 'known'
    assert Decimal(source['inventory_cost_usd']) == (Decimal('3.12') if exposure == 'inventory' else 0)
    assert datetime.fromisoformat(source['read_started_at']) <= datetime.fromisoformat(source['checked_at']) <= datetime.fromisoformat(source['read_ended_at'])
    assert source['snapshot_id'] != original['snapshot_id']
    assert [row['token_id'] for row in first['targets']] == [_refill_identity(i)[2] for i in expected]
    planned = sum((Decimal(row['price']) * Decimal(row['quantity']) for row in first['targets']), Decimal(0))
    assert planned == (Decimal('31.20') if exposure == 'inventory' else Decimal('39.00'))
    assert planned + Decimal(source['inventory_cost_usd']) <= Decimal('39')
    assert account_packet(store) == original
    assert state['funds']['as_of'] == original['checked_at']
    assert state['funds']['status'] == 'unknown'
    assert state['active_plan'] is None and state['last_round']['completed_at']
    assert all(action['state'] == 'success' for action in state['last_round']['actions'])
    assert state['plan_wait']['kind'] == 'round'
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(
        state['last_round']['completed_at']) + timedelta(seconds=60)
    requests = [(session['session_id'], session['idempotency_key'], deepcopy(store.lp_actions(session['session_id'])))
                for session in store.lp_sessions()]
    assert len(requests) == len(expected)
    with pytest.raises(PolymarketTradingError):
        adapter.lp_account_snapshot_shared(max_age_seconds=-1, trade_generation_provider=store.lp_trade_generation)
    assert len(history_calls) == 2
    assert account_packet(store) == original
    account.list_account_trades = history
    runtime.clock[0] += timedelta(seconds=1)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    assert account_packet(store)['checked_at'] != original['checked_at']
    assert_sdk_buys(account, posted)
    if exposure == 'orders':
        recovered = execution.lp_auto_state()
        assert recovered['slots']['occupied'] == 5
        assert Decimal(recovered['funds']['buy_reserved_usd']) == Decimal('39')
        assert [order.id for order in account.orders[:2]] == ['current-1', 'current-2']
    assert [(session['session_id'], session['idempotency_key'], store.lp_actions(session['session_id']))
            for session in store.lp_sessions()] == requests


@pytest.mark.parametrize('failure', ['orders', 'position_cost'])
def test_history_fallback_preserves_current_read_failures(runtime, failure):
    store, adapter, account, _, execution, _ = sdk_planning_setup(runtime)
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    original = deepcopy(account_packet(store))
    sessions = deepcopy(store.lp_sessions())
    history, orders, sign = account.list_account_trades, account.list_open_orders, account.create_limit_order
    signed = []

    def unavailable_history(**kwargs):
        raise TimeoutError('historical transport still unavailable')

    def unavailable_orders(**kwargs):
        runtime.clock[0] += timedelta(seconds=3)
        raise TimeoutError('current orders still unavailable')

    def counted_sign(**kwargs):
        signed.append(kwargs)
        return sign(**kwargs)

    account.list_account_trades, account.create_limit_order = unavailable_history, counted_sign
    if failure == 'orders':
        account.list_open_orders = unavailable_orders
    else:
        account.positions = (_position('8').model_copy(update={'avg_price': None, 'initial_value': None}),)
    state = execution.lp_auto_run_once()
    assert signed == account.posts == account.cancels == []
    assert state['active_plan'] is None and state['last_round']['targets'] == []
    assert state['last_round']['reason'] == ('account_order_sync_unknown' if failure == 'orders' else 'account_position_cost_unknown')
    assert account_packet(store) == original
    assert store.lp_sessions() == sessions
    with pytest.raises(PolymarketTradingError):
        adapter.lp_account_snapshot_shared(max_age_seconds=-1, trade_generation_provider=store.lp_trade_generation)
    account.list_account_trades = history
    advance_api_wait(runtime, execution, refresh=False)
    state = execution.lp_auto_run_once()
    assert signed == account.posts == account.cancels == []
    assert state['active_plan'] is None and state['last_round']['targets'] == []
    assert store.lp_sessions() == sessions
    assert state['last_round']['reason'] == ('account_order_sync_unknown' if failure == 'orders' else 'account_position_cost_unknown')
    account.list_open_orders, account.positions = orders, ()
    advance_api_wait(runtime, execution)
    state = execution.lp_auto_run_once()
    assert_sdk_buys(account, range(1, 6))
    assert len(signed) == 5 and len(store.lp_sessions()) == 5
    assert state['last_round']['completed_at'] and state['active_plan'] is None
