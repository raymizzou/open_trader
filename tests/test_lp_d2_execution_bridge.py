"""Inactive Execution attachment at real scheduler, Store, SDK and native boundaries."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import sqlite3
import threading
import time
from threading import Event

import pytest
from polymarket.models.clob.order_response import AcceptedOrder

from open_trader import polymarket_lp_market_reader, polymarket_trading
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
from open_trader.polymarket_lp_market_calculation import CalculationPolicy
from open_trader.polymarket_lp_scheduler import LPAutoScheduler
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_lp_d2_integration import seed_running, settle_control
from tests.test_lp_order_registration_contract import _adapter, _SDKPublicClient, _Notifier, WALLET, CONDITION_ID, TOKEN_ID
from tests.test_polymarket_lp_risk import _direction

MIXED_WALLET = '0x' + 'aA' * 20


def wallet_case_runtime(tmp_path, monkeypatch):
    now = datetime(2026, 10, 17, 1, tzinfo=UTC)
    class ClockMeta(type):
        def __instancecheck__(cls, value): return isinstance(value, datetime)
    class FixedDateTime(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None): return now if tz is not None else now.replace(tzinfo=None)
    monkeypatch.setattr(polymarket_lp_market_reader, 'datetime', FixedDateTime)
    monkeypatch.setattr(polymarket_trading, 'datetime', FixedDateTime)
    adapter, sdk = _adapter(tmp_path, public_client=_SDKPublicClient(now))
    adapter.config = replace(adapter.config, wallet_address=MIXED_WALLET)
    sdk.positions = (dict(user=WALLET, condition_id=CONDITION_ID, token_id=TOKEN_ID,
        size='2', average_price='.50', redeemable=False),)
    sdk.create_limit_order = lambda **kw: dict(kw, order_type='GTD')
    def post(signed):
        sdk.posts.append(signed)
        return dict(order_id='wallet-case-buy', status='LIVE', accepted=True, taking_amount='0')
    sdk.post_order = post
    store = PredictionArbitrageStore(tmp_path)
    seed_running(store)
    account = '30ffa9b5e1968e54bddadba7ff14c633c7c955a6f41808d9f5633a42c07537dd'
    store.lp_save_current_settings(account, expected_config_version=0,
        changes=dict(budget_usd='40', target_buy_count=1, buy_price_level=1))
    live = LPD2LiveExecution(store, adapter, account_id=MIXED_WALLET, venue='polymarket',
        process_epoch='wallet-case', policy=CalculationPolicy(None), utc_now=lambda: now)
    live.load_settings()
    live.initialize(ValidatedStartAdmission('wallet-case', 'offline-permit'))
    producer = PolymarketLPService(store, adapter, clock=lambda: now)
    return live, producer, sdk, now


@pytest.mark.parametrize('current_buy', [False, True], ids=['empty-orders', 'exact-buy'])
def test_mixed_case_wallet_adapter_reaches_startup_round(tmp_path, monkeypatch, current_buy):
    from tests.test_lp_order_registration_contract import _open_order
    live, producer, sdk, now = wallet_case_runtime(tmp_path, monkeypatch)
    if current_buy:
        sdk.orders = (_open_order('startup-current-buy', 'BUY', price='.40', original='10', matched='3'),)
        cancel = sdk.cancel_orders
        def cancel_and_remove(*, order_ids):
            receipt = cancel(order_ids=order_ids)
            sdk.orders = tuple(o for o in sdk.orders if o.id not in receipt['canceled'])
            return receipt
        sdk.cancel_orders = cancel_and_remove
    try:
        execution = attach(live, producer, tmp_path)
        scheduler = LPAutoScheduler(execution, clock=lambda: now)
        execution.set_lp_auto_scheduler(scheduler)
        assert scheduler.run_due()
        snapshot = live.state.snapshot()
        assert snapshot.observation is not None
        assert snapshot.observation.account_id == MIXED_WALLET
        assert snapshot.observation.source_frame.positions[0]['account_id'] == WALLET
        assert snapshot.positions[0].account_id == MIXED_WALLET
        assert snapshot.positions[0].quantity.value == Decimal('2')
        assert snapshot.observation.calculation.inventory_cost == Decimal('1.00')
        assert snapshot.observation.calculation.buy_reserved == (Decimal('2.80') if current_buy else Decimal('0'))
        assert live.rounds.snapshot().round_number == 1
        assert sdk.posts == []
        assert sdk.cancels == ([('startup-current-buy',)] if current_buy else [])
        assert scheduler.run_due()
        assert sdk.cancels == ([('startup-current-buy',)] if current_buy else [])
        assert sdk.posts == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('unknown', ['balance', 'inventory', 'wrong-wallet'])
def test_case_fix_preserves_persisted_identity_and_unknown_gate(tmp_path, monkeypatch, unknown):
    from tests.test_lp_business_writer_contract import request
    from tests.test_lp_d2_integration import candidates_for_fixed_send
    live, producer, sdk, now = wallet_case_runtime(tmp_path, monkeypatch)
    account = '30ffa9b5e1968e54bddadba7ff14c633c7c955a6f41808d9f5633a42c07537dd'
    audit = request('old-wallet-request', account_id=MIXED_WALLET, process_epoch='old-epoch',
        session_id='old-wallet-audit', condition_id=CONDITION_ID, token_id=TOKEN_ID,
        order_id='historical-buy', requested_at=now)
    try:
        assert live.store.lp_log_business_record(audit)
        def documents():
            with sqlite3.connect(live.store.path) as connection:
                return tuple(connection.execute(sql).fetchall() for sql in (
                    'SELECT payload FROM lp_auto_pool',
                    "SELECT payload FROM lp_sessions WHERE session_id='old-wallet-audit'",
                    "SELECT payload FROM lp_actions WHERE action_key='old-wallet-request'"))
        before = documents()
        control = live.store.lp_load_current_control(account)
        settings = live.store.lp_load_current_settings(account)
        healthy = live.run_once()
        assert healthy.acceptance.status == 'accepted'
        assert live.state.snapshot().new_capacity.value == Decimal('39')
        assert live.state.snapshot().current.account_id == MIXED_WALLET
        assert live.rounds.snapshot().account_id == MIXED_WALLET
        assert live.rounds.snapshot().round_number == 1
        assert live.state.snapshot().orders == ()  # An audit cannot restore a BUY.
        assert live.store.lp_load_current_control(account) == control
        assert live.store.lp_load_current_settings(account) == settings
        assert settings['account_id'] == account
        assert settings['budget_usd'] == '40'
        assert documents() == before
        assert not live.store.lp_log_business_record(replace(audit,
            request_id='wrong-parent-case', account_id=WALLET))
        assert documents() == before
        candidates, rows = candidates_for_fixed_send()
        for row in rows:
            row['account_id'] = WALLET
        if unknown == 'balance':
            def unavailable(**kw): raise OSError('offline balance unavailable')
            sdk.get_balance_allowance = unavailable
        elif unknown == 'inventory':
            sdk.positions = (dict(user=WALLET, condition_id=CONDITION_ID, token_id=TOKEN_ID, size='2'),)
        else:
            sdk.positions = (dict(user='0x'+'b'*40, condition_id=CONDITION_ID,
                token_id=TOKEN_ID, size='2', average_price='.50'),)
        rejected = live.run_once(candidates)
        # Complete position provenance can still carry an unknown cost.
        assert rejected.acceptance.status == ('accepted' if unknown == 'inventory' else 'partial')
        assert live.state.snapshot().new_capacity.state == 'unknown'
        assert live.state.snapshot().new_capacity.value is None
        assert sdk.posts == [] and sdk.cancels == []
        assert live.rounds.snapshot().round_number == 1
        assert live.store.lp_load_current_control(account) == control
        assert live.store.lp_load_current_settings(account) == settings
        assert documents() == before
        with pytest.raises(ValueError, match='lp_control_identity_or_value_invalid'):
            live.store.lp_load_current_control(MIXED_WALLET)
        with pytest.raises(ValueError, match='lp_settings_account_identity'):
            live.store.lp_load_current_settings(MIXED_WALLET)
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def wallet_case_candidates():
    from tests.test_lp_d2_integration import candidates_for_fixed_send
    captured, rows = candidates_for_fixed_send()
    return replace(captured, value=[dict(rows[0], account_id=WALLET)])


def test_case_variant_candidate_builds_nonempty_round_one_plan(tmp_path, monkeypatch):
    live, producer, sdk, now = wallet_case_runtime(tmp_path, monkeypatch)
    try:
        live.run_once()
        assert live.rounds.snapshot().round_number == 1
        captured = wallet_case_candidates()
        result = live.run_once(captured)
        assert result.acceptance.status == 'accepted'
        assert result.start.status.value == 'batch'
        assert len(result.start.token.actions) == 1
        action = result.start.token.actions[0]
        assert action.account_id == MIXED_WALLET
        assert action.target.token_id == 'token-F'
        assert action.target.terms.price == Decimal('.40')
        assert action.target.terms.quantity == Decimal('20')
        assert len(sdk.posts) == 1
        assert result.outcomes[0].status.value == 'accepted'
        assert live.rounds.snapshot().round_number == 2
        snapshot = live.state.snapshot()
        assert snapshot.orders[0].key == (MIXED_WALLET, 'condition-F', 'token-F', 'wallet-case-buy')
        assert snapshot.positions[0].account_id == MIXED_WALLET
        assert snapshot.observation.account_id == MIXED_WALLET
        assert captured.value[0]['account_id'] == WALLET
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('receipt_account', ['absent', 'lowercase', 'different'])
def test_wallet_case_fix_keeps_buy_receipt_identity_strict(tmp_path, monkeypatch, receipt_account):
    live, producer, sdk, now = wallet_case_runtime(tmp_path, monkeypatch)
    def post(signed):
        sdk.posts.append(signed)
        response = dict(order_id='wallet-case-receipt', status='LIVE', accepted=True, taking_amount='0')
        if receipt_account != 'absent':
            response['account_id'] = WALLET if receipt_account == 'lowercase' else '0x'+'b'*40
        return response
    sdk.post_order = post
    try:
        live.run_once()
        result = live.run_once(wallet_case_candidates())
        assert len(result.outcomes) == 1 and len(sdk.posts) == 1
        assert result.outcomes[0].identity.account_id == MIXED_WALLET
        snapshot = live.state.snapshot()
        if receipt_account == 'absent':
            assert result.outcomes[0].status.value == 'accepted'
            assert snapshot.orders[0].key == (MIXED_WALLET, 'condition-F', 'token-F', 'wallet-case-receipt')
            assert live.status()['last_buy_uncertainty'] is None
        else:
            assert result.outcomes[0].status.value == 'unknown_error'
            assert snapshot.orders == ()
            assert all(f.state == 'unknown' and f.value is None for f in (
                snapshot.buy_reserved, snapshot.inventory_cost, snapshot.current_exposure, snapshot.new_capacity))
            uncertainty = live.status()['last_buy_uncertainty']
            assert uncertainty.reason == 'invalid_receipt'
            assert uncertainty.account_id == MIXED_WALLET
            assert uncertainty.request_id == result.outcomes[0].identity.request_id
        assert live.complete_local_attempts(result.start.token, result.outcomes).status.value == 'duplicate'
        assert len(sdk.posts) == 1
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_fixed_plan_still_rejects_wrong_wallet_candidate(tmp_path, monkeypatch):
    live, producer, sdk, now = wallet_case_runtime(tmp_path, monkeypatch)
    try:
        live.run_once()
        captured = wallet_case_candidates()
        captured.value[0]['account_id'] = '0x'+'b'*40
        result = live.run_once(captured)
        assert result.acceptance.status == 'accepted'
        assert result.start.status.value == 'needs_fixed_plan'
        assert live.rounds.snapshot().round_number == 1
        assert sdk.posts == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def bridge_runtime(tmp_path, monkeypatch, *, target_count=1, candidate_count=1):
    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    class ClockMeta(type):
        def __instancecheck__(cls, value): return isinstance(value, datetime)
    class FixedDateTime(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None): return now if tz is not None else now.replace(tzinfo=None)
    monkeypatch.setattr(polymarket_lp_market_reader, 'datetime', FixedDateTime)
    monkeypatch.setattr(polymarket_trading, 'datetime', FixedDateTime)
    public = _SDKPublicClient(now)
    adapter, sdk = _adapter(tmp_path, public_client=public)
    sdk.orders = (); sdk.positions = ()
    sdk.create_limit_order = lambda **kw: dict(kw, order_type='GTD')
    def post(signed):
        sdk.posts.append(signed)
        return AcceptedOrder(order_id='bridge-buy', status='live', making_amount=Decimal('0'),
            taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
    sdk.post_order = post
    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    producer = PolymarketLPService(store, adapter, clock=lambda: now)
    direction = _direction(minimum_order_size='20')
    direction['market'].update(condition_id=CONDITION_ID, token_id=TOKEN_ID, market_id='bridge-market',
        metadata_checked_at=now-timedelta(seconds=2), fees_checked_at=now-timedelta(seconds=3))
    direction['book'].update(condition_id=CONDITION_ID, token_id=TOKEN_ID,
        received_at=now-timedelta(seconds=1), timestamp=now-timedelta(seconds=1),
        bids=[dict(price=Decimal('.40'), size=Decimal('1000')), dict(price=Decimal('.39'), size=Decimal('1000'))],
        asks=[dict(price=Decimal('.42'), size=Decimal('1000'))])
    direction['reward_checked_at'] = now-timedelta(seconds=4)
    direction['history_summary'] = dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, state='known',
        checked_at=now-timedelta(seconds=5), amplitude=Decimal('.01'))
    assert producer._candidate_pool_record_success(CONDITION_ID,
        dict(condition_id=CONDITION_ID, state='eligible', selected_direction=dict(token_id=TOKEN_ID)),
        judged_at=now, facts=dict(checked_at=now-timedelta(seconds=4), directions=[direction],
            account=dict(balance='0', allowance='0', authenticated=False), reservations=[dict(amount='999999')]),
        global_recovery_generation=7)
    for digit, reward in list(zip(('3', '4', '5'), ('18', '12', '6')))[:candidate_count-1]:
        condition, token = '0x'+digit*64, '0x'+digit*64
        extra = {**direction, 'daily_pool_usd': Decimal(reward),
            'market': {**direction['market'], 'condition_id': condition, 'token_id': token, 'market_id': 'bridge-'+digit},
            'book': {**direction['book'], 'condition_id': condition, 'token_id': token},
            'history_summary': {**direction['history_summary'], 'condition_id': condition, 'token_id': token}}
        assert producer._candidate_pool_record_success(condition, dict(condition_id=condition, state='eligible',
            selected_direction=dict(token_id=token)), judged_at=now, facts=dict(checked_at=now-timedelta(seconds=4),
                directions=[extra]), global_recovery_generation=7)
    account = hashlib.sha256(WALLET.strip().casefold().encode()).hexdigest()
    store.lp_save_current_settings(account, expected_config_version=0,
        changes=dict(budget_usd='40', target_buy_count=target_count, buy_price_level=1))
    seed_running(store)
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='execution-bridge', policy=CalculationPolicy(None), utc_now=lambda: now)
    live.load_settings(); live.initialize(ValidatedStartAdmission('execution-bridge', 'offline-permit'))
    return live, producer, sdk, public, now


def attach(live, producer, tmp_path):
    execution = PredictionExecutionService(store=live.store, monitor=object(), trading=live.trading,
        notifier=_Notifier(), lock_path=tmp_path/'execution.lock', lp=producer, lp_d2=live)
    execution._breaker_open = False  # existing execution startup-clear fixture boundary
    return execution


def test_d2_execution_scheduler_uses_current_facts_without_legacy_registration(tmp_path, monkeypatch):
    live, producer, sdk, public, now = bridge_runtime(tmp_path, monkeypatch)
    release, entered = Event(), Event()
    worker = None
    try:
        execution = attach(live, producer, tmp_path)
        assert not hasattr(execution, '_auto_pool')
        assert producer._facts_publisher is None and producer._facts_validator is None
        def prohibited(*args, **kwargs): pytest.fail('legacy registration or extra candidate API authority')
        for name in ('lp_active_sessions', 'lp_sessions', 'lp_register_account_snapshot'):
            if hasattr(live.store, name): monkeypatch.setattr(live.store, name, prohibited)
        public.get_market = public.get_order_book = prohibited
        real_connect = sqlite3.connect
        class BrokenBusinessLog(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if threading.current_thread().name == 'lp-business-writer' and 'lp_sessions' in sql:
                    raise sqlite3.OperationalError('ordinary business log unavailable')
                return super().execute(sql, *args, **kwargs)
        monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: real_connect(*a, **(kw|dict(factory=BrokenBusinessLog))))
        scheduler = LPAutoScheduler(execution, clock=lambda: now)
        execution.set_lp_auto_scheduler(scheduler)
        assert execution.lp_auto_run_once()['state'] == 'scheduled'
        assert sdk.balance_reads == 0 and sdk.posts == []
        assert scheduler.run_due()
        assert live.rounds.snapshot().round_number == 1 and sdk.posts == []
        balance = sdk.get_balance_allowance
        def held_balance(**kw):
            entered.set(); assert release.wait(5), 'SDK reader watchdog'
            return balance(**kw)
        sdk.get_balance_allowance = held_balance
        worker = threading.Thread(target=scheduler.run_due)
        worker.start(); assert entered.wait(2)
        assert scheduler.snapshot()['check_in_progress'] is True
        assert execution.lp_auto_run_once()['state'] == 'scheduled'
        assert execution.lp_auto_reconcile_unknown()['state'] == 'scheduled'
        assert scheduler.run_due() is False and sdk.posts == []
        release.set(); worker.join(3); assert not worker.is_alive()
        assert scheduler.snapshot()['last_check_error'] is None
        assert scheduler.snapshot()['next_check_at'] == now.isoformat()  # wake during read retained
        assert sdk.posts == [dict(token_id=TOKEN_ID, price=Decimal('.40'), size=Decimal('20'),
            side='BUY', post_only=True, expiration=1792195260, order_type='GTD')]
        snapshot = live.state.snapshot(); order, = snapshot.orders
        assert (order.key, order.original_size.value, order.price.value, order.order_type.value,
            order.expiration.value) == ((WALLET, CONDITION_ID, TOKEN_ID, 'bridge-buy'), Decimal('20'),
                Decimal('.40'), 'GTD', 1792195260)
        assert snapshot.observation.source_frame.account == WALLET
        assert snapshot.observation.calculation.policy_version == 1
        assert sdk.balance_reads == sdk.order_reads == sdk.position_reads == 2
        source = producer.capture_qualified_pool().rows[CONDITION_ID]
        baseline = next(b for b in snapshot.baselines.values() if b.key.kind == 'queue_bucket')
        assert baseline.source.source_id == source.source_id and baseline.source.checked_at == source.checked_at
        assert execution.lp_tick()['state'] == 'scheduled' and len(sdk.posts) == 1
        assert live.rounds.snapshot().round_number == 2
        assert json.loads(json.dumps(execution.lp_auto_state(include_intents=False)))['budget_usd'] == '40'
    finally:
        release.set()
        if worker: worker.join(3)
        assert live.close(timeout=3)
        live.trading.close()


def test_d2_execution_control_projection_keeps_requested_effective_saved_distinct(tmp_path, monkeypatch):
    live, producer, sdk, public, now = bridge_runtime(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    settings_entered, settings_release = Event(), Event()
    real_connect = sqlite3.connect
    class HeldControl(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'COMMIT' and threading.current_thread().name == 'lp-control-save':
                entered.set(); assert release.wait(5), 'real control commit watchdog'
            if sql == 'COMMIT' and threading.current_thread().name == 'lp-settings-save':
                settings_entered.set(); assert settings_release.wait(5), 'real settings commit watchdog'
            return super().execute(sql, *args, **kwargs)
    try:
        execution = attach(live, producer, tmp_path)
        scheduler = LPAutoScheduler(execution, clock=lambda: now)
        execution.set_lp_auto_scheduler(scheduler)
        before = execution.lp_auto_state()
        assert before['desired_running'] is True and before['effective_running'] is True
        assert before['pause_confirmed'] is False and before['control_save_state'] == 'saved'
        assert before['requested_control_version'] == before['saved_control_version'] == 0
        assert before['config_version'] == before['trading_config_version'] == 1
        assert (before['api_retry_interval_seconds'], before['order_check_interval_seconds'], before['round_interval_seconds']) == (60, 10, 30)
        assert before['effective_round_interval_seconds'] is before['check_interval_seconds'] is None
        assert before['current_exposure_usd'] is None and before['new_capacity_usd'] is None
        cycle = live.rounds.snapshot().cycle_generation
        monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: real_connect(*a, **(kw|dict(factory=HeldControl))))
        paused = execution.lp_auto_set_desired_running(False, audit=dict(actor='offline-user'))
        assert entered.wait(2)
        assert paused['desired_running'] is False and paused['effective_running'] is False
        assert paused['pause_confirmed'] is False and paused['control_request_status'] == 'accepted'
        assert paused['requested_control_version'] == 1 and paused['saved_control_version'] == 0
        pending = execution.lp_auto_state(include_intents=False)
        assert pending['control_native_inflight'] and pending['control_save_state'] in ('pending', 'saving')
        assert pending['fatal'] is None and 'intents' not in pending and 'account_buys' not in pending
        assert live.rounds.snapshot().cycle_generation == cycle
        rejected = execution.lp_auto_set_desired_running(True)
        assert rejected['control_request_status'] == 'rejected' and rejected['control_request_reason'] == 'control_save_pending'
        assert rejected['desired_running'] is False and rejected['effective_running'] is False
        with monkeypatch.context() as no_io:
            def forbidden(*a, **kw): pytest.fail('memory projection performed I/O')
            no_io.setattr(sqlite3, 'connect', forbidden)
            for name in ('get_balance_allowance', 'list_open_orders', 'list_positions'):
                no_io.setattr(sdk, name, forbidden)
            assert json.loads(json.dumps(execution.lp_auto_state(include_intents=False))) == rejected
        release.set(); settle_control(live)
        saved = execution.lp_auto_state(include_intents=False)
        assert saved['pause_confirmed'] is True and saved['control_native_inflight'] is False
        assert saved['requested_control_version'] == saved['saved_control_version'] == 1
        resume = execution.lp_auto_set_desired_running(True)
        assert resume['control_request_status'] == 'accepted' and resume['desired_running'] is True
        settle_control(live)
        resumed = execution.lp_auto_state(include_intents=False)
        assert resumed['effective_running'] is True and resumed['pause_confirmed'] is False
        assert resumed['requested_control_version'] == resumed['saved_control_version'] == 2
        generation = live.rounds.snapshot().cycle_generation
        assert generation != cycle and live.rounds.snapshot().round_number == 0
        assert execution.lp_auto_set_desired_running(True)['control_request_status'] == 'noop'
        assert execution.lp_auto_run_once()['state'] == 'scheduled'
        assert live.rounds.snapshot().cycle_generation == generation
        # Timing delegates to the real 05a native CAS, without changing RUN/generation.
        configured = execution.lp_auto_configure(dict(api_retry_interval_seconds=62, expected_config_version=1))
        assert settings_entered.wait(2) and configured['settings_pending']
        assert configured['effective_settings']['api_retry_interval_seconds'] == 60
        assert configured['pending_settings']['api_retry_interval_seconds'] == 62
        assert configured['pending_settings']['config_version'] == 2
        settings_release.set()
        deadline = time.monotonic()+3
        while execution.lp_auto_state()['settings_pending'] and time.monotonic() < deadline: time.sleep(.001)
        updated = execution.lp_auto_state(include_intents=False)
        assert (updated['api_retry_interval_seconds'], updated['config_version'], updated['trading_config_version']) == (62, 2, 1)
        assert updated['effective_running'] is True and live.rounds.snapshot().cycle_generation == generation
        assert updated['pending_settings'] is None and updated['effective_settings']['budget_usd'] == '40'
        assert live.store.lp_load_current_settings(hashlib.sha256(WALLET.encode()).hexdigest())['api_retry_interval_seconds'] == 62
        assert sdk.posts == []
    finally:
        release.set(); settings_release.set()
        assert live.close(timeout=3)
        live.trading.close()


def test_d2_execution_preserves_existing_mutation_fences_without_blocking_facts(tmp_path, monkeypatch):
    import fcntl
    # Each existing fence is exercised against the same public read/send path.
    for fence in ('breaker', 'incident', 'active_execution', 'foreign_file_lock'):
        path = tmp_path/fence
        live, producer, sdk, public, now = bridge_runtime(path, monkeypatch)
        foreign = None
        try:
            execution = attach(live, producer, path)
            execution.lp_auto_scheduled_check()  # real empty round0
            execution_id = incident_id = None
            if fence == 'breaker': execution._breaker_open = True
            if fence in ('incident', 'active_execution'):
                row = live.store.create_recovery_execution(dict(reason='offline-existing-fence'), idempotency_key=fence)
                execution_id = row['execution_id']
                if fence == 'incident':
                    incident_id = live.store.open_incident(execution_id, dict(reason='offline-existing-fence'))
                else:
                    live.store.transition_execution(execution_id, state='submitting', evidence={})
            if fence == 'foreign_file_lock':
                path.mkdir(parents=True, exist_ok=True)
                foreign = (path/'execution.lock').open('a+')
                fcntl.flock(foreign.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
            before = live.state.snapshot().snapshot_version
            denied = execution.lp_auto_scheduled_check()
            assert denied.acceptance.observation is not None and live.state.snapshot().snapshot_version > before
            assert [o.status.value for o in denied.outcomes] == ['not_sent']
            assert sdk.posts == [] and live.state.snapshot().orders == ()
            assert live.status()['last_buy_uncertainty'] is None
            assert live.control.status().fatal is None and live.control.status().requested_version == 0
            assert live.state.snapshot().new_capacity.value == Decimal('40')
            if fence == 'breaker': execution._breaker_open = False
            if incident_id: live.store.acknowledge_incident(incident_id, dict(note='offline-cleared'))
            if fence == 'active_execution': live.store.transition_execution(execution_id, state='complete', evidence={})
            if foreign:
                fcntl.flock(foreign.fileno(), fcntl.LOCK_UN); foreign.close(); foreign = None
            allowed = execution.lp_auto_scheduled_check()
            assert [o.status.value for o in allowed.outcomes] == ['accepted'] and len(sdk.posts) == 1
            assert allowed.outcomes[0].identity.request_id != denied.outcomes[0].identity.request_id
            assert allowed.outcomes[0].identity.target.terms == denied.outcomes[0].identity.target.terms
        finally:
            if foreign: foreign.close()
            assert live.close(timeout=3)
            live.trading.close()
    # Sealed C/D/E remains fixed when permission changes inside C's actual SDK post.
    live, producer, sdk, public, now = bridge_runtime(tmp_path/'sealed', monkeypatch, target_count=3, candidate_count=4)
    try:
        execution = attach(live, producer, tmp_path/'sealed')
        execution.lp_auto_scheduled_check()
        real_post = sdk.post_order
        def revoke_after_c(signed):
            response = real_post(signed); execution._breaker_open = True
            return response
        sdk.post_order = revoke_after_c
        real_connect = sqlite3.connect
        class OutsideStateLock(sqlite3.Connection):
            def execute(self, sql, *a, **kw):
                assert not live.state._lock._is_owned(), 'mutation SQL under CurrentState lock'
                return super().execute(sql, *a, **kw)
        with monkeypatch.context() as context:
            context.setattr(sqlite3, 'connect', lambda *a, **kw: real_connect(*a, **(kw|dict(factory=OutsideStateLock))))
            result = execution.lp_auto_scheduled_check()
        assert [o.status.value for o in result.outcomes] == ['accepted', 'not_sent', 'not_sent']
        assert [o.identity.target.token_id for o in result.outcomes] == [TOKEN_ID, '0x'+'3'*64, '0x'+'4'*64]
        assert [p['token_id'] for p in sdk.posts] == [TOKEN_ID]  # no F, no substitution
        assert result.finish.status.value == 'completed' and live.status()['last_buy_uncertainty'] is None
        assert len({o.identity.request_id for o in result.outcomes}) == 3
        execution.lp_auto_run_once()  # wake never replays the sealed batch
        assert len(sdk.posts) == 1
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_d2_execution_native_lease_drains_all_actual_calls_before_release(tmp_path, monkeypatch):
    import fcntl
    from concurrent.futures import TimeoutError as NativeTimeout
    from tests.test_lp_order_registration_contract import _open_order
    from tests.test_lp_d2_integration import _d2_filled_opening, _d2_protection_runtime
    def file_available(path):
        with path.open('a+') as handle:
            try: fcntl.flock(handle.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: return False
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return True
    live, producer, sdk, public, now = bridge_runtime(tmp_path/'native', monkeypatch)
    releases = {oid: Event() for oid in ('native-A', 'native-B')}
    entered = {oid: Event() for oid in releases}
    handles = []
    try:
        execution = attach(live, producer, tmp_path/'native')
        execution.lp_auto_scheduled_check()
        sdk.orders = tuple(_open_order(oid, 'BUY', price='.40', original='20') for oid in releases)
        execution.lp_auto_scheduled_check()
        assert sdk.posts == []
        def cancel(*, order_ids):
            oid, = order_ids; sdk.cancels.append(order_ids); entered[oid].set()
            assert releases[oid].wait(5), 'real independent cancel watchdog'
            return dict(canceled=order_ids, not_canceled={})
        sdk.cancel_orders = cancel
        for oid in releases:
            handles.append(live.cancel_independent((WALLET, CONDITION_ID, TOKEN_ID, oid)))
            assert entered[oid].wait(2)
        assert live.status()['native_inflight'] == 2 and sdk.cancels == [('native-A',), ('native-B',)]
        assert file_available(tmp_path/'native'/'execution.lock') is False
        acquired = execution._process_lock.acquire(False)
        if acquired: execution._process_lock.release()
        assert acquired is False
        with pytest.raises(NativeTimeout): handles[0].result(timeout=.01)
        execution.lp_auto_set_desired_running(False); settle_control(live)
        assert execution.lp_auto_state()['pause_confirmed'] is True
        execution.lp_auto_set_desired_running(True); settle_control(live)
        assert live.status()['native_inflight'] == 2
        assert live.close(timeout=.01) is False
        assert file_available(tmp_path/'native'/'execution.lock') is False
        releases['native-A'].set(); handles[0].result(timeout=2); handles[0]._thread.join(2)
        assert handles[0].done() and not handles[1].done() and live.status()['native_inflight'] == 1
        assert file_available(tmp_path/'native'/'execution.lock') is False
        with pytest.raises(ValueError): live.cancel_independent((WALLET, CONDITION_ID, TOKEN_ID, 'native-A'))
        assert len(sdk.cancels) == 2 and execution.lp_auto_state()['control_native_inflight'] is False
        releases['native-B'].set(); handles[1].result(timeout=2); handles[1]._thread.join(2)
        assert live.close(timeout=3) and file_available(tmp_path/'native'/'execution.lock') is True
        acquired = execution._process_lock.acquire(False)
        assert acquired; execution._process_lock.release()
    finally:
        for event in releases.values(): event.set()
        for handle in handles: handle._thread.join(3)
        assert live.close(timeout=3)
        live.trading.close()
    # Real BUY fills and current bid loss authorize protection, never a synthetic latch.
    live, sdk, public, clock = _d2_filled_opening(tmp_path/'sell', monkeypatch, manual_sale=False)
    sell_release, sell_entered = Event(), Event()
    try:
        producer = PolymarketLPService(live.store, live.trading)
        execution = attach(live, producer, tmp_path/'sell')
        scope = (WALLET, CONDITION_ID, TOKEN_ID, 'protection-epoch')
        execution._breaker_open = True; public.bid = Decimal('.20'); clock[0] += 1
        execution.lp_auto_scheduled_check()  # actual own BUY fill receipt invalidates old observation
        execution.lp_auto_scheduled_check()
        snapshot = live.state.snapshot()
        assert snapshot.openings[scope].stop_loss_latched.value is True
        assert snapshot.own_fills[(*scope[:3], 'entry-A')].quantity.value == 100
        assert snapshot.sells[scope].protected is None and sdk.market_orders == []
        assert len(sdk.posts) == 1  # original real BUY only
        post = sdk.post_order
        def held_sell(signed):
            assert signed['order_type'] == 'FOK'
            sell_entered.set(); assert sell_release.wait(5), 'real protected SELL watchdog'
            return post(signed)
        sdk.post_order = held_sell
        execution._breaker_open = False; clock[0] += 1
        execution.lp_auto_scheduled_check(); assert sell_entered.wait(2)
        pending = live.state.snapshot().sells[scope].protected
        assert pending.native_state == 'running' and pending.submit_quantity.value == 100
        assert pending.submit_residual_quantity.value == 100 and pending.submit_sold_quantity.value == 0
        assert sdk.market_orders[-1]['shares'] == Decimal('100') and sdk.market_orders[-1]['min_price'] == Decimal('.20')
        assert file_available(tmp_path/'sell'/'execution.lock') is False
        sell_release.set()
        deadline = time.monotonic()+3
        while live.status()['native_inflight'] and time.monotonic() < deadline: time.sleep(.001)
        assert live.status()['native_inflight'] == 0 and file_available(tmp_path/'sell'/'execution.lock') is True
        assert live.state.snapshot().sells[scope].protected.state == 'accepted'
        assert len(sdk.posts) == 2 and len(sdk.market_orders) == 1
    finally:
        sell_release.set()
        assert live.close(timeout=3)
        live.trading.close()
    # A guard-denied accepted risk trigger retains its original never-started target.
    live, sdk, public, clock = _d2_protection_runtime(tmp_path/'deferred', monkeypatch)
    try:
        producer = PolymarketLPService(live.store, live.trading)
        execution = attach(live, producer, tmp_path/'deferred')
        execution._breaker_open = True; public.level = Decimal('4000'); clock[0] += 1  # own2000/front2000 = .50
        execution.lp_auto_scheduled_check()
        progress, = live.state.snapshot().progress.values()
        assert progress.state == 'triggered' and progress.cancel_targets == () and sdk.cancels == []
        execution._breaker_open = False; clock[0] += 1
        execution.lp_auto_scheduled_check()
        deadline = time.monotonic()+3
        while live.status()['native_inflight'] and time.monotonic() < deadline: time.sleep(.001)
        assert sdk.cancels == [('entry-A',)]
        progress, = live.state.snapshot().progress.values()
        assert len(progress.cancel_targets) == 1 and progress.cancel_targets[0].order_key[3] == 'entry-A'
        assert progress.cancel_targets[0].native_state == 'ended'
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('transition', ['BUYpause', 'BUYresume', 'BUYfatal', 'BUYclose',
    'independentBUYcancelpause', 'protectedSELLclose', 'SELLcancelclose'])
def test_d2_final_safety_sql_cannot_outlive_current_dispatch_permission(tmp_path, monkeypatch, transition):
    from tests.test_lp_d2_integration import _d2_filled_opening
    from tests.test_lp_order_registration_contract import _open_order
    entered, release, signed = Event(), Event(), Event()
    failures, completed, errors, phase_reads = [], [], [], []
    business_clock = [0.0]
    worker = handle = None
    if transition.startswith('BUY') or transition == 'independentBUYcancelpause':
        live, producer, sdk, public, now = bridge_runtime(tmp_path, monkeypatch)
        if transition == 'BUYfatal':
            assert live.close(timeout=3)
            live = LPD2LiveExecution(live.store, live.trading, account_id=WALLET, venue='polymarket',
                process_epoch='fatal-sql-window', policy=CalculationPolicy(None),
                clock=lambda: business_clock[0], utc_now=lambda: now)
            live.load_settings(); live.initialize(ValidatedStartAdmission('fatal-sql-window', 'offline-permit'))
        execution = attach(live, producer, tmp_path)
        execution.lp_auto_scheduled_check()
        if transition == 'independentBUYcancelpause':
            sdk.orders = (_open_order('cancel-window', 'BUY', price='.40', original='20'),)
            execution.lp_auto_scheduled_check()
        create = sdk.create_limit_order
        def observed_create(**kw):
            result = create(**kw); signed.set(); return result
        sdk.create_limit_order = observed_create
    else:
        live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
        producer = PolymarketLPService(live.store, live.trading)
        execution = attach(live, producer, tmp_path)
        execution.lp_auto_scheduled_check()  # actual own-fill receipt
        if transition == 'SELLcancelclose':
            execution.lp_auto_scheduled_check()  # real passive SELL response
            deadline = time.monotonic()+3
            while live.status()['native_inflight'] and time.monotonic() < deadline: time.sleep(.001)
            assert live.status()['native_inflight'] == 0 and len(sdk.posts) == 2
        public.bid = Decimal('.20'); clock[0] += 1
    before_posts, before_cancels = len(sdk.posts), len(sdk.cancels)
    generation = live.state.snapshot().current.cycle_generation
    real_connect = sqlite3.connect
    class LastSafetySQL(sqlite3.Connection):
        def execute(self, sql, *a, **kw):
            thread = threading.current_thread().name
            if sql == 'COMMIT' and thread == 'lp-control-save' and transition == 'BUYfatal':
                failures.append(sql); raise sqlite3.OperationalError('actual control save rejected')
            cursor = super().execute(sql, *a, **kw)
            safety = sql.startswith('SELECT * FROM executions WHERE state NOT IN')
            selected = (transition.startswith('BUY') and thread == 'e1-dispatch' and signed.is_set()
                or transition == 'independentBUYcancelpause' and thread == 'lp-d2-independent-cancel'
                or transition in ('protectedSELLclose', 'SELLcancelclose') and thread == 'lp-d2-protection-send')
            if safety and selected and not entered.is_set():
                assert not live.state._lock._is_owned(), 'safety SQL must not hold CurrentState lock'
                phase_reads.append((thread, sql))
                # FOK has no limit-sign phase: the second actual safety read is
                # its last pre-post check, selected by the real pending attempt.
                if transition == 'protectedSELLclose':
                    attempt, = (s.protected for s in live.state.snapshot().sells.values())
                    assert attempt.kind == 'protected_exit' and attempt.native_state == 'running'
                    if len(phase_reads) < 2: return cursor
                entered.set(); assert release.wait(8), 'final safety SQL watchdog'
            return cursor
    def run():
        try: completed.append(execution.lp_auto_scheduled_check())
        except BaseException as exc: errors.append(exc)
    try:
        monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: real_connect(*a, **(kw|dict(factory=LastSafetySQL))))
        if transition == 'independentBUYcancelpause':
            handle = live.cancel_independent((WALLET, CONDITION_ID, TOKEN_ID, 'cancel-window'))
        else:
            worker = threading.Thread(target=run, name='e1-dispatch'); worker.start()
        assert entered.wait(3), 'last actual safety SQL was not reached'
        assert len(sdk.posts) == before_posts and len(sdk.cancels) == before_cancels
        if transition in ('BUYpause', 'BUYresume', 'independentBUYcancelpause'):
            assert live.request_running(False).status == 'accepted'; settle_control(live)
            if transition == 'BUYresume':
                assert live.request_running(True).status == 'accepted'; settle_control(live)
                assert live.state.snapshot().current.cycle_generation != generation
        elif transition == 'BUYfatal':
            assert live.request_running(False).status == 'accepted'
            deadline = time.monotonic()+3
            while live.poll().save_state != 'retry_wait' and time.monotonic() < deadline: time.sleep(.001)
            assert live.control.status().save_state == 'retry_wait' and len(failures) == 1
            business_clock[0] = 1.0
            deadline = time.monotonic()+3
            while live.poll().fatal is None and time.monotonic() < deadline: time.sleep(.001)
            assert live.control.status().fatal.origin == 'save_retry_exhausted' and len(failures) == 2
        else:
            assert live.close(timeout=.01) is False
        release.set()
        if worker: worker.join(3); assert not worker.is_alive() and errors == []
        if handle:
            assert handle.result(timeout=3).status.value == 'not_sent'
            handle._thread.join(2); assert handle.done()
        deadline = time.monotonic()+3
        while live.status()['native_inflight'] and time.monotonic() < deadline: time.sleep(.001)
        assert live.status()['native_inflight'] == 0
        assert len(sdk.posts) == before_posts and len(sdk.cancels) == before_cancels
        assert live.status()['last_buy_uncertainty'] is None
        if transition.startswith('BUY'):
            result, = completed
            outcome, = result.outcomes
            assert outcome.status.value == 'not_sent'
            assert outcome.identity == result.start.token.actions[0]
            terms = outcome.identity.target.terms
            assert (terms.price, terms.quantity, terms.expiration, terms.send_options) == (Decimal('.40'), Decimal('20'), 1792195260, ())
        if transition == 'BUYfatal': assert live.control.status().fatal.origin == 'save_retry_exhausted'
        if transition == 'protectedSELLclose':
            attempt, = (s.protected for s in live.state.snapshot().sells.values())
            assert attempt.state == 'rejected' and attempt.native_state == 'ended' and attempt.order_key is None
        if transition == 'SELLcancelclose':
            attempt, = (s.passive for s in live.state.snapshot().sells.values())
            assert attempt.cancel_request_id is None
    finally:
        release.set()
        if worker: worker.join(3)
        if handle: handle._thread.join(3)
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('change', ['positive_anchor_fill', 'unknown_terminal_anchor',
    'changed_target_terms', 'new_observation_only'])
def test_d2_deferred_cancel_revalidates_original_trigger_after_admission_io(tmp_path, monkeypatch, change):
    from tests.test_lp_d2_integration import _d2_protection_runtime
    from tests.test_lp_order_registration_contract import _open_order
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch, submit=False)
    producer = PolymarketLPService(live.store, live.trading)
    execution = attach(live, producer, tmp_path)
    releases = {oid: Event() for oid in ('occupied-X', 'occupied-Y', 'entry-A')}
    entered = {oid: Event() for oid in releases}
    sql_entered, sql_release = Event(), Event()
    terminal, exact_reads, handles, outcomes, errors = {}, [], {}, [], []
    worker = None
    def cancel(*, order_ids):
        oid, = order_ids; sdk.cancels.append(order_ids)
        if oid in releases:
            entered[oid].set(); assert releases[oid].wait(8), 'actual retained anchor/capacity watchdog'
        original = next(o for o in sdk.orders if o.id == oid)
        terminal[oid] = original.model_copy(update={'status': 'CANCELED'})
        sdk.orders = tuple(o for o in sdk.orders if o.id != oid)
        return dict(canceled=order_ids, not_canceled={})  # genuine ID-only ACK never proves final0
    def exact(*, order_id):
        exact_reads.append(order_id)
        return terminal.get(order_id)
    sdk.cancel_orders, sdk.get_order = cancel, exact
    def tick():
        live.run_once()
        return live.run_protection_once()
    def finish(attempts):
        for handle in attempts:
            handle.result(timeout=3); handle._thread.join(2); assert handle.done()
    try:
        sdk.orders = (_open_order('entry-A', 'BUY', price='.30', original='2000'),)
        tick()
        baseline = next(b for b in live.state.snapshot().baselines.values() if b.key.episode_id == 'entry-A')
        assert baseline.anchor_order_ids == ('entry-A',) and live.state.snapshot().openings == {}
        sdk.orders += (_open_order('occupied-X', 'BUY', price='.29', original='20'),
            _open_order('occupied-Y', 'BUY', price='.28', original='20'))
        live.run_once()
        for oid in ('occupied-X', 'occupied-Y'):
            handles[oid] = live.cancel_independent((WALLET, CONDITION_ID, TOKEN_ID, oid))
            assert entered[oid].wait(2)
        sdk.orders += (_open_order('retained-B', 'BUY', price='.30', original='500'),)
        public.level, clock[0] = Decimal('5000'), 1
        assert tick() == ()
        progress = live.state.snapshot().progress[baseline.key]
        assert progress.state == 'triggered' and progress.ratio.value == Decimal('.50') and progress.cancel_targets == ()
        for oid in ('occupied-X', 'occupied-Y'):
            releases[oid].set(); handles[oid].result(timeout=3); handles[oid]._thread.join(2)
            assert handles[oid].done()
        clock[0] += 1; live.run_once()
        handles['entry-A'] = live.cancel_independent((WALLET, CONDITION_ID, TOKEN_ID, 'entry-A'))
        assert entered['entry-A'].wait(2)
        live.request_running(False); settle_control(live)
        clock[0] += 1; live.run_once()
        original = live.state.snapshot()
        assert original.observation.read_ticket.trade_version_at_start == original.trade_version
        assert not original.progress[baseline.key].cancel_targets and live.status()['native_inflight'] == 1
        real_connect = sqlite3.connect
        class WaitingAdmission(sqlite3.Connection):
            def execute(self, sql, *a, **kw):
                cursor = super().execute(sql, *a, **kw)
                if (threading.current_thread().name == 'e2-dispatch' and not sql_entered.is_set()
                        and sql.startswith('SELECT * FROM executions WHERE state NOT IN')):
                    assert not live.state._lock._is_owned()
                    sql_entered.set(); assert sql_release.wait(8), 'real B lease acquisition watchdog'
                return cursor
        monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: real_connect(*a, **(kw|dict(factory=WaitingAdmission))))
        def dispatch():
            try: outcomes.extend(live.run_protection_once())
            except BaseException as exc: errors.append(exc)
        worker = threading.Thread(target=dispatch, name='e2-dispatch'); worker.start()
        assert sql_entered.wait(3)
        assert ('retained-B',) not in sdk.cancels
        if change == 'positive_anchor_fill':
            sdk.orders = tuple(o.model_copy(update={'size_matched': Decimal('1')}) if o.id == 'entry-A' else o for o in sdk.orders)
            sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='1', average_price='.30'),)
        elif change == 'unknown_terminal_anchor':
            releases['entry-A'].set()
            assert handles['entry-A'].result(timeout=3).status.value == 'accepted'
            anchor = next(o for o in live.state.snapshot().orders if o.key[3] == 'entry-A')
            assert anchor.terminal_proof.matched.state == 'unknown' and anchor.size_matched.value == 0
        elif change == 'changed_target_terms':
            sdk.orders = tuple(o.model_copy(update={'price': Decimal('.29'), 'original_size': Decimal('600')})
                if o.id == 'retained-B' else o for o in sdk.orders)
        clock[0] += 1
        accepted = live.run_once().acceptance.observation
        assert accepted.observation_id != original.observation.observation_id
        assert live.state.snapshot().current.effective_running is False
        if change == 'positive_anchor_fill':
            assert next(o for o in live.state.snapshot().orders if o.key[3] == 'entry-A').size_matched.value == 1
        if change == 'changed_target_terms':
            changed = next(o for o in live.state.snapshot().orders if o.key[3] == 'retained-B')
            assert changed.price.value == Decimal('.29') and changed.original_size.value == Decimal('600')
        sql_release.set(); worker.join(3); assert not worker.is_alive() and errors == []
        finish(outcomes)
        assert ('retained-B',) not in sdk.cancels, 'old original trigger cannot be re-stamped after admission I/O'
        snapshot = live.state.snapshot()
        assert not any(t.order_key[3] == 'retained-B' for p in snapshot.progress.values() for t in p.cancel_targets)
        assert snapshot.openings == {} and snapshot.own_fills == {} and sdk.posts == []
        if change == 'unknown_terminal_anchor':
            handles['entry-A']._thread.join(2); assert handles['entry-A'].done()
            # Exact authoritative final0 is a new Reader receipt, not the old ID ACK.
            terminal['entry-A'] = terminal['entry-A'].model_copy(update={'size_matched': Decimal('0')})
            clock[0] += 1
            assert tick() == ()
            assert next(o for o in live.state.snapshot().orders if o.key[3] == 'entry-A').terminal_proof.matched.value == 0
            assert ('retained-B',) not in sdk.cancels
        clock[0] += 1
        later = tick(); finish(later)
        expected = 1 if change in ('unknown_terminal_anchor', 'new_observation_only') else 0
        assert sdk.cancels.count(('retained-B',)) == expected
        clock[0] += 1
        finish(tick())
        assert sdk.cancels.count(('retained-B',)) == expected  # attempted/ID-only UNKNOWN never replays
        if expected:
            target, = [t for p in live.state.snapshot().progress.values() for t in p.cancel_targets if t.order_key[3] == 'retained-B']
            assert target.requested_remaining.value == Decimal('500') and target.native_state == 'ended'
        assert sdk.posts == [] and live.state.snapshot().own_fills == {}
    finally:
        sql_release.set()
        for event in releases.values(): event.set()
        if worker: worker.join(3)
        for handle in handles.values(): handle._thread.join(3)
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('change', ['passive_position_decreased', 'protected_position_decreased', 'protected_manual_sell'])
def test_d2_sell_dispatch_revalidates_original_financial_origin_after_safety_sql(tmp_path, monkeypatch, change):
    import fcntl
    from tests.test_lp_d2_integration import _d2_filled_opening
    from tests.test_lp_order_registration_contract import _open_order
    live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
    producer = PolymarketLPService(live.store, live.trading)
    execution = attach(live, producer, tmp_path)
    entered, release, signed = Event(), Event(), Event()
    scope = (WALLET, CONDITION_ID, TOKEN_ID, 'protection-epoch')
    phase_reads, errors, results = [], [], []
    worker = None
    try:
        execution.lp_auto_scheduled_check()  # real own BUY100 receipt
        if change.startswith('protected'): public.bid = Decimal('.20')
        before_posts = len(sdk.posts)
        assert before_posts == 1 and sdk.market_orders == []
        create = sdk.create_limit_order
        def create_phase(**kw):
            result = create(**kw)
            if kw['side'] == 'SELL': signed.set()
            return result
        sdk.create_limit_order = create_phase
        real_connect = sqlite3.connect
        class FinalSellSQL(sqlite3.Connection):
            def execute(self, sql, *a, **kw):
                cursor = super().execute(sql, *a, **kw)
                if (threading.current_thread().name == 'lp-d2-protection-send' and not entered.is_set()
                        and sql.startswith('SELECT * FROM executions WHERE state NOT IN')):
                    assert not live.state._lock._is_owned()
                    attempt = live.state.snapshot().sells[scope].passive if change.startswith('passive') else live.state.snapshot().sells[scope].protected
                    assert attempt.submit_quantity.value == attempt.submit_residual_quantity.value == 100
                    assert attempt.submit_sold_quantity.value == 0 and attempt.native_state == 'running'
                    phase_reads.append(sql)
                    if change.startswith('passive') and not signed.is_set(): return cursor
                    if change.startswith('protected') and len(phase_reads) < 2: return cursor
                    entered.set(); assert release.wait(8), 'last actual financial SELL safety SQL watchdog'
                return cursor
        monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: real_connect(*a, **(kw|dict(factory=FinalSellSQL))))
        control = live.state.snapshot().current
        def dispatch():
            try: results.append(execution.lp_auto_scheduled_check())
            except BaseException as exc: errors.append(exc)
        worker = threading.Thread(target=dispatch, name='sell-refinement-dispatch'); worker.start()
        assert entered.wait(3)
        original = live.state.snapshot()
        pending = original.sells[scope].passive if change.startswith('passive') else original.sells[scope].protected
        assert pending.state == 'pending' and pending.order_key is None
        assert len(sdk.posts) == before_posts and sdk.market_orders == []
        with (tmp_path/'execution.lock').open('a+') as foreign:
            with pytest.raises(BlockingIOError): fcntl.flock(foreign.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
        if change.endswith('position_decreased'):
            sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='60', average_price='.30'),)
        else:
            sdk.orders += (_open_order('actual-manual-sell40', 'SELL', price='.25', original='40').model_copy(
                update={'created_at': original.observation.accepted_at}),)
        clock[0] += 1
        accepted = live.run_once().acceptance.observation  # no new protection tick or rewritten submit facts
        changed = live.state.snapshot()
        assert accepted.observation_id != original.observation.observation_id and changed.current == control
        if change.endswith('position_decreased'):
            assert changed.positions[0].quantity.value == Decimal('60')
        else:
            manual, = [o for o in changed.active_orders if o.key[3] == 'actual-manual-sell40']
            assert manual.side == 'SELL' and manual.remaining.value == Decimal('40')
            assert changed.positions[0].quantity.value == Decimal('100')
        release.set(); worker.join(3); assert not worker.is_alive() and errors == []
        deadline = time.monotonic()+3
        while live.status()['native_inflight'] and time.monotonic() < deadline: time.sleep(.001)
        assert live.status()['native_inflight'] == 0
        assert len(sdk.posts) == before_posts and sdk.market_orders == [], 'old SELL100 must not post against later financial facts'
        final = live.state.snapshot()
        attempt = final.sells[scope].passive if change.startswith('passive') else final.sells[scope].protected
        assert attempt.request_id == pending.request_id and attempt.state == 'rejected' and attempt.native_state == 'ended'
        assert attempt.order_key is None and attempt.own_fill_evidence is None
        for field in ('submit_quantity', 'submit_sold_quantity', 'submit_residual_quantity', 'price', 'order_type', 'expiration'):
            assert getattr(attempt, field) == getattr(pending, field)
        assert final.positions == changed.positions and final.orders == changed.orders
        assert final.own_fills == changed.own_fills and final.current == control
        assert final.trade_version == changed.trade_version and live.status()['last_buy_uncertainty'] is None
        with (tmp_path/'execution.lock').open('a+') as foreign:
            fcntl.flock(foreign.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(foreign.fileno(), fcntl.LOCK_UN)
    finally:
        release.set()
        if worker: worker.join(3)
        assert live.close(timeout=3)
        live.trading.close()


def test_d2_same_tick_scoring_preserves_current_sell_cancel_admission(tmp_path, monkeypatch):
    import fcntl
    from tests.test_lp_d2_integration import _d2_filled_opening
    live, sdk, public, clock = _d2_filled_opening(tmp_path, monkeypatch, manual_sale=False)
    execution = attach(live, PolymarketLPService(live.store, live.trading), tmp_path)
    scope = (WALLET, CONDITION_ID, TOKEN_ID, 'protection-epoch')
    scoring_entered, scoring_release, sql_entered, sql_release = (Event() for _ in range(4))
    results, errors, scoring_calls = [], [], []
    worker = None
    try:
        execution.lp_auto_scheduled_check()  # real BUY100 own-fill receipt
        live.run_once()
        for handle in live.run_protection_once():
            assert handle.result(timeout=3).status == 'accepted'
            handle._thread.join(2)
            assert handle.done()
        live.run_once()  # real current passive-exit order after its native receipt
        original = live.state.snapshot()
        passive = original.sells[scope].passive
        assert passive.order_key[3] == 'passive-exit' and passive.submit_quantity.value == 100
        assert passive.native_state == 'ended' and sdk.cancels == [] and sdk.market_orders == []
        clock[0] += 5
        public.bid = Decimal('.20')
        live.run_once()
        original = live.state.snapshot()
        def scoring(*, order_id):
            scoring_calls.append(order_id)
            assert order_id == 'passive-exit'
            scoring_entered.set()
            assert scoring_release.wait(8), 'same-tick scoring completion watchdog'
            return True
        sdk.get_order_scoring = scoring
        real_connect = sqlite3.connect
        class CancelSafetySQL(sqlite3.Connection):
            def execute(self, sql, *a, **kw):
                cursor = super().execute(sql, *a, **kw)
                if (threading.current_thread().name == 'lp-d2-protection-send'
                        and sql.startswith('SELECT * FROM executions WHERE state NOT IN') and not sql_entered.is_set()):
                    assert not live.state._lock._is_owned()
                    sql_entered.set()
                    assert sql_release.wait(8), 'same-tick native cancel SQL watchdog'
                return cursor
        monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: real_connect(*a, **(kw|dict(factory=CancelSafetySQL))))
        def dispatch():
            try: results.extend(live.run_protection_once())
            except BaseException as exc: errors.append(exc)
        worker = threading.Thread(target=dispatch, name='same-tick-protection'); worker.start()
        assert scoring_entered.wait(3)
        scoring_release.set()
        assert sql_entered.wait(3)
        worker.join(3)
        assert not worker.is_alive() and errors == [] and len(results) == 1
        scored = live.state.snapshot()
        assert scoring_calls == ['passive-exit']
        assert scored.openings[scope].scoring.order_key == passive.order_key
        assert scored.openings[scope].scoring.role == 'passive_exit'
        assert scored.openings[scope].scoring.scoring.value is True
        assert scored.openings[scope].scoring.checked_at == original.observation.accepted_at
        assert scored.observation is original.observation and scored.trade_version == original.trade_version
        assert scored.openings[scope].stop_loss_latched.value is True
        assert sdk.cancels == [] and sdk.market_orders == []
        handle, = results
        assert not handle.done() and live.status()['native_inflight'] == 1
        with (tmp_path/'execution.lock').open('a+') as foreign:
            with pytest.raises(BlockingIOError): fcntl.flock(foreign.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
        sql_release.set()
        outcome = handle.result(timeout=3)
        handle._thread.join(2)
        assert handle.done() and live.status()['native_inflight'] == 0
        assert outcome.status == 'accepted', 'same-tick scoring must not invalidate the original passive cancellation'
        assert outcome.order_id == 'passive-exit' and sdk.cancels == [('passive-exit',)]
        final = live.state.snapshot()
        order = next(o for o in final.orders if o.key == passive.order_key)
        assert order.terminal_proof.identity_verified and order.terminal_proof.matched.state == 'unknown'
        assert final.sells[scope].passive.request_id == passive.request_id
        assert final.sells[scope].passive.submit_quantity == passive.submit_quantity
        assert final.sells[scope].passive.submit_sold_quantity == passive.submit_sold_quantity
        assert sdk.market_orders == [] and len(sdk.posts) == 2
        with (tmp_path/'execution.lock').open('a+') as foreign:
            fcntl.flock(foreign.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(foreign.fileno(), fcntl.LOCK_UN)
    finally:
        scoring_release.set(); sql_release.set()
        if worker: worker.join(3)
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('change', ['partial_before_new_attempt', 'partial_during_admission_then_fresh_tick'])
def test_d2_deferred_target_partial_fill_uses_fresh_attempt_revision(tmp_path, monkeypatch, change):
    from tests.test_lp_d2_integration import _d2_protection_runtime
    from tests.test_lp_order_registration_contract import _open_order
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch, submit=False)
    execution = attach(live, PolymarketLPService(live.store, live.trading), tmp_path)
    releases = {oid: Event() for oid in ('occupied-X', 'occupied-Y', 'entry-A')}
    entered = {oid: Event() for oid in releases}
    sql_entered, sql_release = Event(), Event()
    terminal, exact_reads, handles, old_attempts, errors = {}, [], {}, [], []
    worker = None
    def cancel(*, order_ids):
        oid, = order_ids
        sdk.cancels.append(order_ids)
        if oid in releases:
            entered[oid].set()
            assert releases[oid].wait(8), 'real deferred-target capacity completion watchdog'
        original = next(o for o in sdk.orders if o.id == oid)
        terminal[oid] = original.model_copy(update={'status': 'CANCELED'})
        sdk.orders = tuple(o for o in sdk.orders if o.id != oid)
        return dict(canceled=order_ids, not_canceled={})
    def exact(*, order_id):
        exact_reads.append(order_id)
        return terminal.get(order_id)
    sdk.cancel_orders, sdk.get_order = cancel, exact
    def tick():
        clock[0] += 1
        live.run_once()
        return live.run_protection_once()
    def finish(attempts):
        for handle in attempts:
            outcome = handle.result(timeout=3)
            handle._thread.join(2)
            assert handle.done()
            assert outcome.status.value == 'accepted'
    def partial_fill():
        sdk.orders = tuple(o.model_copy(update={'size_matched': Decimal('1')})
            if o.id == 'original-B' else o for o in sdk.orders)
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID, size='1', average_price='.30'),)
    try:
        sdk.orders = (_open_order('entry-A', 'BUY', price='.30', original='2000'),)
        assert tick() == ()
        baseline = next(b for b in live.state.snapshot().baselines.values() if b.key.episode_id == 'entry-A')
        sdk.orders += (_open_order('occupied-X', 'BUY', price='.29', original='20'),
            _open_order('occupied-Y', 'BUY', price='.28', original='20'))
        live.run_once()
        for oid in ('occupied-X', 'occupied-Y'):
            handles[oid] = live.cancel_independent((WALLET, CONDITION_ID, TOKEN_ID, oid))
            assert entered[oid].wait(2)
        sdk.orders += (_open_order('original-B', 'BUY', price='.30', original='10'),)
        public.level = Decimal('4000')
        assert tick() == ()  # both slots occupied; original A/B tickets accepted
        original = next(o for o in live.state.snapshot().orders if o.key[3] == 'original-B')
        assert original.original_size.value == original.remaining.value == 10 and original.size_matched.value == 0
        assert live.state.snapshot().progress[baseline.key].cancel_targets == ()
        releases['occupied-X'].set(); finish((handles['occupied-X'],))
        handles['entry-A'], = tick()
        assert entered['entry-A'].wait(2)
        progress = live.state.snapshot().progress[baseline.key]
        assert {t.order_key[3] for t in progress.cancel_targets} == {'entry-A'}
        assert live.status()['native_inflight'] == 2 and ('original-B',) not in sdk.cancels
        # Original registered A makes the bucket ineligible for silent replanning.
        for oid in ('entry-A', 'occupied-Y'):
            releases[oid].set(); finish((handles[oid],))
        assert tick() == ()  # actual Reader obtains A's authoritative final0
        anchor = next(o for o in live.state.snapshot().orders if o.key[3] == 'entry-A')
        assert anchor.terminal_proof.identity_verified and anchor.terminal_proof.matched.value == 0
        assert exact_reads and set(exact_reads) == {'entry-A'}
        if change == 'partial_before_new_attempt':
            partial_fill()
        else:
            clock[0] += 1
            live.run_once()
            real_connect = sqlite3.connect
            class HeldFinalCancelSQL(sqlite3.Connection):
                def execute(self, sql, *a, **kw):
                    cursor = super().execute(sql, *a, **kw)
                    if (threading.current_thread().name == 'lp-d2-independent-cancel'
                            and sql.startswith('SELECT * FROM executions WHERE state NOT IN') and not sql_entered.is_set()):
                        assert not live.state._lock._is_owned()
                        sql_entered.set()
                        assert sql_release.wait(8), 'old B attempt final safety SQL watchdog'
                    return cursor
            monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: real_connect(*a, **(kw|dict(factory=HeldFinalCancelSQL))))
            def dispatch():
                try: old_attempts.extend(live.run_protection_once())
                except BaseException as exc: errors.append(exc)
            worker = threading.Thread(target=dispatch, name='deferred-target-dispatch'); worker.start()
            assert sql_entered.wait(3)
            worker.join(3)
            assert not worker.is_alive() and errors == [] and len(old_attempts) == 1
            assert ('original-B',) not in sdk.cancels
            partial_fill()
            clock[0] += 1
            live.run_once()  # genuinely newer ordinary frame, not a forged receipt
            sql_release.set()
            old, = old_attempts
            assert old.result(timeout=3).status.value == 'not_sent'
            old._thread.join(2)
            assert old.done() and ('original-B',) not in sdk.cancels
            assert not any(t.order_key[3] == 'original-B' for t in live.state.snapshot().progress[baseline.key].cancel_targets)
        clock[0] += 1
        live.run_once()
        partial = next(o for o in live.state.snapshot().orders if o.key == original.key)
        assert partial.terms_version > original.terms_version
        assert partial.size_matched.value == 1 and partial.remaining.value == 9
        assert all(getattr(partial, field).value == getattr(original, field).value
            for field in ('price', 'original_size', 'order_type', 'expiration'))
        fresh = live.run_protection_once()
        finish(fresh)
        assert sdk.cancels.count(('original-B',)) == 1, 'fresh original B remainder9 must not be pinned to trigger-time revision'
        final = live.state.snapshot()
        target, = [t for t in final.progress[baseline.key].cancel_targets if t.order_key == original.key]
        assert target.requested_remaining == partial.remaining and target.requested_remaining.value == 9
        assert target.native_state == 'ended'
        current = next(o for o in final.orders if o.key == original.key)
        assert current.size_matched == partial.size_matched  # lower-bound1 retains its real API source
        assert current.terminal_proof.matched.state == 'unknown' and current.terminal_proof.identity_verified
        assert current.original_size == partial.original_size and current.terminal_proof.original_size.value == 10
        finish(tick())
        assert sdk.cancels.count(('original-B',)) == 1  # attempted UNKNOWN is not another permission
        assert live.status()['native_inflight'] == 0 and sdk.posts == []
        assert live.state.snapshot().openings == {} and live.state.snapshot().own_fills == {}
    finally:
        sql_release.set()
        for event in releases.values(): event.set()
        if worker: worker.join(3)
        for handle in (*handles.values(), *old_attempts): handle._thread.join(3)
        assert live.close(timeout=3)
        live.trading.close()
