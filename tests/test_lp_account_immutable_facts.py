"""Public account snapshots must agree with durable exact-ID economics."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.test_lp_account_reservation_reconciliation import runtime
from tests.test_lp_order_registration_contract import TOKEN_ID, _maker_order, _open_order, _trade


def fresh_snapshot(runtime, adapter, store):
    runtime.clock[0] += timedelta(seconds=1)
    return adapter.lp_account_snapshot_shared(max_age_seconds=0, trade_generation_provider=store.lp_trade_generation)


@pytest.mark.parametrize('conflict,expected_reason', [
    ({'price': '0.20'}, 'order_limit_price_conflict'),
    ({'original': '10'}, 'order_original_quantity_conflict'),
])
def test_public_snapshot_rejects_conflicting_original_order_without_reducing_hold(runtime, conflict, expected_reason):
    original = _open_order('immutable-buy', 'BUY', price='0.40', original='20')
    store, adapter, account, lp, execution = runtime(orders=(original,))
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    before = deepcopy(execution._auto_pool._read())
    sid = store.lp_active_sessions()[0]['session_id']
    account.orders = (_open_order('immutable-buy', 'BUY', **{'price': '0.40', 'original': '20', **conflict}),)
    snapshot = fresh_snapshot(runtime, adapter, store)
    assert snapshot['trade_generation'] == before['account_financial_facts']['trade_generation']
    result = lp.register_account_snapshot(snapshot)
    assert result['state'] == 'failed', result
    assert result['reason'] == expected_reason
    assert execution._auto_pool._read() == before
    durable = store.lp_session(sid)['order_history']['immutable-buy']
    assert Decimal(durable['quantity']) == 20
    assert Decimal(durable['price']) == Decimal('0.4')
    state = execution.lp_auto_state()
    assert Decimal(state['funds']['buy_reserved_usd']) == 8
    assert Decimal(state['funds']['available_usd']) == 92
    assert state['funds']['as_of'] == before['account_financial_facts']['checked_at']
    assert state['funds']['spendable_usd'] is None
    assert 'account_order_sync_unknown' in state['admission_block_reasons']
    account.orders = (original,)
    assert lp.register_account_snapshot(fresh_snapshot(runtime, adapter, store))['state'] == 'registered'
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 8
    assert account.posts == account.cancels == []


def test_fill_only_execution_average_can_upgrade_to_actual_limit_and_original_size(runtime):
    fill = _trade('actual-fill', _maker_order('fill-only-buy', 'BUY', '10', '0.35'), size='10')
    positions = ({'condition_id': fill.market, 'token_id': TOKEN_ID, 'size': '10', 'average_price': '0.35'},)
    store, adapter, account, lp, execution = runtime(trades=(fill,), positions=positions)
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    sid = store.lp_active_sessions()[0]['session_id']
    before = store.lp_session(sid)['order_history']['fill-only-buy']
    assert before['quantity'] is None
    assert Decimal(before['average_price']) == Decimal('0.35')
    account.orders = (_open_order('fill-only-buy', 'BUY', price='0.40', original='20', matched='10'),)
    result = lp.register_account_snapshot(fresh_snapshot(runtime, adapter, store))
    assert result['state'] == 'registered', result
    after = store.lp_session(sid)['order_history']['fill-only-buy']
    assert Decimal(after['quantity']) == 20
    assert Decimal(after['price']) == Decimal('0.4')
    assert Decimal(after['average_price']) == Decimal('0.35')
    state = execution.lp_auto_state()
    assert state['funds']['status'] == 'known'
    assert Decimal(state['funds']['buy_reserved_usd']) == 4
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.5')
    assert account.posts == account.cancels == []


def test_public_empty_history_cannot_erase_durable_exact_receipt_fills(runtime):
    store, adapter, account, lp, execution = runtime(orders=(
        _open_order('receipt-buy', 'BUY', price='0.40', original='20'),))
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    sid = store.lp_active_sessions()[0]['session_id']
    # An actual accepted order receipt can precede the raw trade stream. Its
    # cumulative execution is durable even while execution fees are unknown.
    store.lp_register_trade_change(sid)
    lp._register_direct_receipt(store.lp_session(sid), order_id='receipt-buy',
        response={'status': 'PARTIALLY_FILLED', 'size_matched': '5'}, side='BUY',
        price=Decimal('0.40'), quantity=Decimal('20'))
    account.orders = ()
    result = lp.register_account_snapshot(fresh_snapshot(runtime, adapter, store))
    assert result['state'] == 'registered', result
    state = execution.lp_auto_state()
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['available_usd'] is None
    assert 'account_order_fill_history_incomplete' in state['block_reasons']
    assert Decimal(store.lp_session(sid)['order_history']['receipt-buy']['size_matched']) == 5
    assert account.posts == account.cancels == []
