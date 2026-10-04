"""Offline account-fact accounting and temporary reservation coverage contracts."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

NOW = datetime(2026, 10, 3, 7, 0, tzinfo=UTC)
WALLET = '0x' + 'a' * 40
POOL = hashlib.sha256(WALLET.encode()).hexdigest()


def snapshot(**patch):
    return {**dict(authenticated=True, account_id=WALLET, wallet_address=WALLET,
        balance_complete=True, open_orders_complete=True, positions_complete=True,
        trades_complete=True, pagination_complete=True, balance='100', allowance='100',
        read_started_at=NOW-timedelta(seconds=2), checked_at=NOW-timedelta(seconds=1),
        read_ended_at=NOW, trade_generation=0, open_orders=[], positions=[], raw_trades=[]), **patch}


def order(oid='manual', **patch):
    value = dict(order_id=oid, token_id='token', condition_id='condition', side='BUY',
        price='0.4', original_size='10', size_matched='0', status='LIVE')
    return {**value, **patch}


def trade(tid='fill', oid='manual', *, side='BUY', size='3', price='0.4', fee='0', at=NOW-timedelta(minutes=2)):
    return dict(id=tid, market='condition', asset_id='token', status='CONFIRMED',
        trader_side='MAKER', side=side, taker_order_id='foreign', size=size, price=price,
        match_time=at, maker_orders=[dict(order_id=oid, asset_id='token', side=side,
            matched_amount=size, price=price, maker_address=WALLET, fee_rate_bps=fee)])


def build(**patch):
    from open_trader.polymarket_lp_accounting import build_account_financial_facts
    return build_account_financial_facts(snapshot(**patch), now=NOW, expected_account_id=WALLET)


def test_financial_facts_count_manual_partial_and_canceling_buys_once():
    rows = [order(size_matched='3'), order('cancel', status='CANCELING', original_size='2'),
        order('done', status='CANCELED', original_size='2', size_matched='0'), order('sell', side='SELL')]
    fill = trade()
    position = dict(token_id='token', condition_id='condition', size='3', average_price='0.4')
    facts = build(open_orders=rows, raw_trades=[fill, fill], positions=[position, position])
    assert facts['financial_status'] == 'known'
    assert facts['inventory_cost_usd'] == '1.2'
    assert facts['realized_pnl_usd'] == '0'
    assert len(facts['buys']) == 2
    assert sum(Decimal(row['reserved_usd']) for row in facts['buys']) == Decimal('3.6')
    assert {row['state'] for row in facts['buys']} == {'active', 'canceling'}


@pytest.mark.parametrize('patch, reason', [
    ({'positions': [dict(token_id='token', size='3')]}, 'account_position_cost_unknown'),
    ({'positions': [dict(token_id='token', size='3', average_price='NaN')]}, 'account_position_cost_unknown'),
    ({'positions': [dict(token_id='token', size='3', initial_value='-1')]}, 'account_position_cost_unknown'),
    ({'open_orders': [order(status='FILLED')]}, 'order_fill_coverage_unknown'),
])
def test_uncertain_economics_never_publish_zero_cost(patch, reason):
    facts = build(**patch)
    assert facts['financial_status'] == 'unknown'
    assert reason in facts['reason_codes']
    assert facts['inventory_cost_usd'] is None


def test_realized_pnl_releases_chronological_cost_once():
    fills = [trade('buy', size='10'), trade('sell', oid='sell', side='SELL', size='4', price='0.6', at=NOW-timedelta(minutes=1))]
    facts = build(raw_trades=fills, positions=[dict(token_id='token', size='6', average_price='0.5')])
    assert facts['financial_status'] == 'known'
    assert Decimal(facts['inventory_cost_usd']) == Decimal('3.0')
    assert Decimal(facts['realized_pnl_usd']) == Decimal('0.8')


def test_invalid_round_rejects_without_financial_facts():
    from open_trader.polymarket_lp_accounting import build_account_financial_facts
    for patch in ({'read_started_at': NOW+timedelta(seconds=1)}, {'positions_complete': False},
                  {'account_id': 'foreign'}, {'trade_generation': True}):
        with pytest.raises(ValueError):
            build_account_financial_facts({**snapshot(), **patch}, now=NOW, expected_account_id=WALLET)


def seed(store, *, finished=NOW-timedelta(seconds=3), stage='send_unknown', account=WALLET):
    payload = dict(account_id=account, token_id='token', condition_id='condition',
        submit_status='unknown', submit_stage=stage, post_started=True,
        submit_requested_at=(NOW-timedelta(seconds=5)).isoformat())
    if finished is not None:
        payload['submit_finished_at'] = finished.isoformat()
    store.lp_create_session('session', 'intent', state='needs_attention', payload=payload)
    document = dict(account_id=POOL, intents={'intent': dict(intent_id='intent', session_id='session',
        state='unknown', reserved_usd='4', financial_status='unknown')}, events={'audit': {'kind': 'unknown'}})
    with store._transaction() as connection:
        connection.execute('INSERT INTO lp_auto_pool(singleton,payload) VALUES (1,?)', (json.dumps(document),))
    return document


def read_pool(store):
    with store._read_connection() as connection:
        return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])


def test_coverage_is_atomic_durable_idempotent_and_preserves_unknown_audit(tmp_path):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    document = seed(store)
    facts = build()
    with pytest.raises(RuntimeError):
        with store._transaction() as connection:
            store.lp_publish_account_financial_facts(facts, connection=connection, expected_generation=0)
            raise RuntimeError('rollback')
    assert read_pool(store) == document
    store.lp_publish_account_financial_facts(facts, expected_generation=0)
    saved = read_pool(store)
    marker = saved['intents']['intent']['reservation_coverage']
    assert marker['state'] == 'covered'
    assert saved['intents']['intent']['state'] == 'unknown'
    assert saved['intents']['intent']['reserved_usd'] == '4'
    assert saved['events'] == document['events']
    assert store.lp_session('session')['submit_status'] == 'unknown'
    assert store.lp_session('session')['reservation_coverage'] == marker
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    store.lp_publish_account_financial_facts(facts, expected_generation=0)
    assert read_pool(store) == saved


@pytest.mark.parametrize('finished,stage,account', [
    (NOW-timedelta(seconds=2), 'send_unknown', WALLET),
    (NOW, 'send_unknown', WALLET),
    (None, 'sending', WALLET),
    (None, 'send_unknown', WALLET),
    (NOW-timedelta(seconds=3), 'send_unknown', 'foreign'),
])
def test_coverage_retains_inflight_new_or_unproved_reservations(tmp_path, finished, stage, account):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store, finished=finished, stage=stage, account=account)
    store.lp_publish_account_financial_facts(build(), expected_generation=0)
    assert 'reservation_coverage' not in read_pool(store)['intents']['intent']


def test_publication_rejects_older_snapshot_or_changed_generation(tmp_path):
    from open_trader.polymarket_lp_errors import LpObservationWait
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store)
    store.lp_publish_account_financial_facts(build(), expected_generation=0)
    before = read_pool(store)
    older = build()
    older['read_started_at'] = (NOW-timedelta(seconds=4)).isoformat()
    older['read_ended_at'] = (NOW-timedelta(seconds=3)).isoformat()
    older['checked_at'] = older['read_ended_at']
    with pytest.raises(LpObservationWait):
        store.lp_publish_account_financial_facts(older, expected_generation=0)
    assert read_pool(store) == before
    store.lp_advance_trade_generation(0)
    with pytest.raises(LpObservationWait):
        store.lp_publish_account_financial_facts(build(), expected_generation=0)
    assert read_pool(store) == before


@pytest.mark.parametrize('existing_pnl,expected', [(None, '0'), ('-2', '-2'), ('2', '2')])
def test_account_pnl_boundary_excludes_historical_profit_preserves_existing_pnl(tmp_path, existing_pnl, expected):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store)
    if existing_pnl is not None:
        document = read_pool(store)
        document['allocations'] = [{'delta_usd': '10'}]
        document['intents']['intent'].update(realized_pnl_usd=existing_pnl, financial_status='known')
        with store._transaction() as connection:
            connection.execute('UPDATE lp_auto_pool SET payload=?', (json.dumps(document),))
    fills = [trade('buy', size='10'), trade('sell', oid='sell', side='SELL', size='10', price='0.6', at=NOW-timedelta(minutes=1))]
    store.lp_publish_account_financial_facts(build(raw_trades=fills), expected_generation=0)
    facts = read_pool(store)['account_financial_facts']
    assert Decimal(facts['lifetime_realized_pnl_usd']) == Decimal('2')
    assert Decimal(facts['realized_pnl_usd']) == Decimal(expected)


def test_fresh_market_fee_metadata_matches_existing_maker_fee_rules():
    fill = trade(fee='100')
    position = dict(token_id='token', size='3', average_price='0.4')
    facts = build(raw_trades=[fill], positions=[position], fee_metadata_by_token={
        'token': dict(fees_checked_at=NOW, fees_enabled=True, fee='0')})
    assert facts['financial_status'] == 'known'
    assert Decimal(facts['inventory_cost_usd']) == Decimal('1.2')
    stale = build(raw_trades=[fill], positions=[position], fee_metadata_by_token={
        'token': dict(fees_checked_at=NOW-timedelta(minutes=1), fees_enabled=True, fee='0')})
    assert stale['financial_status'] == 'known'
    assert stale['report_status'] == 'unknown'
    assert 'trade_fee_unknown' in stale['report_reason_codes']


def test_coverage_does_not_follow_changed_session_account_or_late_trade_callback(tmp_path):
    from open_trader.polymarket_lp_accounting import reservation_is_covered
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store)
    store.lp_publish_account_financial_facts(build(), expected_generation=0)
    intent = read_pool(store)['intents']['intent']
    store.lp_register_trade_change('session')
    assert read_pool(store)['intents']['intent'] == intent
    session = store.lp_session('session')
    assert reservation_is_covered(session)
    assert not reservation_is_covered({**session, 'account_id': 'foreign'})
    assert not reservation_is_covered(session, 'foreign')


def test_legacy_receipt_action_proves_end_but_pending_action_does_not(tmp_path):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store, finished=None, account='')
    with store._transaction() as connection:
        connection.execute("UPDATE lp_sessions SET idempotency_key='lp-auto:intent'")
    store.lp_upsert_action('session', 'session:entry-submit', state='unknown', payload={'role': 'entry'})
    with store._transaction() as connection:
        connection.execute('UPDATE lp_actions SET updated_at=?', ((NOW-timedelta(seconds=3)).isoformat(),))
    facts = build()
    facts['trade_generation'] = store.lp_trade_generation()
    store.lp_publish_account_financial_facts(facts)
    marker = read_pool(store)['intents']['intent']['reservation_coverage']
    assert marker['basis'] == 'legacy_finished_action'


def test_empty_covered_management_session_retires_without_claiming_submit_result(tmp_path):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store)
    store.lp_publish_account_financial_facts(build(), expected_generation=0)
    session = store.lp_session('session')
    assert session['state'] == 'complete'
    assert session['submit_status'] == 'unknown'
    assert session['submit_stage'] == 'send_unknown'
    assert session['account_coverage_retired']['reason'] == 'account_observation_no_exposure'
    assert read_pool(store)['intents']['intent']['state'] == 'unknown'
    assert not store.lp_active_sessions()


def test_covered_management_session_with_independent_unknown_action_is_not_retired(tmp_path):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store)
    store.lp_upsert_action('session', 'session:augment', state='unknown', payload={'role': 'augment', 'side': 'BUY'})
    facts = build()
    facts['trade_generation'] = store.lp_trade_generation()
    store.lp_publish_account_financial_facts(facts)
    session = store.lp_session('session')
    assert session['state'] == 'needs_attention'
    assert 'account_coverage_retired' not in session
    assert read_pool(store)['account_financial_facts']['financial_status'] == 'unknown'
    assert 'account_send_inflight' in read_pool(store)['account_financial_facts']['reason_codes']


def test_manual_inflight_entry_without_auto_intent_blocks_account_admission(tmp_path):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store, finished=None, stage='sending')
    with store._transaction() as connection:
        document = read_pool(store)
        document['intents'] = {}
        connection.execute('UPDATE lp_auto_pool SET payload=?', (json.dumps(document),))
    store.lp_upsert_action('session', 'session:entry-submit', state='pending',
        payload={'role': 'entry', 'side': 'BUY', 'submit_stage': 'sending'})
    facts = build()
    facts['trade_generation'] = store.lp_trade_generation()
    store.lp_publish_account_financial_facts(facts)
    assert read_pool(store)['account_financial_facts']['financial_status'] == 'unknown'
    assert 'account_send_inflight' in read_pool(store)['account_financial_facts']['reason_codes']
    assert read_pool(store)['intents'] == {}


@pytest.mark.parametrize('inventory_only', [False, True])
def test_covered_unknown_request_resumes_real_owned_exposure_without_rewriting_audit(tmp_path, inventory_only):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store)
    real = order('actual-order', original_size='3', size_matched='3' if inventory_only else '0',
                 status='FILLED' if inventory_only else 'LIVE')
    store.lp_register_exchange_orders(WALLET, 'token', [real], expected_generation=0)
    facts = (build(raw_trades=[trade(oid='actual-order')], positions=[dict(token_id='token', size='3', average_price='0.4')])
             if inventory_only else build(open_orders=[real]))
    store.lp_publish_account_financial_facts(facts, expected_generation=0)
    session = store.lp_session('session')
    assert session['state'] == 'entry_open'
    assert session['resume_state'] == 'entry_open'
    assert session['submit_status'] == 'unknown'
    assert session['submit_stage'] == 'send_unknown'
    assert session['account_coverage_management']['reason'] == 'account_observation_owned_exposure'
    assert not session.get('position_reconciled')
    assert 'buy_fees' not in session
    assert read_pool(store)['intents']['intent']['state'] == 'unknown'
    assert read_pool(store)['intents']['intent'].get('order_id') is None


@pytest.mark.parametrize('patch', [
    {'stop_requested': True}, {'stop_loss_latched': True}, {'entry_cancel_requested': True},
    {'resume_state': 'stop_loss_exit'}, {'reconciliation': 'independent_sell_unknown'},
    {'facts_error': 'order_identity_conflict'}, {'passive_exit_attempt_state': 'unknown'},
])
def test_coverage_never_resumes_independently_blocked_management(tmp_path, patch):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store)
    store.lp_update_session('session', patch=patch)
    real = order('actual-order')
    store.lp_register_exchange_orders(WALLET, 'token', [real], expected_generation=0)
    store.lp_publish_account_financial_facts(build(open_orders=[real]), expected_generation=0)
    session = store.lp_session('session')
    assert session['state'] == 'needs_attention'
    assert 'account_coverage_management' not in session
    for key, value in patch.items():
        assert session[key] == value


def test_known_account_coverage_does_not_resume_independent_unknown_sell(tmp_path):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store)
    real = order('actual-order')
    store.lp_register_exchange_orders(WALLET, 'token', [real], expected_generation=0)
    store.lp_upsert_action('session', 'session:exit', state='unknown',
        payload={'role': 'passive_exit', 'side': 'SELL', 'submit_finished_at': (NOW-timedelta(seconds=3)).isoformat()})
    facts = build(open_orders=[real])
    facts['trade_generation'] = store.lp_trade_generation()
    store.lp_publish_account_financial_facts(facts)
    assert read_pool(store)['account_financial_facts']['financial_status'] == 'known'
    session = store.lp_session('session')
    assert session['state'] == 'needs_attention'
    assert 'account_coverage_management' not in session
    assert store.lp_actions('session')[0]['state'] == 'unknown'


@pytest.mark.parametrize('new_active_group', [False, True])
def test_late_explicit_receipt_reuses_retired_empty_owner_without_restoring_hold(tmp_path, new_active_group):
    from types import SimpleNamespace
    from open_trader.polymarket_lp import PolymarketLPService
    path = tmp_path / 'ledger.sqlite'
    store = PredictionArbitrageStore(path)
    seed(store)
    store.lp_publish_account_financial_facts(build(), expected_generation=0)
    original_intent = read_pool(store)['intents']['intent']
    if new_active_group:
        store.lp_create_session('new-owner', 'new-group', state='entry_open',
            payload={'account_id': WALLET, 'token_id': 'token', 'condition_id': 'condition'})
    # The accepted-action write owns the direct receipt's generation fence.
    store.lp_register_trade_change('session')
    exchange = SimpleNamespace(config=SimpleNamespace(wallet_address=WALLET))
    service = PolymarketLPService(store, exchange, clock=lambda: NOW)
    received, changed = service._register_direct_receipt(store.lp_session('session'),
        order_id='late-real-id', response={'status': 'LIVE', 'size_matched': '0'},
        side='BUY', price=Decimal('0.4'), quantity=Decimal('10'))
    expected_owner = 'new-owner' if new_active_group else 'session'
    assert received['session_id'] == expected_owner
    assert received['state'] == 'entry_open'
    assert received['owned_order_ids'] == ['late-real-id']
    assert changed is True
    assert store.lp_session('session')['submit_status'] == 'unknown'
    assert read_pool(store)['intents']['intent'] == original_intent
    assert len(store.lp_active_sessions()) == 1
    store = PredictionArbitrageStore(path)
    service = PolymarketLPService(store, exchange, clock=lambda: NOW)
    replay, changed = service._register_direct_receipt(store.lp_session('session'),
        order_id='late-real-id', response={'status': 'LIVE', 'size_matched': '0'},
        side='BUY', price=Decimal('0.4'), quantity=Decimal('10'))
    assert replay['session_id'] == expected_owner
    assert replay['owned_order_ids'] == ['late-real-id']
    assert changed is False
    assert read_pool(store)['intents']['intent'] == original_intent
    assert len(store.lp_active_sessions()) == 1


@pytest.mark.parametrize('request_field', ['augment_cancel_requested', 'owned_cancel_requested', None])
def test_accepted_nonentry_cancel_absence_uses_current_account_buys(tmp_path, request_field):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store)
    orders = [order('first'), order('victim')]
    store.lp_register_exchange_orders(WALLET, 'token', orders, expected_generation=0)
    store.lp_publish_account_financial_facts(build(open_orders=orders), expected_generation=0)
    if request_field:
        store.lp_update_session('session', patch={request_field: ['victim']})
    store.lp_upsert_action('session', 'session:augment-cancel:victim', state='accepted',
        payload={'role': 'augment-cancel', 'order_id': 'victim'})
    generation = store.lp_trade_generation()
    # Use the next actual observation, keeping its clock fixed across replay.
    facts = build(open_orders=[orders[0]], trade_generation=generation,
        read_started_at=NOW-timedelta(seconds=1), checked_at=NOW, read_ended_at=NOW)
    published = store.lp_publish_account_financial_facts(facts, expected_generation=generation)
    assert [row['order_id'] for row in published['buys']] == ['first']
    assert published['financial_status'] == 'known'
    assert store.lp_actions('session')[-1]['state'] == 'accepted'
    store.lp_register_exchange_orders(WALLET, 'token', [order('victim', status='CANCELED')],
        expected_generation=generation)
    published = store.lp_publish_account_financial_facts(facts, expected_generation=generation)
    assert [row['order_id'] for row in published['buys']] == ['first']
    assert published['financial_status'] == 'known'


@pytest.mark.parametrize('trader_side', ['MAKER', 'TAKER'])
def test_ambiguous_historical_maker_does_not_override_current_account_exposure(tmp_path, trader_side):
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store)
    raw = trade()
    raw['trader_side'] = trader_side
    raw['fee_rate_bps'] = '0'
    raw['maker_orders'][0].pop('maker_address')
    facts = build(raw_trades=[raw], positions=[dict(token_id='token', size='3', average_price='0.4')] if trader_side == 'TAKER' else [])
    assert facts['financial_status'] == 'known'
    assert facts['report_status'] == 'unknown'
    assert 'trade_ownership_unknown' in facts['report_reason_codes']
    assert Decimal(facts['inventory_cost_usd']) == (Decimal('1.2') if trader_side == 'TAKER' else 0)
    store.lp_publish_account_financial_facts(facts, expected_generation=0)
    assert 'reservation_coverage' in read_pool(store)['intents']['intent']
    assert read_pool(store)['intents']['intent']['reserved_usd'] == '4'
    assert read_pool(store)['intents']['intent']['state'] == 'unknown'


def test_explicit_foreign_maker_rows_are_excluded_from_account_economics():
    raw = trade()
    raw['maker_orders'][0]['maker_address'] = 'foreign-wallet'
    facts = build(raw_trades=[raw])
    assert facts['financial_status'] == 'known'
    assert Decimal(facts['inventory_cost_usd']) == 0
    assert facts['order_fills'] == {}


@pytest.mark.parametrize('change', ['missing', 'price', 'quantity', 'fee'])
def test_disappearing_confirmed_history_cannot_erase_closed_account_loss(tmp_path, change):
    from copy import deepcopy
    from open_trader.polymarket_lp_accounting import build_account_financial_facts
    store = PredictionArbitrageStore(tmp_path / 'ledger.sqlite')
    seed(store)
    def observe(stamp, trades):
        facts = build_account_financial_facts(snapshot(raw_trades=trades,
            read_started_at=stamp-timedelta(seconds=2), checked_at=stamp-timedelta(seconds=1),
            read_ended_at=stamp), now=stamp, expected_account_id=WALLET)
        return store.lp_publish_account_financial_facts(facts, expected_generation=0)
    observe(NOW, [])
    trades = [trade('buy', oid='buy', size='10'),
        trade('sell', oid='sell', side='SELL', size='10', price='0.2', at=NOW-timedelta(minutes=1))]
    if change == 'fee':
        trades[0]['maker_orders'][0]['fee'] = '1'
    expected_loss = -3 if change == 'fee' else -2
    known_loss = observe(NOW+timedelta(seconds=5), trades)
    assert Decimal(known_loss['realized_pnl_usd']) == expected_loss
    baseline = read_pool(store)['account_realized_pnl_baseline_usd']
    changed = deepcopy(trades)
    if change == 'missing':
        changed = []
    elif change == 'price':
        changed[1]['maker_orders'][0]['price'] = '0.3'
    elif change == 'quantity':
        for raw in changed:
            raw['maker_orders'][0]['matched_amount'] = '5'
    elif change == 'fee':
        changed[0]['maker_orders'][0]['fee'] = '0'
    incomplete = observe(NOW+timedelta(seconds=10), changed)
    assert incomplete['financial_status'] == 'known'
    assert incomplete['report_status'] == 'unknown'
    reason = 'account_trade_history_incomplete' if change == 'missing' else 'account_trade_history_conflict'
    assert reason in incomplete['report_reason_codes']
    assert incomplete['realized_pnl_usd'] is None
    assert read_pool(store)['account_realized_pnl_baseline_usd'] == baseline
    restored = observe(NOW+timedelta(seconds=15), trades)
    assert restored['financial_status'] == 'known'
    assert Decimal(restored['realized_pnl_usd']) == expected_loss
