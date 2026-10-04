"""Account coverage replaces temporary automatic allocations without reattribution."""
from copy import deepcopy
from datetime import timedelta
import hashlib
from decimal import Decimal

import pytest

from tests import test_lp_auto_pool as pool


def covered_pool(tmp_path, *, buys=(), inventory='0', pnl='0'):
    engine, exchange, lp, store = pool.setup(tmp_path, 3)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=2))
    engine.lp_auto_set_desired_running(True)
    auto = engine._auto_pool
    facts = dict(account_id='test-wallet', pool_account_id=engine._lp_account_id(),
        checked_at=pool.NOW.isoformat(), read_started_at=pool.NOW.isoformat(),
        read_ended_at=pool.NOW.isoformat(), trade_generation=store.lp_trade_generation(),
        inventory_cost_usd=inventory, realized_pnl_usd=pnl,
        financial_status='known', reason_codes=[], buys=list(buys))
    auto._update(lambda d: d.update(account_financial_facts=facts))
    return engine, exchange, lp, store


def buy(order_id, *, token='m00', reserved='8', state='active'):
    return dict(order_id=order_id, session_id='account-session', condition_id=token,
        token_id=token, market_id=token, outcome='YES', price='.4', quantity='20',
        original_quantity='20', filled_quantity='0', reserved_usd=reserved,
        state=state, financial_status='known', checked_at=pool.NOW.isoformat())


def old_intent(**updates):
    return dict(intent_id='old', session_id='old-session', order_id=None,
        condition_id='m00', token_id='m00', market_id='m00', outcome='YES',
        price='.4', quantity='20', reserved_usd='8', inventory_cost_usd='0',
        realized_pnl_usd='0', financial_status='unknown', state='unknown',
        created_at=pool.NOW.isoformat(), checked_at=pool.NOW.isoformat(),
        reservation_coverage={'version': 1, 'state': 'covered', 'account_id': 'test-wallet',
            'pool_account_id': hashlib.sha256(b'test-wallet').hexdigest(),
            'snapshot_id': 'snapshot', 'session_id': 'old-session', 'intent_id': 'old',
            'read_started_at': pool.NOW.isoformat(), 'checked_at': pool.NOW.isoformat()}, **updates)


@pytest.mark.parametrize('pnl', ['3', '-20', None])
def test_account_projection_counts_order_ids_inventory_and_covered_audit_once(tmp_path, pnl):
    engine, _, _, _ = covered_pool(tmp_path, buys=[buy('a'), buy('b')], inventory='12', pnl=pnl)
    intent = old_intent()
    engine._auto_pool._update(lambda d: d['intents'].update(old=intent))
    state = engine.lp_auto_state()
    assert state['slots'] == dict(active=2, pending=0, pending_review=0, canceling=0, occupied=2)
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    assert Decimal(state['funds']['inventory_cost_usd']) == 12
    assert Decimal(state['funds']['total_usd']) == 100
    assert Decimal(state['funds']['spendable_usd']) == 72
    assert Decimal(state['funds']['isolated_reserved_usd']) == 0
    assert state['funds']['status'] == 'known'
    assert state['intents'] == [intent]
    assert not state['admission_block_reasons']
    assert 'submission_unknown' not in state['block_reasons']


def test_late_callback_cannot_restore_covered_funds_or_slot(tmp_path):
    engine, _, _, _ = covered_pool(tmp_path)
    auto = engine._auto_pool
    auto._update(lambda d: d['intents'].update(old=old_intent()))
    before = deepcopy(auto.state()['funds'])
    auto._record_session('old', dict(session_id='old-session'), error='late_network_failure',
        connection=None)
    state = auto.state()
    assert state['intents'][0]['state'] == 'unknown'
    assert 'reconcile_error' not in state['intents'][0]
    assert state['intents'][0]['reservation_coverage']
    assert state['funds'] == before
    assert state['slots']['occupied'] == 0


@pytest.mark.parametrize('invalid', ['stale', 'generation', 'inventory_unknown', 'wrong_account'])
def test_account_projection_failure_never_reenables_covered_reservations(tmp_path, invalid):
    engine, _, _, store = covered_pool(tmp_path)
    auto = engine._auto_pool
    def change(d):
        d['intents']['old'] = old_intent()
        facts = d['account_financial_facts']
        if invalid == 'stale':
            facts['checked_at'] = (pool.NOW - timedelta(seconds=61)).isoformat()
        elif invalid == 'inventory_unknown':
            facts.update(financial_status='unknown', inventory_cost_usd=None,
                         reason_codes=['account_inventory_cost_unknown'])
        elif invalid == 'wrong_account':
            facts['pool_account_id'] = 'other-wallet'
    auto._update(change)
    if invalid == 'generation':
        store.lp_advance_trade_generation(store.lp_trade_generation())
    state = auto.state()
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['spendable_usd'] is None
    assert state['funds']['available_usd'] is None
    assert state['admission_block_reasons']
    assert state['slots']['occupied'] == 0
    assert Decimal(state['funds']['buy_reserved_usd']) == 0
    assert Decimal(state['funds']['isolated_reserved_usd']) == 0


def test_uncovered_temporary_reservation_remains_beside_real_order(tmp_path):
    engine, _, _, _ = covered_pool(tmp_path, buys=[buy('a')])
    intent = old_intent()
    intent.pop('reservation_coverage')
    intent.update(state='reserved', financial_status='known', condition_id='m01', token_id='m01')
    engine._auto_pool._update(lambda d: d['intents'].update(old=intent))
    state = engine.lp_auto_state()
    assert state['slots']['occupied'] == 2
    assert state['slots']['pending'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == 16
    assert Decimal(state['funds']['spendable_usd']) == 84


def rotation_pool(tmp_path, monkeypatch, *, same_token=False, target=1):
    from tests import test_lp_auto_rotation as rotation
    engine, exchange, lp, store = rotation.setup(tmp_path, monkeypatch, count=3, target=1)
    original = engine.lp_auto_state()['intents'][0]
    session = store.lp_session(original['session_id'])
    rows = [buy('o1')]
    if same_token:
        exchange.orders.append({**exchange.orders[0], 'order_id': 'manual-second'})
        store.lp_register_exchange_orders('test-wallet', 'm00', exchange.orders, session=session)
        rows.append(buy('manual-second'))
    for row in rows:
        row['session_id'] = session['session_id']
    facts = dict(account_id='test-wallet', pool_account_id=engine._lp_account_id(),
        checked_at=pool.NOW.isoformat(), trade_generation=store.lp_trade_generation(),
        inventory_cost_usd='0', realized_pnl_usd='0', financial_status='known',
        reason_codes=[], buys=rows, order_fills={row['order_id']: '0' for row in rows})
    # Imported account orders have no automatic intent. The durable sessions
    # are the same execution objects regardless of their submitting origin.
    engine._auto_pool._update(lambda d: d.update(intents={}, account_financial_facts=facts,
                                                 target_buy_count=target))
    return engine, exchange, lp, store, rotation


def test_account_rotation_ranks_and_cancels_real_id_without_fake_intent(tmp_path, monkeypatch):
    engine, exchange, lp, store, rotation = rotation_pool(tmp_path, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    rotation.refresh(lp, exchange, 3)
    state = engine.lp_auto_run_once()
    assert exchange.cancels == ['o1']
    assert len(exchange.posts) == 1
    assert state['intents'] == []
    assert state['slots']['occupied'] == 1
    assert state['slots']['canceling'] == 1
    assert state['funds']['spendable_usd'] is None
    assert engine._auto_pool._read()['account_rotations']['o1']['rotation_cancel_acknowledged']
    assert any(a.get('order_id') == 'o1' and a.get('role') == 'reconciliation_cancel'
               for a in store.lp_actions(state['account_buys'][0]['session_id']))


def test_overcapacity_same_token_buys_use_two_slots_and_exact_rotation_victim(tmp_path, monkeypatch):
    engine, exchange, lp, _, rotation = rotation_pool(tmp_path, monkeypatch, same_token=True)
    exchange.rewards['m00'] = Decimal('30')
    rotation.refresh(lp, exchange, 3)
    state = engine.lp_auto_state()
    assert state['slots']['occupied'] == 2
    targets, victims, _, blocked = engine._auto_pool._ranked_buys(state)
    assert not blocked
    assert len(targets) == 1
    assert len(victims) == 1
    assert targets[0]['order_id'] != victims[0]['order_id']
    assert {targets[0]['order_id'], victims[0]['order_id']} == {'o1', 'manual-second'}
    engine.lp_auto_run_once()
    assert len(exchange.cancels) == 1
    assert len(exchange.posts) == 1


def test_account_rotation_fill_keeps_inventory_budget_without_sale(tmp_path, monkeypatch):
    engine, exchange, lp, store, rotation = rotation_pool(tmp_path, monkeypatch)
    exchange.rewards['m01'] = Decimal('25')
    rotation.refresh(lp, exchange, 3)
    engine.lp_auto_run_once()
    exchange.orders[0].update(status='CANCELED', size_matched='8')
    def terminal(d):
        d['account_financial_facts'].update(buys=[], order_fills={'o1': '8'},
            inventory_cost_usd='3.2', trade_generation=store.lp_trade_generation())
    engine._auto_pool._update(terminal)
    state = engine.lp_auto_run_once()
    assert state['last_round']['reason'] == 'rotation_filled'
    assert state['slots']['occupied'] == 0
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.2')
    assert Decimal(state['funds']['spendable_usd']) == Decimal('96.8')
    assert len(exchange.posts) == 1


def test_late_known_receipt_cannot_reattribute_covered_unknown_request(tmp_path):
    engine, _, _, store = covered_pool(tmp_path, buys=[buy('account-order')])
    auto = engine._auto_pool
    old = old_intent()
    auto._update(lambda d: d['intents'].update(old=deepcopy(old)))
    discovered = dict(session_id='old-session', state='entry_open', entry_order_id='account-order',
        condition_id='m00', token_id='m00', submit_status='accepted',
        order_history={'account-order': dict(status='LIVE', side='BUY', token_id='m00')})
    with store._transaction() as connection:
        auto._record_session('old', discovered, connection=connection)
    assert auto.state()['intents'] == [old]
    assert auto._read()['events'] == {}
    assert auto.state()['slots']['occupied'] == 1
    assert Decimal(auto.state()['funds']['buy_reserved_usd']) == 8


@pytest.mark.parametrize('returned', [True, False])
def test_refresh_generation_wait_preserves_health_and_does_not_retry_in_round(tmp_path, returned):
    from open_trader.polymarket_lp import LpObservationWait
    engine, exchange, lp, _ = covered_pool(tmp_path)
    calls = []
    def snapshot(**kwargs):
        calls.append(kwargs)
        if not returned:
            raise LpObservationWait('account_round_invalid')
        return {}
    exchange.lp_account_snapshot_shared = snapshot
    lp.register_account_snapshot = lambda snapshot: dict(state='skipped', reason='account_round_invalid')
    prior = lp._account_order_sync_error
    state = engine.lp_auto_reconcile_unknown()
    assert len(calls) == 1
    assert calls[0]['max_age_seconds'] == 0
    assert callable(calls[0]['trade_generation_provider'])
    assert lp._account_order_sync_error == prior
    assert state['funds']['spendable_usd'] is None
    assert 'account_round_invalid' in state['admission_block_reasons']


def test_unknown_real_order_capital_keeps_slot_and_blocks_admission(tmp_path):
    row = buy('missing-remaining', state='canceling')
    row.update(reserved_usd=None, quantity=None, financial_status='unknown')
    engine, _, _, _ = covered_pool(tmp_path, buys=[row])
    state = engine.lp_auto_state()
    assert state['slots']['occupied'] == 1
    assert state['slots']['canceling'] == 1
    assert state['funds']['buy_reserved_usd'] is None
    assert state['funds']['spendable_usd'] is None
    assert state['funds']['status'] == 'unknown'


def test_rotation_metadata_cannot_overwrite_new_partial_fill_capital(tmp_path):
    engine, _, _, _ = covered_pool(tmp_path, buys=[{**buy('a'), 'quantity': '12', 'reserved_usd': '4.8'}], inventory='3.2')
    engine._auto_pool._update(lambda d: d.update(account_rotations={'a': {
        **buy('a'), 'rotation_requested_at': pool.NOW.isoformat(), 'rotation_cancel_acknowledged': True}}))
    state = engine.lp_auto_state()
    assert state['slots']['canceling'] == 1
    assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('4.8')
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.2')
    assert Decimal(state['funds']['spendable_usd']) == 92


def test_first_account_coverage_preserves_inflight_rotation_fill_stop(tmp_path, monkeypatch):
    from tests import test_lp_auto_rotation as rotation
    engine, exchange, lp, store = rotation.setup(tmp_path, monkeypatch, count=2, target=1)
    exchange.rewards['m01'] = Decimal('25')
    rotation.refresh(lp, exchange, 2)
    engine.lp_auto_run_once()
    intent = engine.lp_auto_state()['intents'][0]
    assert intent['state'] == 'canceling'
    exchange.orders[0].update(status='CANCELED', size_matched='8')
    def cover(d):
        d['intents'][intent['intent_id']]['reservation_coverage'] = {
            **old_intent()['reservation_coverage'], 'session_id': intent['session_id'],
            'intent_id': intent['intent_id']}
        d['account_financial_facts'] = dict(account_id='test-wallet', pool_account_id=engine._lp_account_id(),
            checked_at=pool.NOW.isoformat(), trade_generation=store.lp_trade_generation(),
            inventory_cost_usd='3.2', realized_pnl_usd='0', financial_status='known',
            reason_codes=[], buys=[], order_fills={'o1': '8'})
    engine._auto_pool._update(cover)
    state = engine.lp_auto_run_once()
    assert state['last_round']['reason'] == 'rotation_filled'
    assert state['slots']['occupied'] == 0
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.2')
    assert len(exchange.posts) == 1
    assert state['intents'][0]['state'] == 'canceling'  # Audit is frozen at replacement.


def test_initial_configuration_accepts_known_manual_account_overcapacity(tmp_path):
    engine, exchange, _, store = pool.setup(tmp_path, 3)
    engine._lp_auto_pool()._update(lambda d: d.update(account_financial_facts=dict(
        account_id='test-wallet', pool_account_id=engine._lp_account_id(),
        checked_at=pool.NOW.isoformat(), trade_generation=store.lp_trade_generation(),
        financial_status='known', reason_codes=[], inventory_cost_usd='0',
        realized_pnl_usd='0', buys=[buy('manual-a'), buy('manual-b')])))
    state = engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    assert state['slots']['occupied'] == 2
    assert state['target_buy_count'] == 1
    assert Decimal(state['funds']['spendable_usd']) == 84
    assert state['intents'] == []
    with pytest.raises(ValueError, match='pause_and_finish_automatic_buys_before_configuring'):
        engine.lp_auto_configure(dict(budget_usd='120', target_buy_count=3))
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once()
    assert exchange.posts == []


def test_covered_request_attention_is_inactive_but_audit_is_preserved(tmp_path, monkeypatch):
    from open_trader.polymarket_lp_notification_batches import ChannelDeliveryResult
    engine, _, _, _ = covered_pool(tmp_path)
    auto = engine._auto_pool
    intent = old_intent(attention_due=True, attention_recovery_due=True,
        attention_episode='old-fault', attention_since=pool.NOW.isoformat(), manual_attention=True)
    auto._update(lambda d: d['intents'].update(old=deepcopy(intent)))
    auto._deliver_attention('old')
    auto._deliver_attention('old', recovery=True)
    auto._finish_attention_delivery({('old', False, 'old-fault'): ChannelDeliveryResult({'test'}, {'test'})}, recovery=False)
    calls = []
    monkeypatch.setattr(auto, '_deliver_attention', lambda *args, **kwargs: calls.append(args))
    auto.flush_attention()
    auto.reconcile_attention()
    assert calls == []
    assert auto._read()['intents']['old'] == intent


def test_covered_unknown_session_rotates_its_known_account_order(tmp_path, monkeypatch):
    from tests import test_lp_auto_rotation as rotation
    monkeypatch.setattr(pool, 'Exchange', rotation.RotationExchange)
    engine, exchange, lp, store = pool.setup(tmp_path, 2)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    engine.lp_auto_set_desired_running(True)
    exchange.fail = True
    engine.lp_auto_run_once()
    original = engine.lp_auto_state()['intents'][0]
    assert original['state'] == 'unknown'
    exchange.fail = False
    exchange.orders = [dict(order_id='manual-late', token_id='m00', condition_id='m00',
        market_id='m00', outcome='YES', side='BUY', status='LIVE', price='.4',
        original_size='20', size_matched='0')]
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
    result = lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))
    assert result['state'] == 'registered', result
    managed = store.lp_session(original['session_id'])
    assert managed['state'] == 'entry_open'
    assert managed['submit_status'] == 'unknown'
    exchange.rewards['m01'] = Decimal('25')
    rotation.refresh(lp, exchange, 2)
    state = engine.lp_auto_run_once()
    assert exchange.cancels == ['manual-late']
    assert len(exchange.posts) == 1  # Only the original timed-out POST occurred.
    audit = state['intents'][0]
    assert audit['state'] == 'unknown' and audit['order_id'] is None
    assert state['slots']['canceling'] == 1


def test_account_row_fill_blocks_rotation_even_before_session_fill_updates(tmp_path, monkeypatch):
    engine, exchange, lp, store, rotation = rotation_pool(tmp_path, monkeypatch)
    row = engine.lp_auto_state()['account_buys'][0]
    assert Decimal(store.lp_session(row['session_id'])['buy_filled_quantity']) == 0
    def fill(d):
        d['account_financial_facts']['buys'][0].update(filled_quantity='8', quantity='12', reserved_usd='4.8')
        d['account_financial_facts']['inventory_cost_usd'] = '3.2'
    engine._auto_pool._update(fill)
    with pytest.raises(ValueError, match='rotation_awaiting_reconciliation'):
        engine._auto_pool._rotation_session(engine.lp_auto_state()['account_buys'][0])
    assert exchange.cancels == []


def test_rotation_reanchors_same_price_survivor_without_resetting_protection(tmp_path, monkeypatch):
    from tests import test_lp_auto_rotation as rotation
    monkeypatch.setattr(pool, 'Exchange', rotation.RotationExchange)
    engine, exchange, lp, store = pool.setup(tmp_path, 2)
    engine.lp_auto_configure(dict(budget_usd='16', target_buy_count=1))
    exchange.orders = [dict(order_id=oid, token_id='m00', condition_id='m00', market_id='m00',
        outcome='YES', side='BUY', status='LIVE', price='.4', original_size='20', remaining_size='20', size_matched='0')
        for oid in ('rank-b', 'rank-a')]
    exchange.rewards['m00'] = Decimal('30')
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    session = store.lp_active_sessions()[0]
    before = lp._queue_protection_levels(session)
    assert len(before) == 1
    key, bucket = next(iter(before.items()))
    assert bucket['order_id'] == 'rank-b'
    assert bucket['state'] == 'registered'
    assert lp._queue_protection_gate_open(session)
    rotation.refresh(lp, exchange, 2)
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once()
    assert exchange.cancels == ['rank-b']
    after = store.lp_session(session['session_id'])
    assert lp._queue_protection_levels(after)[key] == {**bucket, 'order_id': 'rank-a'}
    assert lp._queue_protection_gate_open(after)
    assert not lp._order_cancel_requested(after, 'rank-a')
    assert lp._order_cancel_requested(after, 'rank-b')
    assert exchange.posts == []


@pytest.mark.parametrize('blocked', ['cancel_batch', 'different_price', 'filled', 'unknown'])
def test_rotation_does_not_reanchor_to_unprotected_or_canceling_order(tmp_path, monkeypatch, blocked):
    engine, _, lp, store, _ = rotation_pool(tmp_path, monkeypatch, same_token=True)
    state = engine.lp_auto_state()
    anchor = next(row for row in state['account_buys'] if row['order_id'] == 'o1')
    session = store.lp_session(anchor['session_id'])
    before = lp._queue_protection_levels(session)
    assert next(iter(before.values()))['order_id'] == 'o1'
    if blocked == 'cancel_batch':
        # All victims are registered before per-session cancel flags change.
        lp.begin_order_cancel(['o1', 'manual-second'])
    else:
        history = deepcopy(session['order_history'])
        patch = {'different_price': {'price': '.39'}, 'filled': {'size_matched': '1'},
                 'unknown': {'status': 'UNKNOWN'}}[blocked]
        history['manual-second'].update(patch)
        store.lp_update_session(session['session_id'], patch={'order_history': history})
    engine._auto_pool._mark_rotation_cancel_requested(anchor)
    after = store.lp_session(session['session_id'])
    for key, bucket in lp._queue_protection_levels(after).items():
        if before[key]['order_id'] == 'o1':
            assert bucket['order_id'] == 'o1'
            assert bucket['state'] == 'canceling'
            assert bucket['baseline_price'] == before[key]['baseline_price']


def test_manual_account_inventory_does_not_become_known_zero_historical_report(tmp_path):
    engine, _, _, _ = covered_pool(tmp_path, inventory='12', pnl='3')
    result = engine.lp_auto_report_facts(period_start=pool.NOW - timedelta(hours=1), period_end=pool.NOW)
    assert result['state']['intents'] == []
    assert result['funds']['status'] == 'known'
    assert Decimal(result['funds']['inventory_cost_usd']) == 12
    assert Decimal(result['funds']['realized_pnl_usd']) == 3
    period = result['financial_period']
    assert period['status'] == 'unknown'
    assert period['reason'] == 'account_historical_boundary_unavailable'
    assert period['inventory_cost_usd'] is None
    assert period['inventory_quantity'] is None
    assert period['realized_pnl_usd'] is None
    assert 'inventories' not in period


@pytest.mark.parametrize('publication_timing', ['during_read', 'after_wait'])
def test_fresh_dashboard_publication_recovers_pool_wait_without_another_helper_read(tmp_path, monkeypatch, publication_timing):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from open_trader.polymarket_lp import LpObservationWait
    engine, exchange, lp, _ = pool.setup(tmp_path)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    auto = engine._auto_pool
    entered, release = Event(), Event()
    reads = []
    def delayed_failure(**kwargs):
        reads.append(kwargs)
        entered.set()
        assert release.wait(5)
        raise LpObservationWait('account_round_invalid')
    exchange.lp_account_snapshot_shared = delayed_failure
    def publish_dashboard():
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
        assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    with ThreadPoolExecutor(1) as workers:
        pending = workers.submit(auto._refresh_account_facts)
        try:
            assert entered.wait(3)
            if publication_timing == 'during_read':
                # Capture the baseline before read I/O, not at failed completion.
                publish_dashboard()
        finally:
            release.set()
        assert pending.result(timeout=3) is False
    if publication_timing == 'after_wait':
        assert 'account_round_invalid' in auto.state()['admission_block_reasons']
        publish_dashboard()
    assert len(reads) == 1
    state = auto.state()
    assert 'account_round_invalid' not in state['admission_block_reasons']
    assert state['funds']['status'] == 'known'
    assert Decimal(state['funds']['spendable_usd']) == 100


@pytest.mark.parametrize('older_outcome', ['success', 'wait', 'error'])
def test_older_pool_success_cannot_clear_newer_concurrent_wait(tmp_path, monkeypatch, older_outcome):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from open_trader.polymarket_lp import LpObservationWait
    engine, exchange, lp, _ = pool.setup(tmp_path)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
    old_success = pool._fresh_registration_bundle(exchange, lp)
    entered, release = Event(), Event()
    lock, reads = Lock(), []
    def overlapping(**kwargs):
        with lock:
            index = len(reads)
            reads.append(kwargs)
        if index == 0:
            entered.set()
            assert release.wait(5)
            if older_outcome == 'wait':
                raise LpObservationWait('session_changed')
            if older_outcome == 'error':
                raise TimeoutError('older_endpoint_failure')
            return old_success
        raise LpObservationWait('account_round_invalid')
    exchange.lp_account_snapshot_shared = overlapping
    auto = engine._auto_pool
    baseline = deepcopy(auto._read()['account_financial_facts'])
    with ThreadPoolExecutor(2) as workers:
        old = workers.submit(auto._refresh_account_facts)
        try:
            assert entered.wait(3)
            new = workers.submit(auto._refresh_account_facts)
            assert new.result(timeout=3) is False
            assert 'account_round_invalid' in auto.state()['admission_block_reasons']
        finally:
            release.set()
        old.result(timeout=3)
    assert len(reads) == 2
    assert auto._read()['account_financial_facts'] == baseline
    assert lp._account_order_sync_error is None
    state = auto.state()
    assert 'account_round_invalid' in state['admission_block_reasons']
    assert state['funds']['spendable_usd'] is None


@pytest.mark.parametrize('partial_status', ['known', 'unknown'])
def test_account_overcapacity_cancels_only_unfilled_extra_beside_partial_buy(tmp_path, monkeypatch, partial_status):
    engine, exchange, lp, store, _ = rotation_pool(tmp_path, monkeypatch)
    partial = dict(order_id='partial-buy', token_id='m01', condition_id='m01', side='BUY',
        status='LIVE', price='.4', original_size='20', size_matched='1')
    exchange.orders.append(partial)
    exchange.positions = [dict(token_id='m01', condition_id='m01', size='1')]
    store.lp_create_session('partial-session', 'manual-partial', state='entry_open', payload=dict(
        account_id='test-wallet', condition_id='m01', token_id='m01', market_id='m01', outcome='YES',
        price='.4', quantity='20', entry_order_id='partial-buy', owned_order_ids=['partial-buy'],
        order_history={'partial-buy': partial}, buy_filled_quantity='1', residual_quantity='1'))
    partial_fact = {**buy('partial-buy', token='m01', reserved='7.6'), 'session_id': 'partial-session',
        'quantity': '19', 'filled_quantity': '1', 'financial_status': partial_status}
    def include_partial(d):
        facts = d['account_financial_facts']
        facts['buys'].append(partial_fact)
        facts.update(inventory_cost_usd='.4', trade_generation=store.lp_trade_generation(),
                     financial_status=partial_status, reason_codes=[] if partial_status == 'known' else ['trade_fee_unknown'])
    engine._auto_pool._update(include_partial)
    before = engine.lp_auto_state()
    assert before['slots']['occupied'] == 2 and before['target_buy_count'] == 1
    state = engine.lp_auto_run_once()
    assert state['slots']['occupied'] == 2
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('.4')
    assert len(exchange.posts) == 1
    assert not store.lp_session('partial-session').get('entry_cancel_requested')
    if partial_status == 'unknown':
        assert exchange.cancels == []
        assert state['funds']['spendable_usd'] is None
    else:
        assert exchange.cancels == ['o1']
        assert state['slots']['canceling'] == 1
        assert state['slots']['active'] == 1
        exchange.orders[0]['status'] = 'CANCELED'
        def terminal(d):
            d['account_financial_facts'].update(buys=[partial_fact], order_fills={'o1': '0', 'partial-buy': '1'},
                trade_generation=store.lp_trade_generation())
        engine._auto_pool._update(terminal)
        settled = engine.lp_auto_run_once()
        assert settled['slots']['occupied'] == 1
        assert Decimal(settled['funds']['buy_reserved_usd']) == Decimal('7.6')
        assert Decimal(settled['funds']['inventory_cost_usd']) == Decimal('.4')
        assert exchange.cancels == ['o1'] and len(exchange.posts) == 1


def test_older_captured_document_cannot_supersede_newer_wait(tmp_path, monkeypatch):
    from open_trader.polymarket_lp import LpObservationWait
    engine, exchange, lp, _ = pool.setup(tmp_path)
    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=1))
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    auto = engine._auto_pool
    older = auto._read()
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
    assert lp.register_account_snapshot(pool._fresh_registration_bundle(exchange, lp))['state'] == 'registered'
    def invalid(**kwargs):
        raise LpObservationWait('account_round_invalid')
    exchange.lp_account_snapshot_shared = invalid
    assert auto._refresh_account_facts() is False
    assert 'account_round_invalid' in auto._projection(older)['admission_block_reasons']
    assert auto._projection(older)['funds']['spendable_usd'] is None
