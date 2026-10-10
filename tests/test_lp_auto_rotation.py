"""Automatic BUYs converge to the highest-yield eligible markets, offline."""

from tests.test_lp_auto_pool import advance_auto_wait
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import threading
from types import SimpleNamespace

import pytest

from tests import test_lp_auto_pool as pool


class RotationExchange(pool.Exchange):
    def __init__(self):
        super().__init__()
        self.rewards = {}
        self.cancels = []

    def direction(self, token):
        direction = super().direction(token)
        direction['daily_pool_usd'] = self.rewards.get(token, Decimal('24'))
        # Public depth includes the account's live orders.
        own = sum((Decimal(str(o['original_size'])) - Decimal(str(o['size_matched']))
                   for o in self.orders if o['token_id'] == token
                   and o['side'] == 'BUY' and o['status'] == 'LIVE'), Decimal(0))
        direction['book']['bids'][0]['size'] = str(Decimal('1000') + own)
        return direction

    def lp_reward_catalog(self, **kwargs):
        result = super().lp_reward_catalog(**kwargs)
        for row in result['markets']:
            row['daily_pool_usd'] = self.rewards.get(row['condition_id'], Decimal('24'))
        return result

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        # A cancel acknowledgment deliberately does not manufacture a terminal receipt.
        return {'canceled': [order_id]}


def setup(tmp_path, monkeypatch, count=10, target=5, budget='100'):
    monkeypatch.setattr(pool, 'Exchange', RotationExchange)
    engine, exchange, lp, store = pool.setup(tmp_path, count)
    engine.lp_auto_configure(dict(budget_usd=budget, target_buy_count=target))
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once()
    return engine, exchange, lp, store


def refresh(lp, exchange, count=10):
    for index in range(count):
        token = f'm{index:02}'
        lp._candidate_pool_record_success(token, dict(condition_id=token), judged_at=pool.NOW,
            facts=dict(directions=[exchange.direction(token)], account=exchange.lp_account_snapshot()))


def test_full_pool_replaces_every_market_outside_top_five_without_threshold(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    for index in range(5, 10):
        exchange.rewards[f'm{index:02}'] = Decimal('24.000001')
    refresh(lp, exchange)

    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert set(exchange.cancels) == {'o1', 'o2', 'o3', 'o4', 'o5'}
    assert len(exchange.posts) == 5
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    engine.lp_auto_run_once()
    assert len(exchange.cancels) == 5
    assert len(exchange.posts) == 5

    for order in exchange.orders:
        order['status'] = 'CANCELED'
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert {o['token_id'] for o in exchange.orders if o['status'] == 'LIVE'} == {
        'm05', 'm06', 'm07', 'm08', 'm09'}
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40


def test_rotation_waits_for_first_real_shared_read_and_recovers_without_overbuying(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    engine.lp_auto_reconcile_unknown()

    slow_entered, fast_entered = threading.Event(), threading.Event()
    slow_release, fast_release = threading.Event(), threading.Event()
    metadata = exchange.lp_market_metadata_fresh
    def blocked_metadata(ids, **kwargs):
        values = {str(value) for value in ids}
        if 'm10' in values:
            slow_entered.set()
            assert slow_release.wait(5)
        if 'm11' in values:
            fast_entered.set()
            assert fast_release.wait(5)
        return metadata(ids, **kwargs)
    exchange.lp_market_metadata_fresh = blocked_metadata

    def read(identity):
        try:
            lp._read_candidate_facts(identity)
        except ValueError:
            pass
    slow = threading.Thread(target=read, args=(dict(condition_id='m10', token_id='m10', outcome='YES'),))
    slow.start()
    assert slow_entered.wait(2)
    fast = threading.Thread(target=read, args=(dict(condition_id='m11', token_id='m11', outcome='YES'),))
    fast.start()
    assert fast_entered.wait(2)
    lp._market_read_timeout = 1

    from open_trader import polymarket_lp as lp_module
    original_wait = lp_module.wait
    wait_entered = threading.Event()
    def observed_wait(futures, **kwargs):
        wait_entered.set()
        return original_wait(futures, **kwargs)
    monkeypatch.setattr(lp_module, 'wait', observed_wait)

    round_thread = None
    try:
        state = engine.lp_auto_scheduled_check()
        assert state['last_round']['reason'] == 'market_read_capacity'
        assert [row['condition_id'] for row in state['last_round']['blocked']] == ['m00']
        assert exchange.cancels == []
        assert len(exchange.posts) == 1

        advance_auto_wait(engine, monkeypatch)
        engine.lp_auto_reconcile_unknown()
        published = lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))
        assert published['state'] == 'registered', published
        account_reads = []
        def complete_account_round(*, max_age_seconds=0, trade_generation_provider=None):
            del max_age_seconds
            pool.NOW += timedelta(microseconds=1)
            snapshot = pool._fresh_registration_bundle(exchange, lp)
            if trade_generation_provider is not None:
                snapshot['trade_generation'] = trade_generation_provider()
            assert snapshot['trade_generation'] == lp.store.lp_trade_generation()
            account_reads.append(snapshot)
            return snapshot
        exchange.lp_account_snapshot_shared = complete_account_round
        wait_entered.clear()
        checked = []
        def check():
            checked.append(engine.lp_auto_scheduled_check())
        round_thread = threading.Thread(target=check)
        round_thread.start()
        assert wait_entered.wait(2), str([x['last_round'] for x in checked])
        assert slow.is_alive() and fast.is_alive()
        assert exchange.cancels == [] and len(exchange.posts) == 1
        fast_release.set()
        round_thread.join(5)
        assert checked
        state = checked[0]
        assert exchange.cancels == ['o1']
        assert len(exchange.posts) == 1
        assert state['slots']['occupied'] == 1
        assert state['funds']['status'] == 'unknown'
        assert state['funds']['available_usd'] is None
        assert state['last_round']['reason'] == 'rotation_awaiting_reconciliation'
    finally:
        lp._market_read_timeout = 10
        fast_release.set()
        slow_release.set()
        slow.join(2)
        fast.join(2)
        if round_thread is not None:
            round_thread.join(2)
            assert not round_thread.is_alive()
        assert not slow.is_alive() and not fast.is_alive()

    for order in exchange.orders:
        order['status'] = 'CANCELED'
    advance_auto_wait(engine, monkeypatch)
    reads_before_refill = len(account_reads)
    state = engine.lp_auto_scheduled_check()
    assert {o['token_id'] for o in exchange.orders if o['status'] == 'LIVE'} == {'m01'}
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    refill_reads = account_reads[reads_before_refill:]
    assert len(refill_reads) >= 3  # Authoritative absence and both fresh BUY fences.
    assert all(later['checked_at'] > earlier['checked_at']
               for earlier, later in zip(refill_reads, refill_reads[1:]))


def test_rotation_reports_exact_block_when_financial_facts_expire(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    intent = engine.lp_auto_state()['intents'][0]
    intent_id = intent['intent_id']
    engine._auto_pool._update(lambda d: d['intents'][intent_id].update(
        checked_at=(pool.NOW - timedelta(seconds=61)).isoformat()))
    engine._auto_pool._reconcile_unknown = lambda **kwargs: engine._auto_pool.state()

    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert exchange.cancels == []
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert state['last_round']['reason'] == 'financial_facts_stale'
    assert state['last_round']['blocked'] == [
        {'condition_id': 'm00', 'token_id': 'm00', 'reason': 'financial_facts_stale'}]


def test_rotation_keeps_unknown_occupied_slots_blocked_and_ranks_known_actives(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=3, target=2)
    exchange.rewards['m02'] = Decimal('25')
    refresh(lp, exchange, 3)
    intents = engine.lp_auto_state()['intents']
    engine._auto_pool._update(lambda d: d['intents'][intents[0]['intent_id']].update(
        state='unknown', financial_status='unknown', reconcile_reason='missing_reliable_order_id'))
    targets, _, _, blocked = engine._auto_pool._ranked_buys(engine._auto_pool.state())
    assert blocked == [{'condition_id': 'm00', 'token_id': 'm00', 'reason': 'missing_reliable_order_id'}]
    assert [t['condition_id'] for t in targets] == ['m00', 'm02']
    assert targets[0]['retained_constraint'] is True

    def all_unknown(document):
        for intent in document['intents'].values():
            intent.update(state='unknown', financial_status='unknown',
                          reconcile_reason='missing_reliable_order_id')
    engine._auto_pool._update(all_unknown)
    engine._auto_pool._reconcile_unknown = lambda **kwargs: engine._auto_pool.state()
    advance_auto_wait(engine, monkeypatch, refresh=False)
    state = engine.lp_auto_run_once()
    assert len(exchange.posts) == 2
    assert state['slots']['occupied'] == 2
    assert state['last_round']['reason'] == 'missing_reliable_order_id'
    assert [row['reason'] for row in state['last_round']['blocked']] == [
        'missing_reliable_order_id', 'missing_reliable_order_id']


def test_rotation_registers_all_victims_and_isolates_unresolved_terminal(tmp_path, monkeypatch):
    engine, exchange, lp, store = setup(tmp_path, monkeypatch)
    advance_auto_wait(engine, monkeypatch)
    intents = engine.lp_auto_state()['intents']
    revisions = {i['session_id']: store.lp_session_revision(i['session_id'], trading=True) for i in intents}
    for index in range(5, 10):
        exchange.rewards[f'm{index:02}'] = Decimal('25')
    refresh(lp, exchange)
    observed = []
    cancel = exchange.cancel_order

    def unlocked():
        lock = engine._acquire_global_lock()
        if lock is None:
            return False
        acquired = lp._mutex.acquire(blocking=False)
        if acquired:
            lp._mutex.release()
        engine._release_global_lock(lock)
        return acquired

    def cancel_after_registration(order_id):
        if not observed:
            observed.append(engine.lp_auto_state())
            observed.append({i['order_id'] for i in intents for a in store.lp_actions(i['session_id'])
                             if a.get('role') == 'reconciliation_cancel' and a['state'] == 'pending'})
            observed.append(all(store.lp_session_revision(sid, trading=True) > rev for sid, rev in revisions.items()))
            with ThreadPoolExecutor(1) as workers:
                observed.append(workers.submit(unlocked).result(timeout=2))
        return cancel(order_id)

    exchange.cancel_order = cancel_after_registration
    engine.lp_auto_run_once()
    assert observed[1] == {'o1', 'o2', 'o3', 'o4', 'o5'}
    assert observed[2:] == [True, True]
    assert observed[0]['funds']['status'] == 'unknown'
    assert observed[0]['slots']['canceling'] == 5
    assert Decimal(observed[0]['funds']['buy_reserved_usd']) == 40
    assert exchange.cancels == ['o1', 'o2', 'o3', 'o4', 'o5']

    for order in exchange.orders[:4]:
        order['status'] = 'CANCELED'
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert len(exchange.posts) == 9
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert exchange.orders[4]['status'] == 'LIVE'
    exchange.orders[4]['status'] = 'CANCELED'
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert len(exchange.posts) == 10
    assert {o['token_id'] for o in exchange.orders if o['status'] == 'LIVE'} == {
        'm05', 'm06', 'm07', 'm08', 'm09'}
    assert state['funds']['status'] == 'known'


def test_second_cancel_registration_failure_sends_nothing_and_recovers_safely(tmp_path, monkeypatch):
    import sqlite3

    engine, exchange, lp, store = setup(tmp_path, monkeypatch)
    for index in range(5, 10):
        exchange.rewards[f'm{index:02}'] = Decimal('25')
    monkeypatch.setattr(pool, 'NOW', pool._maybe_datetime(engine.lp_auto_state()['plan_wait']['deadline']))
    refresh(lp, exchange)
    upsert = store.lp_upsert_action
    registrations = []
    def fail_second(session_id, key, **kwargs):
        if kwargs['payload'].get('role') == 'reconciliation_cancel':
            registrations.append(session_id)
            if len(registrations) == 2:
                raise sqlite3.OperationalError('injected second registration failure')
        return upsert(session_id, key, **kwargs)
    monkeypatch.setattr(store, 'lp_upsert_action', fail_second)
    with pytest.raises(sqlite3.OperationalError, match='second registration'):
        engine.lp_auto_run_once()
    state = engine.lp_auto_state()
    assert exchange.cancels == []
    assert len(exchange.posts) == 5
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['available_usd'] is None
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert all(i['state'] == 'canceling' for i in state['intents'])
    monkeypatch.setattr(store, 'lp_upsert_action', upsert)
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
    state = engine.lp_auto_run_once()
    assert exchange.cancels == ['o1', 'o2', 'o3', 'o4', 'o5']
    assert len(exchange.posts) == 5
    assert state['funds']['status'] == 'unknown'
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    for order in exchange.orders:
        order['status'] = 'CANCELED'
    state = engine.lp_auto_reconcile_unknown()
    assert state['funds']['status'] == 'known'
    assert Decimal(state['funds']['buy_reserved_usd']) == 0
    assert len(exchange.posts) == 5


def test_partial_fill_during_cancel_aborts_replacement_and_keeps_inventory_cost(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1)
    exchange.orders.append(dict(order_id='self-managed-sell', token_id='legacy', condition_id='legacy',
        side='SELL', status='LIVE', price='.30', original_size='13.31', size_matched='0'))
    exchange.rewards['m01'] = Decimal('25')
    assert engine.lp_auto_run_once()['last_round']['completed_at']
    monkeypatch.setattr(pool, 'NOW', pool._maybe_datetime(engine.lp_auto_state()['plan_wait']['deadline']))
    refresh(lp, exchange, 2)
    engine.lp_auto_run_once()
    exchange.orders[0].update(status='CANCELED', size_matched='8')
    exchange.positions = [dict(token_id='m00', condition_id='m00', size='8')]
    monkeypatch.setattr(pool, 'NOW', pool._maybe_datetime(engine.lp_auto_state()['plan_wait']['deadline']))
    state = engine.lp_auto_run_once()
    assert exchange.cancels == ['o1']
    assert len(exchange.posts) == 1
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.2')
    assert Decimal(state['funds']['available_usd']) == Decimal('96.8')
    assert state['last_round']['reason'] == 'rotation_filled'


@pytest.mark.parametrize('guard', ['stale_reward', 'stale_book', 'incomplete_account', 'protection', 'pause', 'breaker', 'review', 'unknown_order'])
def test_rotation_keeps_existing_buy_when_guard_blocks(tmp_path, monkeypatch, guard):
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    session_id = engine.lp_auto_state()['intents'][0]['session_id']
    if guard == 'stale_reward':
        read = exchange.lp_reward_catalog
        def stale(**kwargs):
            result = read(**kwargs)
            for row in result['markets']:
                row['reward_checked_at'] = pool.NOW - timedelta(seconds=61)
            return result
        exchange.lp_reward_catalog = stale
    elif guard == 'stale_book':
        read = exchange.lp_order_books
        def stale(ids, **kwargs):
            result = read(ids, **kwargs)
            for book in result.values():
                book['received_at'] = pool.NOW - timedelta(seconds=61)
            return result
        exchange.lp_order_books = stale
    elif guard == 'incomplete_account':
        read = exchange.lp_account_snapshot
        exchange.lp_account_snapshot = lambda: {**read(), 'open_orders_complete': False}
    elif guard == 'protection':
        session = store.lp_session(session_id)
        protection = session['queue_protection']
        for bucket in protection.get('levels', {'entry': protection}).values():
            bucket['state'] = 'triggered'
        store.lp_update_session(session_id, patch={'queue_protection': protection})
    elif guard in ('pause', 'breaker'):
        read = exchange.lp_reward_catalog
        def interrupt(**kwargs):
            if guard == 'pause':
                engine.lp_auto_set_desired_running(False)
            else:
                engine._breaker_open = True
            return read(**kwargs)
        exchange.lp_reward_catalog = interrupt
    elif guard == 'review':
        store.lp_update_session(session_id, patch={'review_at': pool.NOW.isoformat()})
    else:
        exchange.orders[0]['status'] = 'UNKNOWN'

    early = engine.lp_auto_run_once()
    assert exchange.cancels == [] and len(exchange.posts) == 1
    monkeypatch.setattr(pool, 'NOW', pool._maybe_datetime(early['plan_wait']['deadline']))
    state = engine.lp_auto_run_once()
    assert exchange.cancels == []
    assert len(exchange.posts) == 1
    if guard == 'stale_reward':
        assert state['last_round']['reason'] == 'rotation_yield_unknown'
        assert state['last_round']['blocked'] == [
            {'condition_id': 'm00', 'token_id': 'm00', 'reason': 'rotation_yield_unknown'}]


def test_equal_yields_keep_resting_orders_without_counting_them_as_competition(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch)
    refresh(lp, exchange)
    for _ in range(2):
        engine.lp_auto_run_once()
    assert exchange.cancels == []
    assert len(exchange.posts) == 5


def test_cached_leader_is_reestimated_before_any_cancel(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    exchange.rewards['m01'] = Decimal('23')
    engine.lp_auto_run_once()
    assert exchange.cancels == []
    assert len(exchange.posts) == 1


def test_cancel_timeout_survives_restart_and_reselects_latest_leader_after_terminal(tmp_path, monkeypatch):
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=3, target=1)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 3)
    def timeout(order_id):
        exchange.cancels.append(order_id)
        raise TimeoutError()
    exchange.cancel_order = timeout
    engine.lp_auto_run_once()
    restarted = pool.PredictionExecutionService(store=store, monitor=SimpleNamespace(), trading=exchange,
        notifier=SimpleNamespace(), lock_path=tmp_path/'execution.lock', lp=lp)
    restarted._breaker_open = False
    state = restarted.lp_auto_run_once()
    assert exchange.cancels == ['o1']
    assert len(exchange.posts) == 1
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    exchange.orders[0]['status'] = 'CANCELED'
    exchange.rewards['m02'] = Decimal('26')
    refresh(lp, exchange, 3)
    advance_auto_wait(restarted, monkeypatch)
    restarted.lp_auto_run_once()
    assert [o['token_id'] for o in exchange.orders if o['status'] == 'LIVE'] == ['m01']
    advance_auto_wait(restarted, monkeypatch)
    restarted.lp_auto_run_once()
    assert exchange.cancels[-1] == exchange.orders[-1]['order_id']
    exchange.orders[-1]['status'] = 'CANCELED'
    advance_auto_wait(restarted, monkeypatch)
    restarted.lp_auto_run_once()
    assert [o['token_id'] for o in exchange.orders if o['status'] == 'LIVE'] == ['m02']


def test_unaffordable_best_candidate_keeps_affordable_resting_buy(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1, budget='8')
    advance_auto_wait(engine, monkeypatch)
    direction = exchange.direction
    def larger(token):
        result = direction(token)
        if token == 'm01':
            result['market']['reward_min_size'] = Decimal('40')
            result['market']['minimum_order_size'] = Decimal('40')
        return result
    exchange.direction = larger
    exchange.rewards['m01'] = Decimal('100')
    refresh(lp, exchange, 2)
    resting_buy = exchange.orders[0].copy()
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert state['last_round']['reason'] == 'target_filled'
    assert [row['condition_id'] for row in state['last_round']['targets']] == ['m00']
    assert state['last_round']['blocked'] == [
        {'condition_id': 'm01', 'token_id': 'm01', 'reason': 'rotation_budget_insufficient'}]
    assert exchange.cancels == []
    assert len(exchange.posts) == 1
    assert exchange.orders == [resting_buy]
    assert state['slots']['occupied'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    assert Decimal(state['funds']['available_usd']) == 0


@pytest.mark.parametrize('change', ['observation', 'fill', 'stop', 'protection'])
def test_concurrent_session_changes_are_checked_by_trading_facts(tmp_path, monkeypatch, change):
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    session_id = engine.lp_auto_state()['intents'][0]['session_id']
    read = exchange.lp_reward_catalog
    def update_during_read(**kwargs):
        if change == 'observation':
            patch = {'reward_status': 'known'}
        elif change == 'fill':
            patch = {'buy_filled_quantity': '8'}
        elif change == 'stop':
            patch = {'stop_requested': True}
        else:
            protection = store.lp_session(session_id)['queue_protection']
            for bucket in protection.get('levels', {'entry': protection}).values():
                bucket['state'] = 'triggered'
            patch = {'queue_protection': protection}
        store.lp_update_session(session_id, patch=patch)
        return read(**kwargs)
    exchange.lp_reward_catalog = update_during_read
    engine.lp_auto_run_once()
    assert exchange.cancels == (['o1'] if change == 'observation' else [])
    assert len(exchange.posts) == 1


def test_rotation_waits_when_replacement_cannot_meet_existing_gtd_minimum(tmp_path, monkeypatch):
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    session = store.lp_session(engine.lp_auto_state()['intents'][0]['session_id'])
    deadline = pool.datetime.fromisoformat(session['review_at'])
    monkeypatch.setattr(pool, 'NOW', deadline - timedelta(seconds=60))
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    engine.lp_auto_run_once()
    assert exchange.cancels == []
    assert len(exchange.posts) == 1


@pytest.mark.parametrize('change', ['size', 'price'])
def test_resting_buy_that_no_longer_scores_has_zero_estimated_yield(tmp_path, monkeypatch, change):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1)
    advance_auto_wait(engine, monkeypatch)
    direction = exchange.direction
    def changed(token):
        result = direction(token)
        if token == 'm00':
            if change == 'size':
                result['market']['reward_min_size'] = Decimal('40')
            else:
                result['book']['bids'] = [dict(price='.52', size='1000'),
                    dict(price='.51', size='1000'), dict(price='.40', size='20')]
                result['book']['asks'] = [dict(price='.53', size='1000')]
        return result
    exchange.direction = changed
    catalog = exchange.lp_reward_catalog
    def rewards(**kwargs):
        result = catalog(**kwargs)
        if change == 'size':
            for row in result['markets']:
                if row['condition_id'] == 'm00':
                    row['rewards_min_size'] = '40'
        return result
    exchange.lp_reward_catalog = rewards
    refresh(lp, exchange, 2)
    engine.lp_auto_run_once()
    assert exchange.cancels == ['o1']


@pytest.mark.parametrize('failure', ['timeout', 'denied'])
def test_failed_cancel_retries_only_after_a_new_known_live_receipt(tmp_path, monkeypatch, failure):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    def timeout(order_id):
        exchange.cancels.append(order_id)
        if failure == 'timeout':
            raise TimeoutError()
        return {'not_canceled': {order_id: 'temporary failure'}}
    exchange.cancel_order = timeout
    engine.lp_auto_run_once()
    engine.lp_auto_run_once()
    assert exchange.cancels == ['o1']
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
    exchange.cancel_order = lambda oid: (exchange.cancels.append(oid) or {'canceled': [oid]})
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert exchange.cancels == ['o1', 'o1']
    assert len(exchange.posts) == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
    engine.lp_auto_run_once()
    assert exchange.cancels == ['o1', 'o1', 'o1']  # New LIVE facts warrant an exact recovery retry.


@pytest.mark.parametrize('receipt', ['unknown', 'incomplete', 'stale', 'terminal', 'partial'])
def test_cancel_recovery_uses_known_owned_receipts_and_preserves_fills(tmp_path, monkeypatch, receipt):
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    exchange.cancel_order = lambda oid: (exchange.cancels.append(oid) or {})
    engine.lp_auto_run_once()
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
    account = exchange.lp_account_snapshot
    if receipt == 'unknown':
        exchange.orders[0]['status'] = 'UNKNOWN'
    elif receipt == 'incomplete':
        exchange.lp_account_snapshot = lambda: {**account(), 'open_orders_complete': False}
    elif receipt == 'stale':
        exchange.lp_account_snapshot = lambda: {**account(), 'checked_at': pool.NOW - timedelta(seconds=61)}
    elif receipt == 'terminal':
        exchange.orders[0]['status'] = 'CANCELED'
    else:
        exchange.orders[0]['size_matched'] = '8'
        exchange.positions = [dict(token_id='m00', condition_id='m00', size='8')]
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert exchange.cancels == (['o1', 'o1'] if receipt == 'partial' else ['o1'])
    if receipt == 'terminal':
        assert state['slots']['occupied'] == 1
        assert len(exchange.posts) == 2
    else:
        assert len(exchange.posts) == 1
        assert state['slots']['occupied'] == 1
        assert Decimal(state['funds']['buy_reserved_usd']) == 8
        assert state['funds']['status'] == 'unknown'
        assert state['funds']['available_usd'] is None
    if receipt in ('unknown', 'incomplete', 'stale'):
        exchange.orders[0]['status'] = 'LIVE'
        exchange.lp_account_snapshot = account
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
        engine.lp_auto_run_once()
        assert exchange.cancels == ['o1', 'o1']
    if receipt == 'partial':
        session = store.lp_session(state['intents'][0]['session_id'])
        assert Decimal(session['buy_filled_quantity']) == 8
        assert Decimal(session['buy_cost']) == Decimal('3.2')
        assert Decimal(session['residual_quantity']) == 8
        exchange.orders[0]['status'] = 'CANCELED'
        advance_auto_wait(engine, monkeypatch)
        state = engine.lp_auto_run_once()
        assert state['last_round']['reason'] == 'rotation_filled'
        assert state['funds']['status'] == 'known'
        assert Decimal(state['funds']['buy_reserved_usd']) == 0
        assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.2')
        assert Decimal(state['funds']['available_usd']) == Decimal('96.8')
        assert len(exchange.posts) == 1


@pytest.mark.parametrize('balance,expected_cancels', [('20', ['o1', 'o2']), ('24', ['o2'])])
def test_rotation_budget_counts_manual_and_retained_buys_once(tmp_path, monkeypatch, balance, expected_cancels):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=3, target=2)
    advance_auto_wait(engine, monkeypatch)
    manual = dict(order_id='manual-buy', token_id='manual', condition_id='manual',
                  side='BUY', status='LIVE', price='.40', original_size='20', size_matched='0')
    exchange.orders.append(manual.copy())
    account = exchange.lp_account_snapshot
    exchange.lp_account_snapshot = lambda: {**account(), 'balance': balance, 'allowance': balance}
    exchange.rewards['m02'] = Decimal('25')
    refresh(lp, exchange, 3)
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert exchange.cancels == expected_cancels
    assert exchange.orders[-1] == manual
    assert len(exchange.posts) == 2
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    expected_targets = ['m02'] if balance == '20' else ['m02', 'm00']
    assert [row['condition_id'] for row in state['last_round']['targets']] == expected_targets
    assert len(state['last_round']['targets']) == len(expected_targets)
    assert state['last_round']['reason'] == 'rotation_awaiting_reconciliation'
    if balance == '20':
        assert state['last_round']['blocked'] == [
            {'condition_id': 'm00', 'token_id': 'm00', 'reason': 'rotation_budget_insufficient'},
            {'condition_id': 'm01', 'token_id': 'm01', 'reason': 'rotation_budget_insufficient'}]


def test_full_candidate_count_and_preview_are_not_truncated_to_target_count(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=18)
    advance_auto_wait(engine, monkeypatch)
    refresh(lp, exchange, 18)
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert state['last_round']['candidate_count'] == 13
    assert len(state['last_round']['candidates']) == 10
    assert {r['condition_id'] for r in state['last_round']['targets']} == {
        'm00', 'm01', 'm02', 'm03', 'm04'}


@pytest.mark.parametrize('raw_facts', ['complete', 'missing_quantity', 'invalid_position', 'read_failure', 'stale', 'unknown', 'partial'])
def test_rotation_retry_uses_real_adapter_account_completeness(tmp_path, monkeypatch, raw_facts):
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig

    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=2, target=1)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    exchange.cancel_order = lambda oid: (exchange.cancels.append(oid) or {})
    engine.lp_auto_run_once()
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
    def read_account():
        if raw_facts == 'read_failure':
            raise OSError('account read failed')
        orders = [dict(o) for o in exchange.orders]
        if raw_facts == 'missing_quantity':
            orders[0].pop('size_matched')
        positions = [dict(token_id='m00')] if raw_facts == 'invalid_position' else []
        if raw_facts == 'unknown':
            orders[0]['status'] = 'UNKNOWN'
        if raw_facts == 'partial':
            orders[0]['size_matched'] = '8'
            positions = [dict(token_id='m00', condition_id='m00', size='8')]
        checked_at = pool.NOW - timedelta(seconds=61) if raw_facts == 'stale' else pool.NOW
        return Decimal('1000'), Decimal('1000'), orders, positions, checked_at, (), True

    adapter = PolymarketTradingClient(TradingConfig('test-signer', 'test-wallet'), SimpleNamespace())
    adapter._account_read_facts = read_account
    adapter.lp_market_metadata = lambda ids: {}
    # Real production normalization and completeness flags; only transport is fake.
    exchange.lp_account_snapshot = adapter.lp_account_snapshot
    advance_auto_wait(engine, monkeypatch, refresh=False)
    state = engine.lp_auto_run_once()
    assert exchange.cancels == (['o1', 'o1'] if raw_facts in ('complete', 'partial') else ['o1'])
    assert len(exchange.posts) == 1
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['available_usd'] is None
    assert Decimal(state['funds']['buy_reserved_usd']) == 8


def test_cancel_recovery_waits_when_session_is_missing(tmp_path, monkeypatch):
    engine, exchange, lp, store = setup(tmp_path, monkeypatch, count=2, target=1)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    refresh(lp, exchange, 2)
    exchange.cancel_order = lambda oid: (exchange.cancels.append(oid) or {})
    engine.lp_auto_run_once()
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
    monkeypatch.setattr(store, 'lp_session', lambda session_id: None)
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert 'financial_facts_unknown' in state['block_reasons']
    assert state['funds']['available_usd'] is None
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    assert state['slots']['occupied'] == 1
    assert state['intents'][0]['reconcile_reason'] == 'rotation_session_missing'
    assert exchange.cancels == ['o1']
    assert len(exchange.posts) == 1


def test_failed_market_does_not_block_healthy_market_rotation(tmp_path, monkeypatch):
    engine, exchange, lp, _ = setup(tmp_path, monkeypatch, count=3, target=2)
    advance_auto_wait(engine, monkeypatch)
    exchange.rewards['m02'] = Decimal('30')
    refresh(lp, exchange, 3)
    read = exchange.lp_snapshot

    def unavailable(request):
        if request['token_id'] == 'm00':
            raise ValueError('order_read_failed')
        return read(request)

    exchange.lp_snapshot = unavailable
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert exchange.cancels == ['o2'], 'only the healthy market may rotate'
    assert len(exchange.posts) == 2
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    exchange.orders[1]['status'] = 'CANCELED'
    advance_auto_wait(engine, monkeypatch)
    state = engine.lp_auto_run_once()
    assert [p['token_id'] for p in exchange.posts] == ['m00', 'm01', 'm02']
    assert state['slots']['occupied'] == 2
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
