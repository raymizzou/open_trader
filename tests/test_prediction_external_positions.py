from dataclasses import replace
from types import SimpleNamespace

import pytest

from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from tests.test_prediction_arbitrage_execution import execution_fixture


WALLET = '0x' + '1' * 40
POSITION = {'token_id': 'old-token', 'size': '13.31', 'condition_id': 'old-market'}


def setup_incident(tmp_path):
    service, trading, store, _ = execution_fixture(tmp_path)
    trading.config = SimpleNamespace(wallet_address=WALLET)
    trading.positions = (dict(POSITION),)
    assert service.reconcile_startup()['reason'] == 'unknown_external_state'
    incident = store.unacknowledged_incident()
    return service, trading, store, incident


def register(service, incident, **kwargs):
    return service.register_external_positions(
        incident['incident_id'], {'old-token': '13.31'},
        confirm=True, note='Operator confirmed pre-migration unmanaged inventory', **kwargs,
    )


def test_external_inventory_recovers_startup_without_adopting_or_trading(tmp_path):
    service, trading, store, incident = setup_incident(tmp_path)
    assert register(service, incident)['state'] == 'registered'
    row = store.histories('incidents')[0]
    assert row['positions'] == incident['positions']
    assert row['acknowledgement']['reconciliation'] == 'external_positions_registered'
    assert row['acknowledgement']['positions'] == [{'token_id': 'old-token', 'quantity': '13.31'}]
    restarted = PredictionExecutionService(
        store=store, trading=trading, monitor=service._monitor,
        notifier=service._notifier, lock_path=tmp_path / 'execution.lock',
    )
    assert restarted.reconcile_startup()['state'] == 'ready'
    assert restarted.lp_mutation_allowed()
    assert store.lp_active_sessions() == []
    assert store.active_execution() is None
    assert trading.positions == (POSITION,)
    assert trading.batch_calls == trading.merge_calls == 0
    # The same raw position still excludes its market from new LP entry.
    from open_trader.polymarket_lp_risk import _has_market_order
    assert _has_market_order({'positions': [POSITION]},
                             {'market_id': 'old-market', 'condition_id': 'old-market', 'token_id': 'old-token'})


@pytest.mark.parametrize('change', ['increase', 'new_token', 'other_wallet'])
def test_registration_is_account_and_quantity_bounded(tmp_path, change):
    service, trading, store, incident = setup_incident(tmp_path)
    assert register(service, incident)['state'] == 'registered'
    if change == 'increase':
        trading.positions = ({**POSITION, 'size': '13.32'},)
    elif change == 'new_token':
        trading.positions = (POSITION, {**POSITION, 'token_id': 'new-token'})
    else:
        trading.config = SimpleNamespace(wallet_address='0x' + '2' * 40)
    assert service.reconcile_startup()['state'] == 'locked'


@pytest.mark.parametrize('change', ['stale', 'open_order', 'wrong_wallet', 'quantity', 'active_lp', 'wrong_incident'])
def test_registration_denies_changed_or_unsafe_state(tmp_path, change):
    service, trading, store, incident = setup_incident(tmp_path)
    if change == 'stale':
        trading.account_fresh = False
    elif change == 'open_order':
        read = trading.account_snapshot
        trading.account_snapshot = lambda: replace(read(), open_order_ids=('live-order',))
    elif change == 'wrong_wallet':
        trading.config = SimpleNamespace(wallet_address='0x' + '2' * 40)
    elif change == 'quantity':
        trading.positions = ({**POSITION, 'size': '14'},)
    elif change == 'active_lp':
        store.lp_active_sessions = lambda: [{'session_id': 'existing'}]
    else:
        store.update_incident(incident['incident_id'], {'reason': 'submit_unknown'})
    assert register(service, incident)['state'] == 'locked'
    assert store.unacknowledged_incident() is not None
    assert trading.batch_calls == trading.merge_calls == 0


def test_registration_requires_confirmation(tmp_path):
    service, trading, store, incident = setup_incident(tmp_path)
    result = service.register_external_positions(incident['incident_id'], {'old-token': '13.31'}, confirm=False, note='operator')
    assert result['state'] == 'locked'
    assert store.unacknowledged_incident() is not None


@pytest.mark.parametrize('busy', [False, True])
def test_offline_cli_recovers_only_without_live_runtime(tmp_path, monkeypatch, busy):
    from open_trader import cli
    from open_trader.prediction_runtime import _RuntimeOwnershipLock
    from open_trader import prediction_runtime
    service, trading, store, incident = setup_incident(tmp_path)
    monkeypatch.setattr(prediction_runtime, 'load_trading_config', lambda path: trading.config)
    calls = []
    def client(config):
        calls.append(config)
        return trading
    monkeypatch.setattr(cli.PolymarketTradingClient, 'from_keychain', client)
    lock = _RuntimeOwnershipLock(tmp_path / 'data/prediction_arbitrage/runtime.lock')
    if busy:
        lock.acquire()
    try:
        code = cli.main([
            'prediction-arb', 'recover-external-positions', '--data-dir', str(tmp_path / 'data'),
            '--incident-id', incident['incident_id'], '--position', 'old-token=13.31',
            '--confirm', '--note', 'Confirmed migration inventory',
        ])
        assert code == (2 if busy else 0)
        assert len(calls) == (0 if busy else 1)
        assert (store.unacknowledged_incident() is not None) == busy
    finally:
        lock.release()


def test_reduced_inventory_allowed_but_duplicate_rows_cannot_exceed_limit(tmp_path):
    service, trading, store, incident = setup_incident(tmp_path)
    assert register(service, incident)['state'] == 'registered'
    trading.positions = ({**POSITION, 'size': '8'},)
    assert service.reconcile_startup()['state'] == 'ready'
    trading.positions = ({**POSITION, 'size': '8'}, {**POSITION, 'size': '8'})
    assert service.reconcile_startup()['reason'] == 'unknown_external_state'


def test_registration_ignores_already_settled_zero_value_rows(tmp_path):
    service, trading, store, incident = setup_incident(tmp_path)
    trading.positions += ({'token_id': 'settled', 'size': '60', 'current_value': '0', 'redeemable': 'True'},)
    assert register(service, incident)['state'] == 'registered'
    assert service.reconcile_startup()['state'] == 'ready'


@pytest.mark.parametrize('variant', ['valid', 'price_one', 'buy', 'oversize', 'incomplete', 'wrong_wallet', 'different_id'])
def test_explicit_external_sell_is_preserved_but_other_orders_block(tmp_path, variant):
    from datetime import UTC, datetime
    service, trading, store, incident = setup_incident(tmp_path)
    read = trading.account_snapshot
    trading.account_snapshot = lambda: replace(read(), open_order_ids=('external-sell',))
    order = {'order_id': 'external-sell', 'token_id': 'old-token', 'side': 'SELL',
             'price': '0.30', 'remaining_size': '13.31', 'status': 'LIVE'}
    orders = {'authenticated': True, 'open_orders_complete': True,
              'wallet_address': WALLET, 'checked_at': datetime.now(UTC), 'open_orders': [order]}
    if variant == 'price_one':
        order['price'] = '1'
    elif variant == 'buy':
        order['side'] = 'BUY'
    elif variant == 'oversize':
        order['remaining_size'] = '14'
    elif variant == 'incomplete':
        orders['open_orders_complete'] = False
    elif variant == 'wrong_wallet':
        orders['wallet_address'] = '0x' + '2' * 40
    elif variant == 'different_id':
        order['order_id'] = 'another-order'
    trading.lp_open_orders_snapshot = lambda: orders
    result = register(service, incident, external_sell_order_ids=('external-sell',))
    if variant in ('valid', 'price_one'):
        assert result['state'] == 'registered'
        assert service.reconcile_startup()['state'] == 'ready'
        assert service.reset_breaker(incident['incident_id'])['state'] == 'ready'
        assert store.histories('incidents')[0]['acknowledgement']['external_sell_orders'] == [order]
    else:
        assert result['state'] == 'locked'
        assert store.unacknowledged_incident() is not None
    assert trading.batch_calls == trading.merge_calls == 0
    assert orders['open_orders'] == [order]


def test_registered_incident_reset_rechecks_account_and_preserves_inventory_record(tmp_path):
    service, trading, store, incident = setup_incident(tmp_path)
    assert register(service, incident)['state'] == 'registered'
    saved = store.histories('incidents')[0]['acknowledgement']
    assert service.reset_breaker(incident['incident_id'])['state'] == 'ready'
    assert store.histories('incidents')[0]['acknowledgement'] == saved
    trading.positions = ({**POSITION, 'size': '14'},)
    assert service.reset_breaker(incident['incident_id'])['state'] == 'locked'
    assert store.histories('incidents')[0]['acknowledgement'] == saved


@pytest.mark.parametrize('mode', ['lp', 'paused'])
@pytest.mark.parametrize('change', ['none', 'increase', 'new', 'other_wallet'])
def test_external_caps_apply_before_startup_early_returns(tmp_path, mode, change):
    service, trading, store, incident = setup_incident(tmp_path)
    assert register(service, incident)['state'] == 'registered'
    if mode == 'lp':
        store.lp_active_sessions = lambda: [{'session_id': 'owned', 'token_id': 'lp-owned'}]
        service._lp = SimpleNamespace(tick=lambda: {'state': 'entry_open'})
        trading.positions += ({'token_id': 'lp-owned', 'size': '3'},)
    else:
        service._n_leg_paused = True
    if change == 'increase':
        trading.positions = ({**POSITION, 'size': '14'},)
    elif change == 'new':
        trading.positions += ({'token_id': 'unapproved', 'size': '1'},)
    elif change == 'other_wallet':
        trading.config = SimpleNamespace(wallet_address='0x' + '2' * 40)
    assert service.reconcile_startup()['state'] == ('ready' if change == 'none' else 'locked')


def test_malformed_incident_returns_locked(tmp_path):
    service, trading, store, incident = setup_incident(tmp_path)
    store.update_incident(incident['incident_id'], {'positions': None})
    assert register(service, incident)['state'] == 'locked'


def test_registered_sell_can_close_and_new_legal_orders_do_not_block_startup(tmp_path):
    from datetime import UTC, datetime
    service, trading, store, incident = setup_incident(tmp_path)
    read = trading.account_snapshot
    order = {'order_id': 'external-sell', 'token_id': 'old-token', 'side': 'SELL',
             'price': '0.30', 'remaining_size': '13.31', 'status': 'LIVE'}
    rows = [order]
    trading.account_snapshot = lambda: replace(read(), open_order_ids=tuple(r['order_id'] for r in rows))
    trading.lp_open_orders_snapshot = lambda: {
        'authenticated': True, 'open_orders_complete': True, 'wallet_address': WALLET,
        'checked_at': datetime.now(UTC), 'open_orders': rows,
    }
    assert register(service, incident, external_sell_order_ids=('external-sell',))['state'] == 'registered'
    rows.append({**order, 'order_id': 'new-manual', 'token_id': 'another-token', 'side': 'BUY'})
    assert service.reconcile_startup()['state'] == 'ready'
    assert service.reset_breaker(incident['incident_id'])['reason'] == 'open_orders'
    rows.remove(order)
    assert service.reconcile_startup()['state'] == 'ready'
    rows.clear()
    assert service.reset_breaker(incident['incident_id'])['state'] == 'ready'
    rows.append({**order, 'side': 'BUY'})  # contradictory facts for the recorded ID
    assert service.reconcile_startup()['reason'] == 'external_orders_changed'
