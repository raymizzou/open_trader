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
    from collections import Counter
    from copy import deepcopy
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, initial)
    result_reader = install_order_result_reader(exchange, monkeypatch)
    packets = []
    def results(request):
        packet = result_reader(request)
        packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
    state = engine.lp_auto_run_once()
    first = deepcopy(state['last_round'])
    assert {r['condition_id'] for r in first['targets']} == {'B', 'C', 'E', 'F', 'G'}
    assert all(Decimal(r['price']) == Decimal('.39') and Decimal(r['quantity']) == 20 for r in first['targets'])
    assert first['completed_at'] and state['active_plan'] is None and state['plan_wait'] is None
    completed = datetime.fromisoformat(first['completed_at'])

    immediate = {0: {'B', 'C', 'E', 'F', 'G'}, 3: {'E', 'F'}, 4: {'E'}, 5: set()}[len(initial)]
    missing = {'B', 'C', 'E', 'F', 'G'} - set(initial) - immediate
    victims = [f'original-{key}' for key in initial if key not in {'B', 'C'}]
    assert Counter(p['token_id'] for p in exchange.posts) == Counter(immediate)
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    _wait_round_cancels(store, state)
    assert Counter(exchange.cancels) == Counter(victims)
    physical = {'B', 'C'} | immediate if initial else immediate
    assert set(live_orders(exchange)) == physical
    assert sum(Decimal(str(o['price'])) * Decimal(str(o['original_size'])) for o in exchange.orders
        if o['status'] == 'LIVE') == {0: Decimal('39'), 3: Decimal('31.20'), 4: Decimal('23.40'), 5: Decimal('15.60')}[len(initial)]
    dependent = [a for a in first['actions'] if a['kind'] == 'buy' and a['token_id'] in missing]
    assert {a['token_id'] for a in dependent} == missing
    assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
    old_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    before = (len(exchange.reads), len(packets), deepcopy(exchange.posts), list(exchange.cancels))
    assert engine.lp_auto_set_desired_running(False)['pause_confirmed']
    paused = engine.lp_auto_run_once()
    assert paused['last_round']['round_id'] != first['round_id']
    assert paused['active_plan'] is None and paused['last_round']['actions'] == []
    assert len(exchange.reads) > before[0] and exchange.posts == before[2] and exchange.cancels == before[3]
    assert {sid: store.lp_actions(sid) for sid in old_audits} == old_audits
    engine.lp_auto_set_desired_running(True)
    publish_candidates(exchange, lp, store, REWARDS)
    state = engine.lp_auto_run_once()
    current = state['last_round']
    assert current['round_id'] != first['round_id'] and current['completed_at'] and state['active_plan'] is None
    assert {r['condition_id'] for r in current['targets']} == {'B', 'C', 'E', 'F', 'G'}
    new_buys = [a for a in current['actions'] if a['kind'] == 'buy']
    assert {a['token_id'] for a in new_buys} == missing
    assert all(a['state'] == 'success' and a['session_id'] not in old_audits for a in new_buys)
    assert {a['action_id'] for a in new_buys}.isdisjoint({a['action_id'] for a in first['actions']})
    assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
    for survivor in set(initial) & {'B', 'C'}:
        assert live_orders(exchange)[survivor] == f'original-{survivor}'
    assert Counter(exchange.cancels) == Counter(victims)
    assert Counter(p['token_id'] for p in exchange.posts) == Counter({'B', 'C', 'E', 'F', 'G'} - set(initial))
    assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20
        and p['post_only'] is True and p['side'] == 'BUY' for p in exchange.posts)
    assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('39.00')
    for sid, audits in old_audits.items():
        assert store.lp_actions(sid) == audits
    for action in [a for a in first['actions'] + current['actions'] if a['kind'] == 'buy' and a['state'] == 'success']:
        session = store.lp_session(action['session_id'])
        assert session['entry_order_id'] == live_orders(exchange)[action['token_id']]
        assert session['idempotency_key'] == 'lp-auto:' + action.get('request_id', action['action_id'])
        audit, = [a for a in store.lp_actions(action['session_id']) if a['role'] == 'entry']
        assert audit['state'] == 'accepted' and Decimal(audit['price']) == Decimal('.39') and Decimal(audit['quantity']) == 20


@pytest.mark.parametrize('protection', ['monitoring', 'triggered', 'all-triggered'])
def test_triggered_protection_retains_order_but_monitoring_does_not(tmp_path, monkeypatch, protection):
    tokens = ('A', 'B', 'C', 'D', 'I') if protection == 'all-triggered' else ('A', 'B', 'C', 'D')
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, tokens=tokens)
    from copy import deepcopy
    from collections import Counter
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    result_reader = install_order_result_reader(exchange, monkeypatch)
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
    protection_cancels = list(exchange.cancels)
    protection_audits = {session['session_id']:deepcopy(store.lp_actions(session['session_id'])) for session in store.lp_sessions()}
    publish_candidates(exchange, lp, store, REWARDS)
    state = engine.lp_auto_run_once()
    requests = _wait_round_cancels(store, state)
    expected = {'B', 'C', 'E', 'F', 'G'}
    assert all(a['kind']=='buy' for a in state['last_round']['actions'])
    assert state['slots']['occupied']==5 and Decimal(state['funds']['buy_reserved_usd'])==Decimal('39.00')
    victims = {original[key] for key in ('A','D','I') if key in original}
    assert {a['order_id'] for a in requests}==victims
    assert all(a['state']=='accepted' for a in requests)
    assert Counter(exchange.cancels)==Counter(protection_cancels)+Counter(victims)
    assert len(exchange.posts)==5
    assert all(p['side']=='BUY' for p in exchange.posts)
    for sid, audits in protection_audits.items():
        for old in audits:
            assert next(row for row in store.lp_actions(sid) if row['action_key']==old['action_key'])==old
    if protection == 'all-triggered':
        assert live_orders(exchange)==original and exchange.posts==posts
        assert {r['condition_id'] for r in state['last_round']['targets']}==expected
        return
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
    from collections import Counter
    from copy import deepcopy
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    import json
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    assert {o['order_id'] for o in exchange.orders} == {f'original-{key}' for key in ('A', 'B', 'C', 'D', 'I')}
    for order in exchange.orders:
        assert order['order_id'] == 'original-' + order['token_id'] and order['side'] == 'BUY' and order['status'] == 'LIVE'
        assert Decimal(order['price']) == Decimal('.39') and Decimal(order['original_size']) == 20 and Decimal(order['size_matched']) == 0
        order['fill_quantity_known'] = True
    result_reader = install_order_result_reader(exchange, monkeypatch)
    packets, planning, requested_packets = [], [], []
    def results(request):
        packet = result_reader(request)
        packets.append(deepcopy(packet))
        requested_packets.append((deepcopy(request), deepcopy(packet)))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
    owned_snapshots = []
    snapshot = exchange.lp_snapshot
    def owned_snapshot(request):
        packet = snapshot(request)
        packet['account'].update(trades_complete=True, pagination_complete=True)
        owned_snapshots.append((deepcopy(request), deepcopy(packet)))
        return packet
    monkeypatch.setattr(exchange, 'lp_snapshot', owned_snapshot)
    sign = exchange.lp_create_limit_order
    def signer(**kwargs):
        signed = sign(**kwargs)
        planning.append(deepcopy(engine.lp_auto_state()))
        return signed
    monkeypatch.setattr(exchange, 'lp_create_limit_order', signer)
    exchange.cancel_terminal = False
    state = engine.lp_auto_run_once()
    _wait_round_cancels(store, state)
    first = deepcopy(state['last_round'])
    assert Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'])
    assert live_orders(exchange) == {key: f'original-{key}' for key in ('A', 'B', 'C', 'D', 'I')}
    assert exchange.posts == []
    assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('39.00')
    assert first['completed_at'] and state['active_plan'] is None
    dependent = [a for a in first['actions'] if a['kind'] == 'buy']
    assert {a['token_id'] for a in dependent} == {'E', 'F', 'G'}
    assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
    original_i = next(s for s in store.lp_sessions() if s.get('entry_order_id') == 'original-I')
    original_audit = deepcopy(store.lp_actions(original_i['session_id']))
    old_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    completed = datetime.fromisoformat(first['completed_at'])

    next(o for o in exchange.orders if o['token_id'] == 'A')['status'] = 'CANCELED'
    next(o for o in exchange.orders if o['token_id'] == 'I').update(status='CANCELED', size_matched='8')
    exchange.positions = [dict(token_id='I', condition_id='I', size='8', average_price='.39')]
    exchange.trades = [dict(trade_id='original-I-owned-fill', order_id='original-I', condition_id='I',
        token_id='I', side='BUY', size='8', price='.39', fee='0', status='CONFIRMED',
        timestamp=pool.NOW, trader_side='MAKER', taker_order_id='external-taker',
        maker_orders=[dict(order_id='original-I', token_id='I', side='BUY', price='.39',
            matched_amount='8', fee='0', maker_address='test-wallet', owner='credential-owner-uuid')])]
    # Changed terminal/fill facts belong to the immediate next cycle.
    assert state['plan_wait'] is None
    publish_candidates(exchange, lp, store, REWARDS)
    state = engine.lp_auto_run_once()
    current = state['last_round']
    assert current['round_id'] != first['round_id'] and current['completed_at'] and state['active_plan'] is None
    assert set(live_orders(exchange)) == {'B', 'C', 'D', 'E', 'F'}
    for key in ('B', 'C', 'D'):
        assert live_orders(exchange)[key] == f'original-{key}'
    assert Counter(p['token_id'] for p in exchange.posts) == Counter(['E', 'F']), current
    assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20
        and p['post_only'] is True and p['side'] == 'BUY' for p in exchange.posts)
    assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('39.00')
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.12')
    lp.reconcile_facts(original_i['session_id'])
    proofs = [(request, packet) for request, packet in owned_snapshots
        if request['token_id'] == 'I' and any(o['order_id'] == 'original-I'
            and o['status'] == 'CANCELED' for o in packet['orders'])]
    assert proofs, 'Normal public owned reconciliation must observe actual terminal I'
    request, packet = proofs[-1]
    # Persist the actual unmodified external carrier before any ledger assertion.
    (tmp_path / 'V1-I-terminal-result-proof.json').write_text(json.dumps(
        dict(request=request, packet=packet), default=str, indent=2) + '\n')
    assert packet['account']['authenticated'] is True and packet['account']['wallet_address'] == 'test-wallet'
    assert all(packet['account'][key] is True for key in ('open_orders_complete', 'pagination_complete', 'trades_complete', 'positions_complete'))
    assert packet['account']['checked_at'] <= pool.NOW
    exact_i, = [o for o in packet['orders'] if o['order_id'] == 'original-I']
    assert exact_i['status'] == 'CANCELED' and exact_i['fill_quantity_known'] is True
    assert exact_i['side'] == 'BUY' and exact_i['token_id'] == exact_i['condition_id'] == 'I'
    assert Decimal(exact_i['size_matched']) == 8 and Decimal(exact_i['original_size']) == 20 and Decimal(exact_i['price']) == Decimal('.39')
    assert packet['account']['positions'] == exchange.positions and packet['trades'] == exchange.trades
    trade, = packet['trades']
    maker, = trade['maker_orders']
    assert trade['trade_id'] == 'original-I-owned-fill' and trade['trader_side'] == 'MAKER'
    assert maker['order_id'] == 'original-I' and maker['maker_address'] == 'test-wallet' and maker['owner'] == 'credential-owner-uuid'
    assert Decimal(maker['matched_amount']) == 8 and Decimal(maker['price']) == Decimal('.39') and Decimal(maker['fee']) == 0
    session = engine.lp_status(original_i['session_id'])
    assert Decimal(session['buy_filled_quantity']) == Decimal(session['residual_quantity']) == 8
    assert Decimal(session['buy_cost']) == Decimal('3.12') and Decimal(session['buy_fees']) == 0
    assert session['position_reconciled'] is True
    assert session['fee_status'] == 'known' and Decimal(session['projected_exit_fee']) == 0
    assert not store.lp_session(original_i['session_id']).get('yield_rotation_cancel_owners')
    assert planning and state['funds']['status'] == 'unknown'
    assert state['funds']['as_of'] == planning[0]['funds']['as_of']
    assert store.lp_actions(original_i['session_id']) == original_audit
    buys = [a for a in current['actions'] if a['kind'] == 'buy' and a['state'] == 'success']
    assert {a['token_id'] for a in buys} == {'E', 'F'}
    assert all(a['session_id'] not in old_audits for a in buys)
    assert {a['action_id'] for a in current['actions']}.isdisjoint({a['action_id'] for a in first['actions']})
    d_cancels = [a for a in _wait_round_cancels(store, state) if a['order_id'] == 'original-D']
    assert Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'] + ['original-D'] * len(d_cancels))
    assert len(d_cancels) <= 1
    for sid, audits in old_audits.items():
        now = store.lp_actions(sid)
        for audit in audits:
            assert next(a for a in now if a['action_key'] == audit['action_key']) == audit
        added = [a for a in now if a['action_key'] not in {old['action_key'] for old in audits}]
        if added:
            assert len(added) == len(d_cancels) == 1
            assert added[0]['order_id'] == 'original-D' and added[0]['rotation_round_id'] == current['round_id']
            assert added[0]['state'] == 'accepted' and added[0]['action_id'] not in {a['action_id'] for a in audits}

def test_round_plan_survives_better_ranking_until_next_round(tmp_path, monkeypatch):
    from collections import Counter
    from copy import deepcopy
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    result_reader = install_order_result_reader(exchange, monkeypatch)
    packets = []
    def results(request):
        packet = result_reader(request)
        packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    _wait_round_cancels(store, state)
    first = deepcopy(state['last_round'])
    assert {r['condition_id'] for r in first['targets']} == {'B', 'C', 'E', 'F', 'G'}
    assert exchange.posts == [] and Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'])
    assert first['completed_at'] and state['active_plan'] is None
    dependent = [a for a in first['actions'] if a['kind'] == 'buy']
    assert {a['token_id'] for a in dependent} == {'E', 'F', 'G'}
    assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
    old_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    completed = datetime.fromisoformat(first['completed_at'])

    exchange.rewards.update(H=Decimal(1000), E=Decimal(30))
    assert state['plan_wait'] is None
    assert {sid: store.lp_actions(sid) for sid in old_audits} == old_audits
    publish_candidates(exchange, lp, store, REWARDS)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    current = state['last_round']
    assert current['round_id'] != first['round_id'] and current['completed_at'] and state['active_plan'] is None
    assert [r['condition_id'] for r in current['targets']] == ['H', 'B', 'C', 'F', 'E']
    assert Counter(p['token_id'] for p in exchange.posts) == Counter(['H', 'F', 'E'])
    assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20
        and p['post_only'] is True and p['side'] == 'BUY' for p in exchange.posts)
    assert set(live_orders(exchange)) == {'H', 'B', 'C', 'F', 'E'}
    assert live_orders(exchange)['B'] == 'original-B' and live_orders(exchange)['C'] == 'original-C'
    assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('39.00')
    buys = [a for a in current['actions'] if a['kind'] == 'buy']
    assert {a['token_id'] for a in buys} == {'H', 'F', 'E'} and all(a['state'] == 'success' for a in buys)
    assert all(a['session_id'] not in old_audits for a in buys)
    assert {a['action_id'] for a in buys}.isdisjoint({a['action_id'] for a in first['actions']})
    assert Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'])
    assert {sid: store.lp_actions(sid) for sid in old_audits} == old_audits


@pytest.mark.parametrize('phase', ['planning_refresh', 'after_sign'])
def test_changed_price_rejects_only_that_planned_action(tmp_path, monkeypatch, phase):
    from collections import Counter
    from tests.test_lp_auto_independent_cancel_contract import _wait_round_cancels
    from copy import deepcopy
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, ('B', 'C'))
    result_reader = install_order_result_reader(exchange, monkeypatch)
    direction = exchange.direction
    changed = False
    def changed_direction(token):
        result = direction(token)
        if token == 'E' and changed:
            result['book']['bids'][1]['price'] = '.38'
        return result
    monkeypatch.setattr(exchange, 'direction', changed_direction)
    prior_e_quote = deepcopy(exchange.direction('E')['book']['bids'][1])
    assert Decimal(prior_e_quote['price']) == Decimal('.39')
    signatures, reads, packets, requested_packets = [], [], [], []
    for name in ('lp_market_metadata_fresh', 'lp_reward_catalog', 'lp_order_books', 'lp_account_snapshot', 'lp_account_snapshot_shared'):
        read = getattr(exchange, name)
        def observed(*args, _read=read, _name=name, **kwargs):
            reads.append(dict(method=_name, active=engine.lp_auto_state()['active_plan'] is not None))
            return _read(*args, **kwargs)
        monkeypatch.setattr(exchange, name, observed)
    def results(request):
        packet = result_reader(request)
        packets.append(deepcopy(packet))
        requested_packets.append((deepcopy(request), deepcopy(packet)))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
    sign = exchange.lp_create_limit_order
    def changed_after_sign(**kwargs):
        nonlocal changed
        signed = sign(**kwargs)
        signatures.append(deepcopy(signed))
        if phase == 'after_sign' and kwargs['token_id'] == 'E' and not changed:
            assert Decimal(signed['price']) == Decimal('.39') and Decimal(signed['quantity']) == 20
            changed = True
        return signed
    monkeypatch.setattr(exchange, 'lp_create_limit_order', changed_after_sign)
    if phase == 'planning_refresh':
        changed = True
        assert Decimal(exchange.direction('E')['book']['bids'][1]['price']) == Decimal('.38')
        assert Decimal(prior_e_quote['price']) == Decimal('.39')
    state = engine.lp_auto_run_once()
    first = deepcopy(state['last_round'])
    assert changed and first['completed_at'] and state['active_plan'] is None
    assert Counter(p['token_id'] for p in exchange.posts) == Counter(['E', 'F', 'G']), first
    assert Counter(p['token_id'] for p in signatures) == Counter(['E', 'F', 'G'])
    assert not any(r['active'] for r in reads), 'Approved execution must not reread business market/finance'
    expected_e = Decimal('.38') if phase == 'planning_refresh' else Decimal('.39')
    for token in ('E', 'F', 'G'):
        action, = [a for a in first['actions'] if a['kind'] == 'buy' and a['token_id'] == token]
        target, = [r for r in first['targets'] if r['token_id'] == token]
        post, = [p for p in exchange.posts if p['token_id'] == token]
        expected_price = expected_e if token == 'E' else Decimal('.39')
        assert action['state'] == 'success' and Decimal(target['price']) == Decimal(post['price']) == expected_price
        assert Decimal(target['quantity']) == Decimal(post['quantity']) == 20 and post['post_only'] is True and post['side'] == 'BUY'
        session = store.lp_session(action['session_id'])
        assert session['entry_order_id'] == live_orders(exchange)[token]
        assert session['idempotency_key'] == 'lp-auto:' + action.get('request_id', action['action_id'])
        audit, = [a for a in store.lp_actions(action['session_id']) if a['role'] == 'entry']
        assert audit['state'] == 'accepted' and Decimal(audit['price']) == expected_price and Decimal(audit['quantity']) == 20
    original = live_orders(exchange)
    assert set(original) == {'B', 'C', 'E', 'F', 'G'} and original['B'] == 'original-B' and original['C'] == 'original-C'
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == (Decimal('38.80') if phase == 'planning_refresh' else Decimal('39.00'))
    assert exchange.cancels == []
    old_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    completed = datetime.fromisoformat(first['completed_at'])

    before = (len(reads), len(packets), deepcopy(exchange.posts))
    assert engine.lp_auto_set_desired_running(False)['pause_confirmed']
    current = engine.lp_auto_run_once()
    assert current['last_round']['round_id'] != first['round_id']
    assert current['active_plan'] is None and current['last_round']['actions'] == []
    assert len(reads) > before[0] and exchange.posts == before[2] and exchange.cancels == []
    assert {sid: store.lp_actions(sid) for sid in old_audits} == old_audits
    engine.lp_auto_set_desired_running(True)
    if phase == 'planning_refresh':
        assert live_orders(exchange) == original
        return
    original_e, = [o for o in exchange.orders if o['order_id'] == original['E']]
    assert original_e['token_id'] == original_e['condition_id'] == 'E' and original_e['side'] == 'BUY' and original_e['status'] == 'LIVE'
    assert Decimal(original_e['price']) == Decimal('.39') and Decimal(original_e['original_size']) == 20 and Decimal(original_e['size_matched']) == 0
    original_e['fill_quantity_known'] = True
    departure_books, cancel_observations = [], []
    def current_departure_direction(token):
        result = direction(token)
        if token == 'E':
            for level, price in zip(result['book']['bids'], ('.39', '.38')):
                own = sum((Decimal(str(o['original_size'])) - Decimal(str(o['size_matched']))
                    for o in exchange.orders if o['token_id'] == 'E' and o['side'] == 'BUY'
                    and o['status'] == 'LIVE' and Decimal(str(o['price'])) == Decimal(price)), Decimal(0))
                level.update(price=price, size=str(Decimal(1000) + own))
            departure_books.append(deepcopy(result['book']))
        return result
    monkeypatch.setattr(exchange, 'direction', current_departure_direction)
    cancel = exchange.cancel_order
    def cancel_departed_order(order_id):
        assert order_id == original['E']
        assert departure_books
        book = departure_books[-1]
        assert [Decimal(level['price']) for level in book['bids']] == [Decimal('.39'), Decimal('.38')]
        assert [Decimal(level['size']) for level in book['bids']] == [Decimal(1020), Decimal(1000)]
        assert Decimal(book['asks'][0]['price']) == Decimal('.41')
        assert original_e['status'] == 'LIVE' and Decimal(original_e['price']) == Decimal(book['bids'][0]['price'])
        cancel_observations.append(dict(order_id=order_id, book=deepcopy(book), order=deepcopy(original_e)))
        return cancel(order_id)
    monkeypatch.setattr(exchange, 'cancel_order', cancel_departed_order)
    publish_candidates(exchange, lp, store, REWARDS)
    state = engine.lp_auto_run_once()
    _wait_round_cancels(store, state)
    second = deepcopy(state['last_round'])
    assert second['round_id'] != first['round_id'] and second['completed_at'] and state['active_plan'] is None
    assert Counter(exchange.cancels) == Counter([original['E']]) and len(exchange.posts) == 3
    assert live_orders(exchange) == {key: original[key] for key in ('B', 'C', 'F', 'G')}
    assert len(cancel_observations) == 1
    assert {t['condition_id'] for t in second['targets']} == {'B', 'C', 'F', 'G', 'H'}
    replacement, = [a for a in second['actions'] if a['kind'] == 'buy']
    assert replacement['token_id'] == 'H' and replacement['state'] == 'not_sent' and replacement['reason'] == 'next_round_capacity'
    assert all(p['token_id'] != 'H' for p in exchange.posts)
    current_live_packets = [(request, packet) for request, packet in requested_packets
        if original['E'] in request['owned_order_ids'] and any(o['order_id'] == original['E']
            and o['status'] == 'LIVE' for o in packet['orders'])]
    assert current_live_packets
    live_request, live_packet = current_live_packets[-1]
    live_e, = [o for o in live_packet['orders'] if o['order_id'] == original['E']]
    assert live_packet['authenticated'] is True and live_packet['wallet_address'] == 'test-wallet'
    assert all(live_packet[k] is True for k in ('open_orders_complete', 'pagination_complete', 'trades_complete', 'positions_complete'))
    assert live_e['fill_quantity_known'] is True and Decimal(live_e['size_matched']) == 0
    assert live_e['side'] == 'BUY' and live_e['token_id'] == 'E' and Decimal(live_e['price']) == Decimal('.39') and Decimal(live_e['original_size']) == 20
    old_e, = [a for a in first['actions'] if a['kind'] == 'buy' and a['token_id'] == 'E']
    cancel_audit, = [a for a in store.lp_actions(old_e['session_id']) if a['role'] == 'reconciliation_cancel']
    import uuid
    uuid.UUID(cancel_audit['action_id'])
    assert cancel_audit['state'] == 'accepted' and cancel_audit['order_id'] == original['E'] and cancel_audit['rotation_round_id'] == second['round_id']
    assert cancel_audit['action_key'] not in {a['action_key'] for a in old_audits[old_e['session_id']]}

    for sid, audits in old_audits.items():
        current_audits = store.lp_actions(sid)
        for audit in audits:
            assert next(a for a in current_audits if a['action_key'] == audit['action_key']) == audit
    second_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    completed2 = datetime.fromisoformat(second['completed_at'])

    before = (len(reads), len(packets), deepcopy(exchange.posts), list(exchange.cancels))
    assert engine.lp_auto_set_desired_running(False)['pause_confirmed']
    paused = engine.lp_auto_run_once()
    assert paused['last_round']['round_id'] != second['round_id']
    assert paused['active_plan'] is None and paused['last_round']['actions'] == []
    assert exchange.posts == before[2] and exchange.cancels == before[3]
    assert {sid: store.lp_actions(sid) for sid in second_audits} == second_audits
    engine.lp_auto_set_desired_running(True)
    publish_candidates(exchange, lp, store, REWARDS)
    state = engine.lp_auto_run_once()
    final = state['last_round']
    assert final['round_id'] not in (first['round_id'], second['round_id']) and final['completed_at'] and state['active_plan'] is None
    new_e, = [a for a in final['actions'] if a['kind'] == 'buy' and a['token_id'] == 'E']
    old_e, = [a for a in first['actions'] if a['kind'] == 'buy' and a['token_id'] == 'E']
    assert new_e['state'] == 'success' and new_e['session_id'] != old_e['session_id']
    assert new_e['action_id'] not in {a['action_id'] for a in first['actions'] + second['actions']}
    assert live_orders(exchange)['E'] != original['E']
    new_session = store.lp_session(new_e['session_id'])
    assert new_session['idempotency_key'] == 'lp-auto:' + new_e.get('request_id', new_e['action_id'])
    new_audit, = [a for a in store.lp_actions(new_e['session_id']) if a['role'] == 'entry']
    assert new_audit['state'] == 'accepted' and Decimal(new_audit['price']) == Decimal('.38') and Decimal(new_audit['quantity']) == 20
    assert all(live_orders(exchange)[key] == original[key] for key in ('B', 'C', 'F', 'G'))
    assert Counter(p['token_id'] for p in exchange.posts) == Counter(['E', 'F', 'G', 'E'])
    assert Decimal(exchange.posts[-1]['price']) == Decimal('.38') and Decimal(exchange.posts[-1]['quantity']) == 20
    assert all(p['post_only'] is True and p['side'] == 'BUY' for p in exchange.posts)
    assert Counter(exchange.cancels) == Counter([original['E']])
    assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('38.80')
    owned_packets = []
    snapshot = exchange.lp_snapshot
    def owned_snapshot(request):
        packet = snapshot(request)
        packet['account'].update(trades_complete=True, pagination_complete=True)
        owned_packets.append((deepcopy(request), deepcopy(packet)))
        return packet
    monkeypatch.setattr(exchange, 'lp_snapshot', owned_snapshot)
    lp.reconcile_facts(old_e['session_id'])
    terminal_packets = [(request, packet) for request, packet in owned_packets
        if request['token_id'] == 'E' and any(o['order_id'] == original['E']
            and o['status'] == 'CANCELED' for o in packet['orders'])]
    assert terminal_packets
    terminal_request, terminal_packet = terminal_packets[-1]
    terminal_e, = [o for o in terminal_packet['orders'] if o['order_id'] == original['E']]
    assert terminal_packet['account']['authenticated'] is True and terminal_packet['account']['wallet_address'] == 'test-wallet'
    assert all(terminal_packet['account'][k] is True for k in ('open_orders_complete', 'pagination_complete', 'trades_complete', 'positions_complete'))
    assert terminal_e['fill_quantity_known'] is True and Decimal(terminal_e['size_matched']) == 0
    assert terminal_e['side'] == 'BUY' and terminal_e['token_id'] == 'E' and Decimal(terminal_e['price']) == Decimal('.39') and Decimal(terminal_e['original_size']) == 20
    assert [Decimal(level['price']) for level in departure_books[-1]['bids']] == [Decimal('.39'), Decimal('.38')]
    assert all(p['token_id'] != 'H' for p in exchange.posts)
    import json
    (tmp_path / 'V1-E-current-result-and-departure-proof.json').write_text(json.dumps(dict(
        live_request=live_request, live_packet=live_packet, terminal_request=terminal_request,
        terminal_packet=terminal_packet, cancel_observations=cancel_observations), default=str, indent=2) + '\n')
    assert {sid: store.lp_actions(sid) for sid in second_audits} == second_audits
    assert not any(r['active'] for r in reads)


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
