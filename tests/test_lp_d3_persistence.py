"""F-core public admission contracts with real temporary SQLite transactions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import sqlite3
import threading
import time

import pytest

from open_trader.lp_business_writer import LPBusinessWriter, RequestRecord
from open_trader.lp_metadata_writer import LPMetadataWriter, DEFAULT_POLICY
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


STAMP = datetime(2026, 10, 19, tzinfo=UTC)


def wait_for(read, predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        threading.Event().wait(.002)
    pytest.fail(f'completion watchdog: {read()}')


def request(identity='R1'):
    return RequestRecord(account_id='A', process_epoch='E', request_id=identity,
        session_id='S', condition_id='C', token_id='T', kind='BUY',
        price=Decimal('.4'), quantity=Decimal('10'), requested_at=STAMP)


def test_existing_writer_facades_share_one_owner_without_sharing_budgets(tmp_path, monkeypatch):
    from open_trader.lp_persistence import LPWriteCoordinator
    store = PredictionArbitrageStore(tmp_path)
    store.initialize_lp_metadata_cache()
    peer = PredictionArbitrageStore(tmp_path, initialize=False)
    coordinator = LPWriteCoordinator(store.path)
    assert LPWriteCoordinator(store.path.parent / '.' / store.path.name) is coordinator
    assert LPWriteCoordinator(tmp_path / 'other.sqlite3') is not coordinator
    entered, release = threading.Event(), threading.Event()
    native_begins = []
    real_connect = sqlite3.connect

    class Boundary(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'BEGIN IMMEDIATE':
                native_begins.append(threading.current_thread().name)
            result = super().execute(sql, *args, **kwargs)
            if sql == 'BEGIN IMMEDIATE' and threading.current_thread().name == 'lp-metadata-writer':
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('native holder watchdog')
            return result

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: real_connect(*a, **(k | dict(factory=Boundary))))
    metadata = LPMetadataWriter(store, policy=dict(max_entries=3, max_encoded_bytes=100_000))
    business = LPBusinessWriter(peer, policy=dict(max_entries=3, max_encoded_bytes=100_000))
    try:
        metadata.enqueue({'m1': (STAMP.timestamp() + 10**9, {'condition_id': 'm1'})})
        assert entered.wait(3)
        business.offer(request())
        wait_for(coordinator.status, lambda s: s['waiting'] == 1)
        assert native_begins == ['lp-metadata-writer']
        metadata.enqueue({f'm{i}': (STAMP.timestamp() + 10**9, {'condition_id': f'm{i}'}) for i in (2, 3, 4)})
        business.offer(request('R2'))
        business.offer(request('R3'))
        business.offer(request('R4'))
        assert metadata.status()['dropped_overflow'] == 1
        assert business.status()['dropped_overflow'] == 1
        assert metadata.status()['inflight_entries'] == business.status()['inflight_entries'] == 1
        release.set()
        wait_for(metadata.status, lambda s: s['saved_entries'] == 3)
        wait_for(business.status, lambda s: s['saved_entries'] == 3)
        assert set(store.lp_metadata_cache_entries(now=STAMP)) == {'m1', 'm3', 'm4'}
        with real_connect(store.path) as connection:
            assert [row[0] for row in connection.execute('SELECT action_key FROM lp_actions ORDER BY rowid')] == ['R1', 'R3', 'R4']
        assert DEFAULT_POLICY == dict(max_entries=3000, max_encoded_bytes=16*1024*1024, batch_rows=400)
        assert metadata.status()['failed_entries'] == business.status()['unknown_outcome_entries'] == 0
    finally:
        release.set()
        metadata.stop(timeout=5)
        business.close(timeout=5)


def test_retry_candidate_scan_holds_no_write_lock_and_claims_once(tmp_path, monkeypatch):
    store = PredictionArbitrageStore(tmp_path)
    peer = PredictionArbitrageStore(tmp_path, initialize=False)
    for identity, error, when in (('due', 'TransportError', STAMP),
            ('future', 'TransportError', STAMP + timedelta(seconds=600)),
            ('paused', 'RequestRejectedError', STAMP)):
        store.lp_record_preparation_failure(identity, generation=1, stage='history', error=error, failed_at=when)
    before = {row['condition_id']: row for row in store.lp_preparation_items()}
    assert store.lp_try_acquire_preparation_owner()
    entered, release = threading.Event(), threading.Event()
    real_connect = sqlite3.connect
    claim_begins = []
    scan_thread = [None]

    class Boundary(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if threading.get_ident() == scan_thread[0]:
                if sql == 'BEGIN IMMEDIATE':
                    claim_begins.append(sql)
                if 'SELECT * FROM lp_preparation_items' in sql and 'condition_id IN' in sql:
                    entered.set()
                    if not release.wait(10):
                        raise TimeoutError('scan boundary watchdog')
            return result

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: real_connect(*a, **(k | dict(factory=Boundary))))

    def claim():
        scan_thread[0] = threading.get_ident()
        return store.lp_claim_preparation_retries(now=STAMP + timedelta(seconds=301),
                                                  condition_ids=('due', 'future', 'paused'))

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(claim)
        assert entered.wait(3)
        writing = pool.submit(peer.update_lp_observation, 'A', 'C', {'marker': 'concurrent'})
        try:
            assert writing.result(timeout=3)['marker'] == 'concurrent'
            assert claim_begins == []
        finally:
            release.set()
        assert [row['condition_id'] for row in pending.result(timeout=3)] == ['due']
    claim_begins.clear()
    assert store.lp_claim_preparation_retries(now=STAMP + timedelta(seconds=301), condition_ids=('due',)) == []
    assert claim_begins == []
    after = {row['condition_id']: row for row in store.lp_preparation_items()}
    assert {key: after[key] for key in ('future', 'paused')} == {key: before[key] for key in ('future', 'paused')}
    store.lp_release_preparation_owner()


@pytest.mark.parametrize('change', ['generation', 'recovery', 'claimed', 'late_failure'])
def test_retry_snapshot_rechecks_generation_and_rolls_back_late_failure(tmp_path, monkeypatch, change):
    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_preparation(dict(state='partial', stage='history', generation=1))
    for identity in ('a', 'b'):
        store.lp_record_preparation_failure(identity, generation=1, stage='history',
                                            error='TransportError', failed_at=STAMP)
    assert store.lp_try_acquire_preparation_owner()
    real_connect = sqlite3.connect
    entered, release = threading.Event(), threading.Event()
    worker = [None]

    class Boundary(sqlite3.Connection):
        scanned = False

        def execute(self, sql, *args, **kwargs):
            if threading.get_ident() == worker[0] and 'SELECT * FROM lp_preparation_items' in sql and 'condition_id IN' in sql:
                self.scanned = True
            return super().execute(sql, *args, **kwargs)

        def close(self):
            super().close()
            if self.scanned:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('closed read snapshot watchdog')

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: real_connect(*a, **(k | dict(factory=Boundary))))

    def claim():
        worker[0] = threading.get_ident()
        return store.lp_claim_preparation_retries(now=STAMP + timedelta(seconds=301), condition_ids=('a', 'b'))

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(claim)
            assert entered.wait(3)
            if change == 'generation':
                for identity in ('a', 'b'):
                    store.lp_record_preparation_failure(identity, generation=3, stage='metadata',
                                                        error='TransportError', failed_at=STAMP)
            elif change == 'recovery':
                assert len(store.lp_recover_preparation_items(('a', 'b'))) == 2
            elif change == 'claimed':
                assert len(store.lp_claim_preparation_retries(now=STAMP + timedelta(seconds=301), condition_ids=('a', 'b'))) == 2
            else:
                with real_connect(store.path) as connection:
                    connection.execute("CREATE TRIGGER late_failure BEFORE UPDATE ON lp_preparation_items "
                        "WHEN NEW.condition_id='b' AND NEW.retry_used=1 BEGIN SELECT RAISE(ABORT,'late claim failure'); END")
            before = store.lp_preparation_items()
            release.set()
            if change == 'late_failure':
                with pytest.raises(sqlite3.IntegrityError, match='late claim failure'):
                    pending.result(timeout=3)
            else:
                assert pending.result(timeout=3) == []
            assert store.lp_preparation_items() == before
    finally:
        release.set()
        store.lp_release_preparation_owner()


@pytest.mark.parametrize('boundary', ['external', 'body', 'commit', 'after_commit', 'locked', 'active_begin'])
def test_finite_external_busy_retries_begin_but_never_replays_commit(tmp_path, monkeypatch, boundary):
    store = PredictionArbitrageStore(tmp_path)
    real_connect = sqlite3.connect
    blocker = real_connect(store.path, isolation_level=None, check_same_thread=False)
    if boundary == 'external':
        blocker.execute('BEGIN IMMEDIATE')
    begins, bodies, commits = [], [], []
    timer = None

    def failure(code):
        error = sqlite3.OperationalError('controlled native boundary')
        error.sqlite_errorcode = code
        error.sqlite_errorname = 'SQLITE_BUSY' if code == 5 else 'SQLITE_LOCKED'
        return error

    class Boundary(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            nonlocal timer
            if sql == 'BEGIN IMMEDIATE':
                begins.append(sql)
                if boundary == 'external' and len(begins) == 1:
                    timer = threading.Timer(5.8, lambda: blocker.execute('COMMIT'))
                    timer.start()
                if boundary == 'locked':
                    raise failure(sqlite3.SQLITE_LOCKED)
            if 'INSERT INTO lp_market_observations' in sql:
                bodies.append(sql)
            if sql == 'COMMIT':
                commits.append(sql)
                if boundary == 'commit':
                    raise failure(sqlite3.SQLITE_BUSY)
            result = super().execute(sql, *args, **kwargs)
            if boundary == 'active_begin' and sql == 'BEGIN IMMEDIATE':
                raise failure(sqlite3.SQLITE_BUSY)
            if boundary == 'body' and 'INSERT INTO lp_market_observations' in sql:
                raise failure(sqlite3.SQLITE_BUSY)
            if boundary == 'after_commit' and sql == 'COMMIT':
                raise failure(sqlite3.SQLITE_BUSY)
            return result

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: real_connect(*a, **(k | dict(factory=Boundary))))
    try:
        if boundary == 'external':
            assert store.update_lp_observation('A', 'C', {'marker': 'once'})['marker'] == 'once'
            assert len(begins) == 2
            assert len(bodies) == len(commits) == 1
        else:
            with pytest.raises(sqlite3.OperationalError, match='controlled native boundary'):
                store.update_lp_observation('A', 'C', {'marker': 'once'})
            assert len(begins) == 1
            assert len(bodies) <= 1 and len(commits) <= 1
        with real_connect(store.path) as connection:
            count = connection.execute('SELECT count(*) FROM lp_market_observations').fetchone()[0]
        assert count == (1 if boundary in ('external', 'after_commit') else 0)
    finally:
        if timer is not None:
            timer.join(timeout=10)
        if blocker.in_transaction:
            blocker.execute('ROLLBACK')
        blocker.close()


def test_shutdown_keeps_shared_owner_until_real_inflight_finishes(tmp_path, monkeypatch):
    from open_trader.lp_persistence import LPWriteCoordinator
    import weakref
    store = PredictionArbitrageStore(tmp_path)
    store.initialize_lp_metadata_cache()
    entered, release = threading.Event(), threading.Event()
    real_connect = sqlite3.connect

    class Boundary(sqlite3.Connection):
        def executemany(self, sql, parameters):
            result = super().executemany(sql, parameters)
            if 'lp_market_metadata_cache' in sql:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('shutdown native boundary watchdog')
            return result

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: real_connect(*a, **(k | dict(factory=Boundary))))
    metadata, business = LPMetadataWriter(store), LPBusinessWriter(store)
    owner = LPWriteCoordinator(store.path)
    retained = weakref.ref(owner)
    try:
        metadata.enqueue({'inflight': (STAMP.timestamp() + 10**9, {'condition_id': 'inflight'})})
        assert entered.wait(3)
        metadata.enqueue({'pending': (STAMP.timestamp() + 10**9, {'condition_id': 'pending'})})
        business.offer(request())
        wait_for(owner.status, lambda s: s['waiting'] == 1)
        business.offer(request('pending-business'))
        with pytest.raises(TimeoutError):
            metadata.stop(timeout=0)
        assert metadata.status()['dropped_shutdown'] == 1
        assert metadata.status()['inflight_entries'] == 1
        assert retained() is not None and owner.status()['holder']
        with pytest.raises(TimeoutError):
            business.close(timeout=0)
        assert business.status()['dropped_shutdown'] == 1
        assert business.status()['thread_alive']
        release.set()
        metadata.stop(timeout=3)
        business.close(timeout=3)
        assert metadata.status()['saved_entries'] == business.status()['saved_entries'] == 1
        assert owner.status()['waiting'] == 0 and not owner.status()['holder']
        assert set(store.lp_metadata_cache_entries(now=STAMP)) == {'inflight'}
        # Closing either facade does not close the per-database owner/client.
        assert store.update_lp_observation('A', 'C', {'after_close': True})['after_close'] is True
    finally:
        release.set()
        metadata.stop(timeout=5)
        business.close(timeout=5)


def test_fifo_admission_prioritizes_reliable_and_control_without_overtaking_peers(tmp_path):
    from open_trader.lp_persistence import LPWriteCoordinator
    owner = LPWriteCoordinator(tmp_path / 'priority.sqlite3')
    executed = []

    def enter(identity, priority):
        with owner.acquire(priority):
            executed.append(identity)

    with ThreadPoolExecutor(max_workers=6) as pool:
        with owner.acquire():
            futures = []
            for index, (identity, priority) in enumerate((('b1', 'background'), ('b2', 'background'),
                    ('r1', 'reliable'), ('r2', 'reliable'), ('c1', 'control'), ('c2', 'control')), 1):
                futures.append(pool.submit(enter, identity, priority))
                wait_for(owner.status, lambda s: s['waiting'] == index)
        for future in futures:
            future.result(timeout=3)
    assert executed == ['c1', 'c2', 'r1', 'r2', 'b1', 'b2']


@pytest.mark.parametrize('reason', ['deadline', 'cancel'])
def test_admission_timeout_and_cancel_before_begin_are_known_not_written(tmp_path, reason):
    from open_trader.lp_persistence import LPWriteCoordinator, AdmissionCancelled, AdmissionTimeout
    owner = LPWriteCoordinator(tmp_path / 'cancel.sqlite3')
    cancellation = threading.Event()
    body = []

    def enter():
        with owner.acquire(deadline=time.monotonic() + (.15 if reason == 'deadline' else 3), cancellation=cancellation):
            body.append('must not run')

    with ThreadPoolExecutor(max_workers=1) as pool:
        with owner.acquire():
            pending = pool.submit(enter)
            wait_for(owner.status, lambda s: s['waiting'] == 1)
            if reason == 'cancel':
                cancellation.set()
            with pytest.raises(AdmissionCancelled if reason == 'cancel' else AdmissionTimeout) as outcome:
                pending.result(timeout=2)
            assert outcome.value.known_not_written is True
            assert body == [] and owner.status()['waiting'] == 0
        with owner.acquire():
            body.append('next accepted')
    assert body == ['next accepted']


@pytest.mark.parametrize('phase', ['queued', 'native'])
def test_control_deadline_includes_admission_and_distinguishes_native_unknown(tmp_path, monkeypatch, phase):
    from dataclasses import asdict
    from open_trader.lp_persistence import LPWriteCoordinator
    from open_trader.polymarket_lp_control import LPControlOwner
    from open_trader.polymarket_lp_current_state import CurrentLPState, StoredControl, CommittedControl, ValidatedStartAdmission
    from test_lp_control_contract import ActivationPort, FatalPort, Clock
    store = PredictionArbitrageStore(tmp_path)
    clock = Clock()
    state = CurrentLPState('A', 'process', clock=clock, utc_now=lambda: STAMP)
    entered, release = threading.Event(), threading.Event()
    begins = []
    real_connect = sqlite3.connect

    class Boundary(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'BEGIN IMMEDIATE':
                begins.append(sql)
            result = super().execute(sql, *args, **kwargs)
            if sql == 'BEGIN IMMEDIATE' and phase == 'native':
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('actual native watchdog')
            return result

    class Port:
        def load(self, account_id):
            return StoredControl(account_id, True, True, STAMP, STAMP)

        def save(self, write):
            return CommittedControl(**store.lp_save_current_control(asdict(write)))

        def save_with_admission(self, write, attempt):
            return CommittedControl(**store.lp_save_current_control(asdict(write), admission=attempt))

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: real_connect(*a, **(k | dict(factory=Boundary))))
    fatal = FatalPort()
    owner = LPControlOwner(state, Port(), ActivationPort(state), fatal, clock=clock, utc_now=lambda: STAMP)
    owner.initialize(ValidatedStartAdmission('process', 'start-claim'))
    coordinator = LPWriteCoordinator(store.path)
    from contextlib import nullcontext
    try:
        with coordinator.acquire() if phase == 'queued' else nullcontext():
            owner.request_running(False)
            if phase == 'queued':
                wait_for(coordinator.status, lambda s: s['waiting'] == 1)
            else:
                assert entered.wait(3)
            assert owner.status().attempt_started_monotonic == 100
            assert owner.status().attempt_deadline_monotonic == 110
            clock.value = 111
            if phase == 'queued':
                status = wait_for(lambda: owner.poll(clock()), lambda s: s.save_state == 'retry_wait')
                assert status.fatal is None and status.saved_version == 0
                assert begins == [] and not fatal.signals
                assert status.retry_at_monotonic == 112
            else:
                status = owner.poll(clock())
                assert status.save_state == 'unknown' and status.fatal.outcome == 'unknown'
                assert len(begins) == 1 and status.native_save_in_flight
                assert not owner.close(0).writer_drained
        release.set()
        if phase == 'queued':
            clock.value = 112
            wait_for(lambda: owner.poll(clock()), lambda s: s.save_state == 'saved')
            assert len(begins) == 1 and not fatal.signals
        else:
            wait_for(lambda: owner.poll(clock()), lambda s: not s.native_save_in_flight)
            assert owner.status().fatal is not None and len(begins) == 1
    finally:
        release.set()
        owner.close(3)


@pytest.mark.parametrize('d2_enabled', [True, False])
def test_d2_routes_never_construct_or_call_legacy_mutations(tmp_path, monkeypatch, d2_enabled):
    """F13 characterization, F1d option B: test routing, not financial startup.

    The legacy control retains its real startup breaker. Constructed legacy
    pool/hooks and four legacy gate responses prove branch selection without
    fabricating a ready account or changing financial preconditions. The D2
    arm executes manual_call closures and keeps the zero-legacy-call assertion.
    """
    from types import SimpleNamespace
    from tests.test_prediction_arbitrage_execution import execution_fixture
    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    from open_trader.polymarket_lp import PolymarketLPService
    _, trading, store, monitor = execution_fixture(tmp_path)
    trading.config = SimpleNamespace(wallet_address="0x" + "1" * 40)
    lp = PolymarketLPService(store, trading)
    legacy_calls, manual_calls, entries = [], [], []
    for method in ('start', 'augment', 'submit_entry', 'submit_augment', 'tick_with_apply_lock', 'tick'):
        def spy(*args, _method=method, **kwargs):
            legacy_calls.append(_method)
            return {'routed': 'legacy'}
        monkeypatch.setattr(lp, method, spy)

    class D2:
        _closed = False
        account_id = trading.config.wallet_address
        policy = SimpleNamespace(buy_price_level=1)
        state = SimpleNamespace(snapshot=lambda: SimpleNamespace(current=SimpleNamespace(
            shutdown_requested=False, fatal_id=None)))

        def bind_mutation_admission(self, *args):
            pass

        def manual_call(self, operation):
            manual_calls.append(operation)
            return operation()

        def manual_entry(self, *args, **kwargs):
            entries.append((args, kwargs))
            return {'routed': 'd2'}

        def manual_group(self, sid):
            return None

    live = D2()
    live.store, live.trading = store, trading
    execution = PredictionExecutionService(store=store, trading=trading, monitor=monitor, notifier=object(),
        lock_path=tmp_path/'route.lock', lp=lp, lp_d2=live if d2_enabled else None)
    startup = execution.reconcile_startup()
    if d2_enabled:
        assert startup['state'] == 'ready'
        assert getattr(execution, '_auto_pool', None) is None
        assert lp._facts_publisher is lp._facts_validator is lp._facts_attention_flusher is None
    else:
        assert startup['state'] == 'locked'
        from open_trader.polymarket_lp_auto import LPAutoPool
        assert isinstance(execution._auto_pool, LPAutoPool)
        assert lp._facts_publisher is not None
    results = [execution.lp_start('missing-preview', 'start-key'),
        execution.lp_augment('missing-group', 'missing-preview', 'augment-key'),
        execution.lp_submit_entry({'idempotency_key': 'entry-key'}),
        execution.lp_submit_augment({'session_id': 'missing-group', 'idempotency_key': 'submit-augment-key'})]
    if d2_enabled:
        assert len(manual_calls) == 4
        assert len(entries) == 2  # entry/start; missing group rejects inside D2, never falls back
        assert legacy_calls == []
    else:
        assert manual_calls == entries == []
        assert legacy_calls == []  # genuine legacy safety gates run first
        assert results == [{'state': 'locked', 'reason': 'circuit_breaker_open'}] * 4

    wakes = []
    execution.set_lp_auto_scheduler(SimpleNamespace(request_check=lambda: wakes.append('requested')))
    tick_result = execution.lp_tick()
    if d2_enabled:
        assert tick_result == {'state': 'scheduled'}
        assert wakes == ['requested']
        assert legacy_calls == []  # neither legacy tick port is reached
    else:
        assert tick_result == {'routed': 'legacy'}
        assert wakes == []
        assert legacy_calls == ['tick_with_apply_lock']
        monkeypatch.setattr(lp, 'tick_with_apply_lock', None)
        assert execution.lp_tick() == {'routed': 'legacy'}
        assert legacy_calls == ['tick_with_apply_lock', 'tick']


@pytest.mark.parametrize('port', ['reward', 'preparation', 'share'])
def test_global_lock_order_has_no_memory_lock_to_permit_inversion(tmp_path, port):
    from open_trader.lp_persistence import LPWriteCoordinator
    from open_trader.polymarket_lp import PolymarketLPService
    from tests.test_polymarket_lp import _LPCandidateQueryExchange

    class Exchange(_LPCandidateQueryExchange):
        def lp_reward_snapshot(self, reward_date, condition_id):
            return dict(state='known', reward_date=reward_date, condition_id=condition_id,
                        account_amount=Decimal('1.1'), market_amount=Decimal('.62'))

    store = PredictionArbitrageStore(tmp_path)
    if port == 'share':
        old = dict(last_share_checked_at='2026-10-19T00:01:00Z',
                   breach_started_at='2026-10-19T00:00:00Z', notification_pending=True)
        new = dict(last_share_checked_at='2026-10-19T00:02:00Z',
                   breach_started_at='2026-10-19T00:02:00Z', notification_pending=True)
        store.update_lp_observation('A', 'C', {'share_alert': old})
        store.update_lp_observation('A', 'C', {'share_alert': new})
        store.update_lp_observation('A', 'C', {'share_alert': {'notification_pending': False,
            'breach_started_at': None}}, expected_share_alert=old)
        assert store.lp_observations('A')['C']['share_alert'] == new
        return
    store.lp_create_session('reward', 'reward-key', state='entry_open', payload=dict(
        condition_id='condition-M0', reward_date=STAMP.date().isoformat(), paid_rewards='0', trade_pnl='0'))
    service = PolymarketLPService(store, Exchange(STAMP, {'M0': Decimal('100')}), clock=lambda: STAMP)
    owner = LPWriteCoordinator(store.path)
    refresh = (lambda: service.refresh_rewards('reward')) if port == 'reward' else service.refresh_price_history
    read = (lambda: service.status('reward')) if port == 'reward' else service.preparation_snapshot
    with ThreadPoolExecutor(max_workers=2) as pool:
        with owner.acquire():
            pending = pool.submit(refresh)
            wait_for(owner.status, lambda s: s['waiting'] == 1)
            # A queued writer must not own the memory publication mutex.
            visible = pool.submit(read)
            assert isinstance(visible.result(timeout=3), dict)
        pending.result(timeout=5)
    assert owner.status()['lock_order_violations'] == 0


def test_transaction_metrics_measure_stages_without_changing_checkpoint_policy(tmp_path, monkeypatch):
    from open_trader.lp_persistence import LPWriteCoordinator
    import json
    real_connect = sqlite3.connect
    checkpoints = []

    class Boundary(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if 'wal_checkpoint' in sql.lower():
                checkpoints.append(sql)
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: real_connect(*a, **(k | dict(factory=Boundary))))
    store = PredictionArbitrageStore(tmp_path)
    startup_checkpoints = list(checkpoints)
    store.initialize_lp_metadata_cache()
    store.lp_competitiveness_upsert(((f'C{i}', Decimal('.123456789'), STAMP) for i in range(500)),
                                   progress={'next_start_cursor': 'must-not-be-a-metric'})
    store.lp_metadata_cache_store_entries({'m1': (STAMP.timestamp()+10**9, {'private': 'must-not-be-a-metric'})})
    status = LPWriteCoordinator(store.path).status()
    assert checkpoints == startup_checkpoints == ['PRAGMA wal_checkpoint(TRUNCATE)']
    records = status['transactions']
    competition = next(r for r in records if r['operation'].endswith('.lp_competitiveness_upsert'))
    metadata = next(r for r in records if r['operation'].endswith('.lp_metadata_cache_store_entries'))
    for record in (competition, metadata):
        for field in ('wall_seconds', 'thread_cpu_seconds', 'begin_seconds', 'body_seconds',
                      'guard_wait_seconds', 'commit_seconds', 'after_commit_seconds', 'permit_wait_seconds'):
            assert type(record[field]) is float and record[field] >= 0, (field, record)
        assert type(record['wal_bytes_before']) is int and record['wal_bytes_before'] >= 0
        assert record['stages']['encode_seconds'] >= 0
        assert record['stages']['executemany_seconds'] >= 0
        assert record['phase'] == 'complete'
    assert competition['stages']['bookmark_seconds'] >= 0
    assert competition['stages']['input_rows'] == 500
    assert status['checkpoint']['busy'] == 0
    assert 'must-not-be-a-metric' not in json.dumps(status)


def test_all_lp_write_ports_use_one_resolved_database_coordinator(tmp_path):
    from open_trader.lp_persistence import LPWriteCoordinator, LP_WRITE_OPERATIONS
    import ast
    import inspect
    import re
    store = PredictionArbitrageStore(tmp_path)
    peer = PredictionArbitrageStore(tmp_path, initialize=False)
    owner = LPWriteCoordinator(store.path)
    assert LPWriteCoordinator(peer.path.resolve()) is owner
    # Independent static SQL inventory, including helpers that reuse a supplied
    # transaction. The generic preview table also needs explicit LP tagging.
    module = ast.parse(inspect.getsource(__import__('open_trader.prediction_arbitrage_store', fromlist=['x'])))
    cls = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == 'PredictionArbitrageStore')
    for method in cls.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        writes_lp = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == 'execute' and n.args and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str)
            and re.search(r'\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+lp_', n.args[0].value, re.I)
            for n in ast.walk(method))
        if writes_lp and 'connection' not in [a.arg for a in (*method.args.args, *method.args.kwonlyargs)]:
            assert method.name in LP_WRITE_OPERATIONS, method.name
    writes = [lambda: peer.create_preview({'kind': 'LP'}, expires_at='2026-10-19T00:00:10Z',
                    created_at='2026-10-19T00:00:00Z', lp_priority='reliable'),
              lambda: peer.save_lp_observation('A', 'C', {'state': 'known'}),
              lambda: peer.lp_save_preparation(dict(state='partial', stage='history', generation=1)),
              lambda: peer.lp_competitiveness_upsert((('C', Decimal('.12'), STAMP),), progress={'next_start_cursor': 'P1'})]
    with ThreadPoolExecutor(max_workers=1) as pool:
        for operation in writes:
            with owner.acquire():
                pending = pool.submit(operation)
                wait_for(owner.status, lambda s: s['waiting'] == 1 or pending.done())
                if pending.done():
                    pending.result()  # exposes a missing native LP admission seam
                assert owner.status()['waiting'] == 1
            pending.result(timeout=3)
    assert not owner.status()['holder'] and owner.status()['waiting'] == 0


@pytest.mark.parametrize('failure', ['missing', 'other_value', 'sqlite', 'runtime'])
def test_reward_atomic_write_maps_only_missing_session(tmp_path, monkeypatch, failure):
    """F14a characterizes Ray-approved C already present from the F1e spike.

    Do not fabricate RED by reverting an approved production guard. The missing
    case deletes the real row at the public Store write boundary, after the
    service has selected its session. Other failures keep their exact identity.
    """
    from open_trader.polymarket_lp import PolymarketLPService
    from tests.test_polymarket_lp import _LPCandidateQueryExchange

    class Exchange(_LPCandidateQueryExchange):
        def lp_reward_snapshot(self, reward_date, condition_id):
            return dict(state='known', reward_date=reward_date, condition_id=condition_id,
                        account_amount=Decimal('1.1'), market_amount=Decimal('.62'))

    store = PredictionArbitrageStore(tmp_path)
    store.lp_create_session('reward', 'reward-key', state='entry_open', payload=dict(
        condition_id='condition-M0', reward_date=STAMP.date().isoformat(), paid_rewards='0', trade_pnl='0'))
    service = PolymarketLPService(store, Exchange(STAMP, {'M0': Decimal('100')}), clock=lambda: STAMP)
    update = store.lp_update_session
    error = {'other_value': ValueError('different_value_error'),
             'sqlite': sqlite3.OperationalError('actual_store_failure'),
             'runtime': RuntimeError('unexpected_store_failure')}.get(failure)
    calls = []

    def at_write(session_id, **kwargs):
        calls.append(session_id)
        if failure == 'missing':
            with store._transaction(lp_priority='reliable') as connection:
                connection.execute('DELETE FROM lp_sessions WHERE session_id=?', (session_id,))
            return update(session_id, **kwargs)
        raise error

    monkeypatch.setattr(store, 'lp_update_session', at_write)
    if failure == 'missing':
        assert service.refresh_rewards('reward') == {'state': 'none', 'session_id': 'reward'}
        assert store.lp_session('reward') is None
    else:
        with pytest.raises(type(error)) as caught:
            service.refresh_rewards('reward')
        assert caught.value is error
    assert calls == ['reward']


def test_reward_concurrent_narrow_merge_preserves_both_fields_and_revisions(tmp_path, monkeypatch):
    """F14b characterizes atomic narrow patches; independent literal oracle.

    Both invocations wait before BEGIN, then the single writer admits them.
    Either completion order is valid; each committed revision advances once.
    """
    from open_trader.lp_persistence import LPWriteCoordinator
    from open_trader.polymarket_lp import PolymarketLPService
    from tests.test_polymarket_lp import _LPCandidateQueryExchange

    class Exchange(_LPCandidateQueryExchange):
        def lp_reward_snapshot(self, reward_date, condition_id):
            return dict(state='known', reward_date=reward_date, condition_id=condition_id,
                        account_amount=Decimal('1.1'), market_amount=Decimal('.62'))

    store = PredictionArbitrageStore(tmp_path)
    other = PredictionArbitrageStore(tmp_path, initialize=False)
    store.lp_create_session('reward', 'reward-key', state='entry_open', payload=dict(
        condition_id='condition-M0', reward_date=STAMP.date().isoformat(), paid_rewards='0', trade_pnl='0'))
    initial = store.lp_session_revision('reward')
    revisions = []
    native_connect = sqlite3.connect

    class CommitRevision(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql == 'COMMIT' and threading.current_thread().name.startswith('reward-merge-'):
                # Observe the public revision port after real COMMIT and before
                # releasing admission. No second writer can advance it yet.
                revisions.append(store.lp_session_revision('reward'))
            return result

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw:
        native_connect(*a, **(kw | dict(factory=CommitRevision))))
    service = PolymarketLPService(store, Exchange(STAMP, {'M0': Decimal('100')}), clock=lambda: STAMP)
    owner = LPWriteCoordinator(store.path)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='reward-merge-') as pool:
        with owner.acquire():
            reward = pool.submit(service.refresh_rewards, 'reward')
            patch = pool.submit(other.lp_update_session, 'reward', patch={'operator_note': 'preserve-me'})
            wait_for(owner.status, lambda row: row['waiting'] == 2)
        reward_result, patch_result = reward.result(timeout=5), patch.result(timeout=5)
    current = store.lp_session('reward')
    assert current['operator_note'] == 'preserve-me'
    assert Decimal(str(current['reward_observation']['account_amount'])) == Decimal('1.1')
    assert current['paid_rewards'] == '0' and current['trade_pnl'] == '0'
    assert revisions == [initial+1, initial+2]
    assert store.lp_session_revision('reward') == initial+2
    assert '_lp_revision' not in reward_result and '_lp_revision' not in patch_result
