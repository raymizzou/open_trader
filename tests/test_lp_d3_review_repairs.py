"""Approved F-core review regressions at real Store/public service seams."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import pytest

from open_trader.lp_persistence import AdmissionCancelled, AdmissionTimeout, LPWriteCoordinator
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


@pytest.mark.parametrize('port', ['exclusion', 'ended', 'global_recovery', 'competition', 'account', 'first_seen'])
def test_publication_ports_keep_guards_free_while_metadata_business_control_queue(tmp_path, port):
    """F07/M4: all named publication phases share the real native owner."""
    from datetime import UTC, datetime
    from decimal import Decimal
    from types import SimpleNamespace
    from open_trader.lp_metadata_writer import LPMetadataWriter
    from open_trader.lp_business_writer import LPBusinessWriter
    from open_trader.polymarket_lp import PolymarketLPService
    from tests.test_lp_d3_persistence import request, wait_for
    from tests.test_polymarket_lp import _LPCandidateQueryExchange, _FirstSeenExchange, _FIRST_SEEN_TOKEN, _FIRST_SEEN_CONDITION

    stamp = datetime(2026, 10, 19, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path)
    store.initialize_lp_metadata_cache()
    store.lp_save_preparation(dict(generation=1, global_recovery_generation=1, state='paused', paused=True))
    service = PolymarketLPService(store, _LPCandidateQueryExchange(stamp, {}), clock=lambda: stamp, exclusions_enabled=True)
    guard = service._candidate_state_lock
    control_account = 'A'
    if port == 'exclusion':
        operation = lambda: service._exclude_candidate('C', 'T', 'history_amplitude_exceeded',
            checked_at=stamp, global_recovery_generation=1)
    elif port == 'ended':
        store.lp_record_market_exclusion('C', 'T', 'history_amplitude_exceeded',
            checked_at=stamp, cooldown_until=stamp.replace(year=2027), now=stamp)
        operation = lambda: service._observe_ended_candidate_market('C', {'closed': True},
            checked_at=stamp, global_recovery_generation=1)
    elif port == 'global_recovery':
        operation = lambda: service.recover_preparation(scope='global', expected_generation=1)
    elif port == 'competition':
        from tests.test_lp_competition_batches import NOW, CompetitionOpener, make_competition_adapter
        service = PolymarketLPService(store, make_competition_adapter(CompetitionOpener()), clock=lambda: NOW)
        operation, guard = service.refresh_competition_cache, service._competition_lock
    elif port == 'account':
        from tests.test_lp_account_coverage_ledger import NOW, WALLET, POOL, snapshot
        service = PolymarketLPService(store, SimpleNamespace(config=SimpleNamespace(wallet_address=WALLET)), clock=lambda: NOW)
        operation = lambda: service.register_account_snapshot(snapshot())
        guard, control_account = service._first_seen_apply_lock, POOL
    else:
        exchange = _FirstSeenExchange()
        exchange.set_book(_FIRST_SEEN_TOKEN, level_total='10000', now=stamp)
        service = PolymarketLPService(store, exchange, clock=lambda: stamp)
        operation = lambda: service.register_first_seen_candidates([dict(order_id='f07-first',
            token_id=_FIRST_SEEN_TOKEN, condition_id=_FIRST_SEEN_CONDITION,
            price=Decimal('.30'), remaining=Decimal('2000'), first_seen_at=stamp)], now=stamp)
        guard = service._mutex
    if port in {'exclusion', 'ended', 'global_recovery'}:
        service.exchange.config = SimpleNamespace(wallet_address='0x'+'1'*40)
    owner = LPWriteCoordinator(store.path)
    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    # Same approved D2 routing stand-in as F13, with no active execution.
    # The publication calls below remain real Store/LPService operations.
    d2 = SimpleNamespace(_closed=False, store=store, trading=service.exchange,
        account_id=service.exchange.config.wallet_address,
        policy=SimpleNamespace(buy_price_level=1),
        state=SimpleNamespace(snapshot=lambda: SimpleNamespace(current=SimpleNamespace(
            shutdown_requested=False, fatal_id=None))), bind_mutation_admission=lambda *args: None)
    execution = PredictionExecutionService(store=store, monitor=object(), trading=service.exchange,
        notifier=object(), lock_path=tmp_path/'f07-d2.lock', lp=service, lp_d2=d2)
    wakes = []
    execution.set_lp_auto_scheduler(SimpleNamespace(request_check=lambda: wakes.append('requested')))
    metadata, business = LPMetadataWriter(store), LPBusinessWriter(store)
    def read_guard():
        acquired = guard.acquire(timeout=2)
        if acquired:
            guard.release()
        return acquired
    control_write = dict(account_id=control_account, process_epoch='F2', request_version=1,
        request_id='F2-control', desired_running=False, requested_at=stamp, actor='offline')
    try:
        with owner.enforce_lock_order(), ThreadPoolExecutor(max_workers=3) as pool:
            assert store.active_execution() is None
            assert execution.reconcile_startup()['state'] == 'ready'
            with owner.acquire():
                target = pool.submit(operation)
                wait_for(owner.status, lambda s: s['waiting'] == 1 or target.done())
                assert not target.done(), target.result() if target.done() else None
                assert pool.submit(read_guard).result(timeout=3) is True
                metadata.enqueue({'F2-cache': (stamp.timestamp()+10**9, {'condition_id': 'F2-cache'})})
                business.offer(request('F2-request'))
                control = pool.submit(store.lp_save_current_control, control_write)
                wait_for(owner.status, lambda s: s['waiting'] == 4)
            result = target.result(timeout=8)
            assert result is True or result['state'] in {'ready', 'known', 'registered'}
            receipt = control.result(timeout=5)
            assert (receipt['request_id'], receipt['request_version'], receipt['desired_running']) == ('F2-control', 1, False)
            wait_for(metadata.status, lambda s: s['saved_entries'] == 1)
            wait_for(business.status, lambda s: s['saved_entries'] == 1)
            assert execution.lp_tick() == {'state': 'scheduled'}
            assert wakes == ['requested']
        assert owner.status()['lock_order_violations'] == 0
        assert metadata.status()['failed_entries'] == business.status()['failed_entries'] == 0
        if port == 'competition':
            assert store.lp_competitiveness_count() == 3
            assert store.lp_competitiveness_progress()['next_start_cursor'] is None
        if port == 'account':
            assert store.lp_trade_generation() == 0
    finally:
        metadata.stop(timeout=5)
        business.close(timeout=5)


def test_retry_after_requeue_requires_full_five_second_budget(tmp_path, monkeypatch):
    """R2-2: queueing after the first safe BUSY consumes the original budget."""
    store = PredictionArbitrageStore(tmp_path)
    owner = LPWriteCoordinator(store.path)
    connect = sqlite3.connect
    blocker = connect(store.path, isolation_level=None, check_same_thread=False)
    blocker.execute('BEGIN IMMEDIATE')
    entered = threading.Event()
    attempts = []
    started = time.monotonic()

    class Boundary(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'BEGIN IMMEDIATE' and threading.current_thread().name.startswith('budget-target'):
                attempts.append(time.monotonic() - started)
                entered.set()
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: connect(*a, **(k | dict(factory=Boundary))))

    def queued_control():
        with owner.acquire('control'):
            threading.Event().wait(6.0)

    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='budget-target') as targets, ThreadPoolExecutor(max_workers=1) as holders:
            target = targets.submit(store.update_lp_observation, 'A', 'C', {'marker': 'never-written'})
            assert entered.wait(3)
            holding = holders.submit(queued_control)
            until = time.monotonic() + 3
            while owner.status()['waiting'] != 1 and time.monotonic() < until:
                threading.Event().wait(.002)
            assert owner.status()['waiting'] == 1
            with pytest.raises(AdmissionTimeout) as failure:
                target.result(timeout=23)
            holding.result(timeout=3)
        assert failure.value.known_not_written is True
        assert len(attempts) == 1, attempts
        assert store.lp_observations('A') == {}
        assert owner.status()['waiting'] == 0 and not owner.status()['holder']
        status = owner.status()
        native = next(r for r in status['transactions'] if r['operation'].endswith('.update_lp_observation'))
        terminal = next(r for r in status['admissions'] if r['event'] == 'timeout')
        assert terminal['chain_id'] == native['chain_id']
        assert terminal['begin_attempts'] == 1
        retry_ticket = next(r for r in status['admissions']
            if r['event'] == 'slow_wait' and r['chain_id'] == terminal['chain_id'])
        assert terminal['retry_wait_seconds'] == pytest.approx(retry_ticket['wait_seconds'])
        assert terminal['retry_wait_seconds'] > 0
    finally:
        blocker.execute('ROLLBACK')
        blocker.close()


def test_first_begin_after_guard_wait_respects_inherited_deadline(tmp_path, monkeypatch):
    """R3-1: the first grant cannot authorize a BEGIN after its chain expires."""
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from open_trader import lp_persistence, prediction_arbitrage_store
    from open_trader.polymarket_lp import PolymarketLPService
    from tests.test_lp_d3_persistence import wait_for
    from tests.test_polymarket_lp import _LPCandidateQueryExchange

    stamp = datetime(2026, 10, 19, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path)
    original = dict(generation=1, global_recovery_generation=1, state='paused',
        paused=True, durable_marker='preserve')
    store.lp_save_preparation(original)
    service = PolymarketLPService(store, _LPCandidateQueryExchange(stamp, {}), clock=lambda: stamp)
    owner = LPWriteCoordinator(store.path)
    guard = service._candidate_state_lock
    held, release = threading.Event(), threading.Event()
    clock_lock = threading.Lock()
    now = 1000.0

    def clock():
        with clock_lock:
            return now

    # Override only the two business-deadline lookup sites. Event waits and
    # wait_for's independent monotonic watchdog retain the real system clock.
    monkeypatch.setattr(lp_persistence, 'time', SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(prediction_arbitrage_store, 'monotonic', clock)
    connect = sqlite3.connect
    connections, begins, writes = [], [], []

    class Boundary(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.observed = threading.current_thread().name.startswith('first-begin-target')
            self.closed = False
            if self.observed:
                connections.append(self)

        def execute(self, sql, *args, **kwargs):
            if self.observed:
                verb = sql.strip().split()[0].upper()
                if verb == 'BEGIN':
                    begins.append(sql)
                if verb in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                    writes.append(sql)
            return super().execute(sql, *args, **kwargs)

        def executemany(self, sql, *args, **kwargs):
            if self.observed:
                writes.append(sql)
            return super().executemany(sql, *args, **kwargs)

        def close(self):
            super().close()
            self.closed = True

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: connect(*a, **(k | dict(factory=Boundary))))

    def hold_guard():
        with guard:
            held.set()
            assert release.wait(5), 'independent guard-holder watchdog'

    with ThreadPoolExecutor(max_workers=1) as holders, ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='first-begin-target') as targets:
        holding = holders.submit(hold_guard)
        try:
            assert held.wait(5)
            with owner.enforce_lock_order(), owner.acquire('control'):
                target = targets.submit(service.recover_preparation, scope='global', expected_generation=1)
                wait_for(owner.status, lambda s: s['waiting'] == 1)
                with clock_lock:
                    now = 1014.5  # The first permit has only 0.5s left.
            wait_for(owner.status, lambda s: s['holder'])
            assert not target.done()  # Granted, but blocked on the independent guard.
            with clock_lock:
                now = 1016.0
            release.set()
            with pytest.raises(AdmissionTimeout) as failure:
                target.result(timeout=5)
            holding.result(timeout=5)
        finally:
            release.set()
            holding.result(timeout=5)

    assert failure.value.known_not_written is True
    assert begins == writes == []
    assert connections and all(connection.closed for connection in connections)
    assert guard.acquire(timeout=2)
    guard.release()
    status = owner.status()
    assert not status['holder'] and status['waiting'] == 0
    assert status['lock_order_violations'] == status['begin_retries'] == 0
    assert status['admission_timeouts'] == 1
    terminal = next(row for row in status['admissions'] if row['event'] == 'timeout')
    native = next(row for row in status['transactions']
        if row['operation'].endswith('.lp_recover_preparation_items'))
    assert native['failed'] is True and native['phase'] == 'begin'
    assert native['chain_id'] == terminal['chain_id']
    assert native['begin_attempts'] == terminal['begin_attempts'] == 0
    assert native['retry_wait_seconds'] == terminal['retry_wait_seconds'] == 0.0
    assert store.lp_preparation() == original
    # A new independent operation can reuse the released permit and connection.
    assert service.recover_preparation(scope='global', expected_generation=1)['generation'] == 2


def test_bounded_admission_and_retry_telemetry_is_visible_without_payload(tmp_path, caplog):
    """R2-4/M3: queue ages, chain identity, checkpoint deltas and outcomes."""
    store = PredictionArbitrageStore(tmp_path)
    owner = LPWriteCoordinator(store.path)
    from tests.test_lp_d3_persistence import wait_for
    with ThreadPoolExecutor(max_workers=1) as pool:
        with owner.acquire():
            pending = pool.submit(store.update_lp_observation, 'A', 'C', {'private': 'SECRET-F2'})
            queued = wait_for(owner.status, lambda row: row['waiting'] == 1)
            assert queued['oldest_ticket_age_seconds'] >= 0
            assert type(queued['oldest_ticket_enqueued_at']) is float
            threading.Event().wait(1.1)
        pending.result(timeout=5)
    transaction = owner.status()['transactions'][-1]
    assert type(transaction['thread']) is int
    assert type(transaction['chain_id']) is int
    assert transaction['begin_attempts'] == 1
    assert transaction['retry_wait_seconds'] == 0.0
    assert type(transaction['ticket_enqueued_at']) is float
    checkpoint = owner.status()['checkpoint']
    assert type(checkpoint['wal_bytes_before']) is int
    assert type(checkpoint['wal_bytes_after']) is int
    assert checkpoint['wal_bytes_delta'] == checkpoint['wal_bytes_after'] - checkpoint['wal_bytes_before']
    with pytest.raises(AdmissionTimeout):
        with owner.acquire(deadline=time.monotonic() - 1):
            pytest.fail('expired admission ran')
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(AdmissionCancelled):
        with owner.acquire(cancellation=cancel):
            pytest.fail('cancelled admission ran')
    messages = [r.getMessage() for r in caplog.records]
    for event in ('slow_wait', 'slow_hold', 'timeout', 'cancelled'):
        assert any('lp_write_admission' in m and f'event={event}' in m for m in messages), (event, messages)
    assert 'SECRET-F2' not in str(owner.status())
    assert all('SECRET-F2' not in message for message in messages)
    assert len(owner.status()['admissions']) <= 64


def test_first_seen_registration_reads_network_without_memory_lock_and_orders_admission(tmp_path):
    """M2: the dashboard's public registration cannot invert mutex/permit."""
    from datetime import UTC, datetime
    from decimal import Decimal
    from open_trader.polymarket_lp import PolymarketLPService
    from tests.test_polymarket_lp import _FirstSeenExchange, _FIRST_SEEN_TOKEN, _FIRST_SEEN_CONDITION

    stamp = datetime(2026, 10, 19, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path)
    network_locks = []

    class Exchange(_FirstSeenExchange):
        def lp_order_books(self, *args, **kwargs):
            network_locks.append(service._mutex._is_owned())
            return super().lp_order_books(*args, **kwargs)

    exchange = Exchange()
    exchange.set_book(_FIRST_SEEN_TOKEN, level_total='10000', now=stamp)
    service = PolymarketLPService(store, exchange, clock=lambda: stamp)
    row = dict(order_id='first-f2', token_id=_FIRST_SEEN_TOKEN, condition_id=_FIRST_SEEN_CONDITION,
               price=Decimal('.30'), remaining=Decimal('2000'), created_at=None, first_seen_at=stamp)
    result = service.register_first_seen_candidates([row], now=stamp)
    assert result['state'] == 'registered'
    assert result['baseline_front'] == Decimal('8000')
    assert network_locks == [False]
    assert LPWriteCoordinator(store.path).status()['lock_order_violations'] == 0
    again = service.register_first_seen_candidates([row], now=stamp)
    assert again == dict(state='skipped', reason='episode_exists')
    assert len(store.lp_active_first_seen_episodes()) == 1


def test_outer_public_recovery_requeues_safe_begin_after_releasing_memory_guard(tmp_path, monkeypatch):
    """R2-3: a real scoped service operation recovers from a finite holder."""
    from datetime import UTC, datetime
    from open_trader.polymarket_lp import PolymarketLPService
    from tests.test_polymarket_lp import _LPCandidateQueryExchange

    stamp = datetime(2026, 10, 19, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_preparation(dict(generation=1, state='paused', paused=True, durable_marker='preserve'))
    service = PolymarketLPService(store, _LPCandidateQueryExchange(stamp, {}), clock=lambda: stamp)
    owner = LPWriteCoordinator(store.path)
    connect = sqlite3.connect
    blocker = connect(store.path, isolation_level=None, check_same_thread=False)
    blocker.execute('BEGIN IMMEDIATE')
    attempts, held_guards = [], []

    class Boundary(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'BEGIN IMMEDIATE':
                attempts.append(threading.get_ident())
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: connect(*a, **(k | dict(factory=Boundary))))
    native_acquire = owner.acquire
    from contextlib import contextmanager

    @contextmanager
    def observed_acquire(*args, **kwargs):
        held_guards.append(service._candidate_state_lock._is_owned())
        with native_acquire(*args, **kwargs) as admitted:
            yield admitted

    monkeypatch.setattr(owner, 'acquire', observed_acquire)
    timer = threading.Timer(5.8, lambda: blocker.execute('ROLLBACK'))
    timer.start()
    try:
        result = service.recover_preparation(scope='global', expected_generation=1)
        assert result['generation'] == 2
        assert result['durable_marker'] == 'preserve'
        assert store.lp_preparation()['global_recovery_generation'] == 2
        assert len(attempts) == 2
        assert held_guards == [False, False]
        assert owner.status()['begin_retries'] == 1
        records = [r for r in owner.status()['transactions'] if r['operation'].endswith('.lp_recover_preparation_items')]
        assert [r['begin_attempts'] for r in records] == [1, 2]
        assert len({r['chain_id'] for r in records}) == 1
        assert records[-1]['retry_wait_seconds'] >= 0.0
        assert records[-1]['thread'] == threading.get_ident()
        assert owner.status()['lock_order_violations'] == 0
    finally:
        timer.join(timeout=8)
        if blocker.in_transaction:
            blocker.execute('ROLLBACK')
        blocker.close()
