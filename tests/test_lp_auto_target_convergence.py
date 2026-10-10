"""Issue 322 public-entry contracts, using only a simulated exchange."""
from datetime import timedelta
from datetime import datetime
from decimal import Decimal

import pytest

from tests import test_lp_auto_pool as pool
from tests.test_lp_auto_rotation import RotationExchange


REWARDS = dict(A=12, B=120, C=96, D=6, E=72, F=48, G=24, H=18, I=3)


class PlanExchange(RotationExchange):
    def __init__(self):
        super().__init__()
        self.rewards = {key: Decimal(value) for key, value in REWARDS.items()}
        self.cancel_terminal = True
        self.account_failure = False
        self.reads = []

    def direction(self, token):
        direction = super().direction(token)
        # At buy-two, public depth includes each actual resting BUY.
        direction['book']['bids'][0]['size'] = '1000'
        own = sum((Decimal(str(o['original_size'])) - Decimal(str(o['size_matched']))
                   for o in self.orders if o['token_id'] == token and o['status'] == 'LIVE'), Decimal(0))
        direction['book']['bids'][1]['size'] = str(Decimal(1000) + own)
        return direction

    def lp_account_snapshot(self):
        if self.account_failure:
            raise TimeoutError('simulated account API failure')
        result = super().lp_account_snapshot()
        result['open_orders'] = [o for o in self.orders if o['status'] == 'LIVE']
        return result

    def lp_account_snapshot_shared(self, **kwargs):
        pool.NOW += timedelta(microseconds=1)
        self.reads.append(pool.NOW)
        return pool._fresh_registration_bundle(self, self.lp)

    def cancel_order(self, order_id):
        result = super().cancel_order(order_id)
        if self.cancel_terminal:
            next(o for o in self.orders if o['order_id'] == order_id)['status'] = 'CANCELED'
        return result

    def lp_post_order(self, signed):
        result = super().lp_post_order(signed)
        result.update(market_id=signed['token_id'], outcome='YES')
        return result


def publish_candidates(exchange, lp, store, tokens):
    for token in tokens:
        lp._candidate_pool_record_success(token, dict(condition_id=token), judged_at=pool.NOW,
            facts=dict(directions=[exchange.direction(token)], account=exchange.lp_account_snapshot()))
        store.lp_save_price_history(token, token, [], dict(state='known', amplitude=Decimal('.005'),
            checked_at=pool.NOW, valid_until=pool.NOW + timedelta(days=1)))


def plan_setup(tmp_path, monkeypatch, initial=(), *, budget='100', tokens=None):
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    monkeypatch.setattr(pool, 'Exchange', PlanExchange)
    engine, exchange, lp, store = pool.setup(tmp_path, 0)
    exchange.lp = lp
    engine.lp_auto_configure(dict(budget_usd=budget, target_buy_count=5, buy_price_level=2))
    exchange.orders = [dict(order_id=f'original-{key}', condition_id=key, token_id=key,
        market_id=key, outcome='YES', side='BUY', status='LIVE', price='.39', original_size='20', size_matched='0') for key in initial]
    publish_candidates(exchange, lp, store, REWARDS if tokens is None else tokens)
    engine.lp_auto_set_desired_running(True)
    return engine, exchange, lp, store


def live_orders(exchange):
    return {o['token_id']: o['order_id'] for o in exchange.orders if o['status'] == 'LIVE'}


@pytest.mark.parametrize('initial', [(), ('A', 'B', 'C'), ('A', 'B', 'C', 'D'), ('A', 'B', 'C', 'D', 'I')],
                         ids=['zero', 'three', 'four', 'five'])
def test_unified_selection_for_zero_three_four_five_buys(tmp_path, monkeypatch, initial):
    engine, exchange, _, _ = plan_setup(tmp_path, monkeypatch, initial)
    state = engine.lp_auto_run_once()
    assert {r['condition_id'] for r in state['last_round']['targets']} == {'B', 'C', 'E', 'F', 'G'}
    # Confirm the simulated venue's cancels, then resume through the public path.
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=10))
    state = engine.lp_auto_run_once()
    assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}, state['last_round']
    for survivor in set(initial) & {'B', 'C'}:
        assert live_orders(exchange)[survivor] == f'original-{survivor}'
    assert set(exchange.cancels) == {f'original-{key}' for key in initial if key not in {'B', 'C'}}
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('39.00')


@pytest.mark.parametrize('protection', ['monitoring', 'triggered', 'all-triggered'])
def test_triggered_protection_retains_order_but_monitoring_does_not(tmp_path, monkeypatch, protection):
    tokens = ('A', 'B', 'C', 'D', 'I') if protection == 'all-triggered' else ('A', 'B', 'C', 'D')
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, tokens=tokens)
    engine.lp_auto_run_once()
    original = live_orders(exchange)
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
    direction = exchange.direction
    if protection != 'monitoring':
        def depleted(token):
            result = direction(token)
            if token == 'A' or protection == 'all-triggered' and token in original:
                result['book']['bids'][1]['size'] = '3000'
            return result
        exchange.direction = depleted
        exchange.cancel_terminal = False
    engine.lp_tick()
    if protection == 'all-triggered':
        # Tick drains bounded observation lanes; advance their public clock
        # between passes so all five genuine protection episodes are consumed.
        for _ in range(3):
            monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
            engine.lp_tick()
    session = next(s for s in store.lp_sessions() if s['token_id'] == 'A')
    states = {b['state'] for b in session['queue_protection']['levels'].values()}
    assert states & ({'triggered', 'canceling', 'cancel_unknown'} if protection != 'monitoring' else {'registered', 'monitoring'}), session
    if protection == 'all-triggered':
        for session in store.lp_sessions():
            states = {b['state'] for b in session['queue_protection']['levels'].values()}
            assert states & {'triggered', 'canceling', 'cancel_unknown'}, session
        protection_cancels = list(exchange.cancels)
        assert set(protection_cancels) == set(original.values())
        posts = list(exchange.posts)
    publish_candidates(exchange, lp, store, REWARDS)
    state = engine.lp_auto_run_once()
    if protection == 'all-triggered':
        assert {r['condition_id'] for r in state['last_round']['targets']} == set(tokens), state['last_round']
        assert {r['condition_id']: r['order_id'] for r in state['last_round']['targets']} == original
        assert live_orders(exchange) == original
        assert state['slots']['occupied'] == 5
        assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('39.00')
        assert exchange.cancels == protection_cancels and exchange.posts == posts
        return
    expected = {'A', 'B', 'C', 'E', 'F'} if protection == 'triggered' else {'B', 'C', 'E', 'F', 'G'}
    assert {r['condition_id'] for r in state['last_round']['targets']} == expected, state['last_round']
    if protection == 'triggered':
        assert live_orders(exchange)['A'] == original['A']
        # Actual protection confirmation releases its retained constraint.
        next(o for o in exchange.orders if o['token_id'] == 'A')['status'] = 'CANCELED'
        for order in exchange.orders:
            if order['order_id'] in exchange.cancels:
                order['status'] = 'CANCELED'
        exchange.direction = direction
        exchange.cancel_terminal = True
        engine.lp_tick()
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=10))
        engine.lp_auto_run_once()
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
        state = engine.lp_auto_run_once()
        assert {r['condition_id'] for r in state['last_round']['targets']} == {'B', 'C', 'E', 'F', 'G'}


def test_independent_exits_keep_exact_survivors_and_reservations(tmp_path, monkeypatch):
    engine, exchange, _, _ = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    exchange.cancel_terminal = False
    state = engine.lp_auto_run_once()
    assert exchange.cancels == ['original-A', 'original-D', 'original-I']
    assert live_orders(exchange)['B'] == 'original-B'
    assert live_orders(exchange)['C'] == 'original-C'
    assert exchange.posts == []
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('39.00')
    next(o for o in exchange.orders if o['token_id'] == 'A')['status'] = 'CANCELED'
    next(o for o in exchange.orders if o['token_id'] == 'I').update(status='CANCELED', size_matched='8')
    exchange.positions = [dict(token_id='I', condition_id='I', size='8', average_price='.39')]
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=10))
    state = engine.lp_auto_run_once()
    assert live_orders(exchange)['D'] == 'original-D'
    assert live_orders(exchange)['B'] == 'original-B'
    assert live_orders(exchange)['C'] == 'original-C'
    assert [p['token_id'] for p in exchange.posts] == ['E', 'F'], state['last_round']
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('39.00')
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.12')


def test_round_plan_survives_better_ranking_until_next_round(tmp_path, monkeypatch):
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    engine, exchange, _, _ = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    first = engine.lp_auto_state()['last_round']
    assert {r['condition_id'] for r in first['targets']} == {'B', 'C', 'E', 'F', 'G'}
    exchange.rewards.update(H=Decimal(1000), E=Decimal(30))
    # The original valid E/F/G quotes still execute after the cancels confirm.
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=10))
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['last_round']['round_id'] == first['round_id']
    assert [p['token_id'] for p in exchange.posts] == ['E', 'F', 'G']
    assert state['last_round']['completed_at']
    completed = datetime.fromisoformat(state['last_round']['completed_at'])
    monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=59))
    scheduler.request_check()
    assert not scheduler.run_due()
    monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=60))
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['last_round']['round_id'] != first['round_id']
    assert {r['condition_id'] for r in state['last_round']['targets']} == {'H', 'B', 'C', 'F', 'E'}


@pytest.mark.parametrize('phase', ['before_prepare', 'before_post'])
def test_changed_price_rejects_only_that_planned_action(tmp_path, monkeypatch, phase):
    engine, exchange, _, _ = plan_setup(tmp_path, monkeypatch, ('B', 'C'))
    direction = exchange.direction
    changed = False
    def changed_direction(token):
        result = direction(token)
        if token == 'E' and changed:
            result['book']['bids'][1]['price'] = '.38'
        return result
    exchange.direction = changed_direction
    if phase == 'before_prepare':
        books = exchange.lp_order_books
        e_reads = 0
        def read_books(ids, **kwargs):
            nonlocal e_reads, changed
            if 'E' in ids:
                e_reads += 1
                if e_reads == 2:
                    changed = True
            return books(ids, **kwargs)
        exchange.lp_order_books = read_books
    else:
        sign = exchange.lp_create_limit_order
        def changed_after_sign(**kwargs):
            nonlocal changed
            result = sign(**kwargs)
            if kwargs['token_id'] == 'E':
                changed = True
            return result
        exchange.lp_create_limit_order = changed_after_sign
    state = engine.lp_auto_run_once()
    assert changed
    assert [p['token_id'] for p in exchange.posts] == ['F', 'G'], state['last_round']
    action = next(a for a in state['last_round']['actions'] if a['condition_id'] == 'E')
    assert action['state'] == 'rejected'
    assert action['reason'] == 'candidate_changed'
    assert state['last_round']['completed_at']
    assert Decimal(next(r for r in state['last_round']['targets'] if r['condition_id'] == 'E')['price']) == Decimal('.39')
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
    state = engine.lp_auto_run_once()
    assert exchange.posts[-1]['token_id'] == 'E', state['last_round']
    assert Decimal(exchange.posts[-1]['price']) == Decimal('.38')


@pytest.mark.parametrize('failure', ['budget', 'refresh'])
def test_greedy_budget_and_failed_candidates_keep_valid_fallbacks(tmp_path, monkeypatch, failure):
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, budget='31.20' if failure == 'budget' else '100')
    exchange.rewards['X'] = Decimal(10000)
    direction = exchange.direction
    def costly(token):
        result = direction(token)
        if token == 'X':
            result['market'].update(minimum_order_size=Decimal(100), reward_min_size=Decimal(100))
        return result
    exchange.direction = costly
    publish_candidates(exchange, lp, store, ('X',))
    metadata = exchange.lp_market_metadata_fresh
    unavailable = failure == 'refresh'
    def fresh_metadata(ids, **kwargs):
        if unavailable and 'X' in ids:
            raise ValueError('simulated metadata unavailable')
        return metadata(ids, **kwargs)
    exchange.lp_market_metadata_fresh = fresh_metadata
    state = engine.lp_auto_run_once()
    expected = {'B', 'C', 'E', 'F'} if failure == 'budget' else {'B', 'C', 'E', 'F', 'G'}
    assert {r['condition_id'] for r in state['last_round']['targets']} == expected, state['last_round']
    assert set(live_orders(exchange)) == expected
    assert all(p['token_id'] != 'X' for p in exchange.posts)
    assert Decimal(state['funds']['buy_reserved_usd']) == (Decimal('31.20') if failure == 'budget' else Decimal('39.00'))
    if failure == 'refresh':
        unavailable = False
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=60))
        state = engine.lp_auto_run_once()
        assert {r['condition_id'] for r in state['last_round']['targets']} == {'X', 'B', 'C', 'E', 'F'}
