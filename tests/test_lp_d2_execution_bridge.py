"""Inactive Execution attachment at real scheduler, Store, SDK and native boundaries."""
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
