"""Preparation retries and explicit global recovery use isolated real SQLite."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import sqlite3
import threading

import pytest

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_lp_account_reservation_reconciliation import runtime, _refill_identity


T = datetime(2026, 10, 6, tzinfo=UTC)


class Exchange:
    def __init__(self, clock, cache=None):
        self.clock = clock
        self.cache = cache
        self.calls = 0
        self.error = None

    def lp_reward_catalog(self, *, stop_event=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.cache is not None:
            with sqlite3.connect(self.cache, timeout=0) as connection:
                connection.execute('BEGIN IMMEDIATE')
                connection.rollback()
        return dict(state='known', complete=True, checked_at=self.clock[0], markets=[
            dict(condition_id='healthy', daily_pool_usd=Decimal('120'), reward_active=True)])

    def lp_market_metadata(self, condition_ids, *, stop_event=None):
        return {c: dict(market_id='market-healthy', condition_id=c, accepting_orders=True,
                       outcomes={'yes': dict(label='YES', token_id='token-healthy')}) for c in condition_ids}

    def lp_price_history(self, token_ids, *, start_ts, end_ts, fidelity=1, stop_event=None):
        return dict(state='known', history={t: [dict(t=start_ts, p='0.40'), dict(t=end_ts, p='0.41')]
                                           for t in token_ids})


def test_sqlite_contention_retries_after_release_and_restart(tmp_path):
    clock = [T]
    cache = tmp_path / 'catalog-cache.sqlite3'
    store = PredictionArbitrageStore(tmp_path / 'data')
    exchange = Exchange(clock, cache)
    service = PolymarketLPService(store, exchange, clock=lambda: clock[0])
    with sqlite3.connect(cache) as holder:
        holder.execute('CREATE TABLE cache(value)')
        holder.commit()
        holder.execute('BEGIN IMMEDIATE')
        try:
            failed = service.refresh_price_history()
            assert failed['preparation']['paused'] is False
            assert failed['preparation']['state'] == 'waiting_retry'
            assert failed['preparation']['last_sqlite_errorcode'] == sqlite3.SQLITE_BUSY
            assert failed['preparation']['last_sqlite_errorname'] == 'SQLITE_BUSY'
            assert failed['preparation']['last_error'] == 'OperationalError'
            clock[0] += timedelta(seconds=59)
            assert service.refresh_price_history()['preparation_outcome'] == 'waiting_retry'
            assert exchange.calls == 1
        finally:
            holder.rollback()
    # Rebuild from the same durable retry; never use manual recovery.
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path / 'data'), exchange,
                                 clock=lambda: clock[0])
    clock[0] = datetime.fromisoformat(str(service.preparation_snapshot()['next_retry_at']))
    assert service.refresh_price_history()['preparation_outcome'] == 'success'
    assert exchange.calls == 2
    assert store.lp_price_history_summary('healthy', 'token-healthy', now=clock[0])['state'] == 'known'
    # A later independent lock starts a new bounded retry, not a permanent pause.
    with sqlite3.connect(cache) as holder:
        holder.execute('BEGIN IMMEDIATE')
        try:
            again = service.refresh_price_history()['preparation']
            assert again['paused'] is False
            assert again['failure_count'] == 1
        finally:
            holder.rollback()
    clock[0] = datetime.fromisoformat(str(again['next_retry_at']))
    assert service.refresh_price_history()['preparation_outcome'] == 'success'
    assert exchange.calls == 4


def test_real_adapter_preserves_sqlite_contention_for_preparation(tmp_path):
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
    from tests.test_polymarket_trading import FakeClient, SIGNER, WALLET

    cache = tmp_path / 'sdk-dependency.sqlite3'
    class Public:
        def list_current_rewards(self, *, sponsored=False):
            with sqlite3.connect(cache, timeout=0) as connection:
                connection.execute('BEGIN IMMEDIATE')
            return ()

    adapter = PolymarketTradingClient(TradingConfig(SIGNER, WALLET), client=FakeClient(),
                                    public_client_factory=Public)
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path / 'data'), adapter, clock=lambda: T)
    try:
        with sqlite3.connect(cache) as holder:
            holder.execute('CREATE TABLE cache(value)')
            holder.commit()
            holder.execute('BEGIN IMMEDIATE')
            try:
                result = service.refresh_price_history()
                assert result['preparation']['paused'] is False
                assert result['preparation']['last_sqlite_errorcode'] == sqlite3.SQLITE_BUSY
            finally:
                holder.rollback()
    finally:
        adapter.close()


def test_global_recovery_preserves_other_pauses_and_error_audit(tmp_path):
    clock = [T]
    store = PredictionArbitrageStore(tmp_path / 'data')
    exchange = Exchange(clock)
    exchange.error = sqlite3.OperationalError('private SQL text must not be retained')
    service = PolymarketLPService(store, exchange, clock=lambda: clock[0])
    service.refresh_price_history()
    old = service.preparation_snapshot()
    assert old['paused'] is True
    for i in range(10):
        store.lp_record_preparation_failure(str(i), generation=old['generation'], stage='metadata',
                                           error='event_read_RequestRejectedError', failed_at=T)
    items = store.lp_preparation_items()
    exchange.error = None
    recovered = service.recover_preparation(scope='global', expected_generation=old['generation'])
    assert recovered['paused'] is False
    assert recovered['generation'] == old['generation'] + 1
    assert recovered['recovered_condition_ids'] == []
    assert store.lp_preparation_items() == items
    assert store.lp_record_preparation_failure('0', generation=old['generation'], stage='history',
                                              error='TimeoutError', failed_at=T) is None
    assert store.lp_clear_preparation_items(('0',), generation=old['generation']) == 0
    assert store.lp_preparation_items() == items
    assert recovered['last_success_at'] == old['last_success_at']
    assert recovered['recovery_history'][-1]['last_error'] == 'OperationalError'
    assert 'private SQL' not in str(recovered)
    with pytest.raises(ValueError, match='generation mismatch'):
        service.recover_preparation(scope='global', expected_generation=old['generation'])
    assert store.lp_preparation_items() == items
    restarted = PolymarketLPService(PredictionArbitrageStore(tmp_path / 'data'), exchange, clock=lambda: clock[0])
    assert restarted.refresh_price_history()['preparation_outcome'] == 'success'
    assert store.lp_price_history_summary('healthy', 'token-healthy', now=T)['state'] == 'known'
    assert store.lp_preparation_items() == items


@pytest.mark.parametrize('fault', ['locked', 'schema', 'unknown', 'auth', 'integrity'])
def test_database_error_policy_and_pause_survive_restart(tmp_path, fault):
    clock = [T]
    store = PredictionArbitrageStore(tmp_path / 'data')
    exchange = Exchange(clock)
    first = sqlite3.connect(f'file:{tmp_path}/shared.sqlite3?cache=shared', uri=True, timeout=0)
    second = sqlite3.connect(f'file:{tmp_path}/shared.sqlite3?cache=shared', uri=True, timeout=0)
    try:
        first.execute('CREATE TABLE cache(value UNIQUE)')
        first.commit()
        if fault == 'locked':
            first.execute('BEGIN IMMEDIATE')
            first.execute('INSERT INTO cache VALUES (1)')
            exchange.lp_reward_catalog = lambda **kwargs: second.execute('SELECT * FROM cache').fetchall()
        elif fault == 'schema':
            exchange.lp_reward_catalog = lambda **kwargs: second.execute('SELECT * FROM missing_private_table')
        elif fault == 'integrity':
            first.execute('INSERT INTO cache VALUES (1)')
            first.commit()
            exchange.lp_reward_catalog = lambda **kwargs: second.execute('INSERT INTO cache VALUES (1)')
        else:
            exchange.error = (sqlite3.OperationalError('unknown private detail') if fault == 'unknown'
                              else PermissionError('authentication private credential'))
        service = PolymarketLPService(store, exchange, clock=lambda: clock[0])
        failed = service.refresh_price_history()['preparation']
        assert failed['paused'] is (fault != 'locked')
        assert failed['last_error_category'] == ('transient' if fault == 'locked' else 'operator_attention')
        assert 'private' not in str(failed)
        if fault == 'locked':
            assert failed['last_sqlite_errorcode'] & 0xff == sqlite3.SQLITE_LOCKED
        first.rollback()
        second.rollback()
        healthy = Exchange(clock)
        restarted = PolymarketLPService(PredictionArbitrageStore(tmp_path / 'data'), healthy, clock=lambda: clock[0])
        clock[0] += timedelta(hours=12)
        result = restarted.refresh_price_history()
        assert result['preparation_outcome'] == ('success' if fault == 'locked' else 'paused')
        assert healthy.calls == (1 if fault == 'locked' else 0)
    finally:
        first.rollback()
        second.rollback()
        first.close()
        second.close()


@pytest.mark.parametrize('late_error', [False, True])
@pytest.mark.parametrize('stage', ['catalog', 'history'])
def test_global_recovery_fences_late_old_results(tmp_path, late_error, stage):
    clock = [T]
    store = PredictionArbitrageStore(tmp_path / 'data')
    entered, release = threading.Event(), threading.Event()
    exchange = Exchange(clock)
    reader = exchange.lp_reward_catalog if stage == 'catalog' else exchange.lp_price_history
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'Independent late-result watchdog'
        if late_error:
            raise sqlite3.OperationalError('private old failure')
        return reader(*args, **kwargs)
    if stage == 'catalog':
        exchange.lp_reward_catalog = delayed
    else:
        exchange.lp_price_history = delayed
    old_service = PolymarketLPService(store, exchange, clock=lambda: clock[0])
    errors = []
    def run():
        try:
            old_service.refresh_price_history()
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        paused = {**store.lp_preparation(), 'state': 'paused', 'paused': True, 'last_error': 'OperationalError'}
        store.lp_save_preparation(paused, expected_generation=paused['generation'])
        replacement = PolymarketLPService(store, Exchange(clock), clock=lambda: clock[0])
        recovered = replacement.recover_preparation(scope='global', expected_generation=paused['generation'])
        recovered_durable = store.lp_preparation()
        assert recovered['generation'] == paused['generation'] + 1
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert errors == []
        assert store.lp_preparation() == recovered_durable
        assert old_service._prepared_input_snapshot() is None
        assert store.lp_price_history_summary('healthy', 'token-healthy', now=T) is None
        assert replacement.refresh_price_history()['preparation_outcome'] == 'success'
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()


def test_http_global_recovery_validates_scope_identity_and_wakes_runtime(tmp_path):
    import json
    from open_trader.prediction_runtime import PredictionRuntime
    from tests.test_prediction_service import _ProductionRuntime, _production_server, _production_request, _response

    clock = [T]
    store = PredictionArbitrageStore(tmp_path / 'data')
    exchange = Exchange(clock)
    exchange.error = sqlite3.OperationalError('old unknown error')
    service = PolymarketLPService(store, exchange, clock=lambda: clock[0])
    service.refresh_price_history()
    old = store.lp_preparation()
    runtime = PredictionRuntime(data_dir=tmp_path, prediction_config_path=tmp_path / 'unused.json',
                                dashboard_url='http://127.0.0.1/', enable_n_leg_background=False)
    runtime.lp = service
    wrapper = _ProductionRuntime()
    wrapper.recover_lp_preparation = runtime.recover_lp_preparation
    path = '/api/prediction-arbitrage/lp/candidates/refresh'
    valid = dict(manual_recovery=True, recovery_scope='global', expected_generation=old['generation'])
    with _production_server(wrapper) as (base, _):
        for payload in (
            {**valid, 'expected_generation': True}, {**valid, 'expected_generation': 0},
            {**valid, 'recovery_scope': 'market'}, {**valid, 'recovery_scope': []}, {**valid, 'manual_recovery': False},
            {k: v for k, v in valid.items() if k != 'expected_generation'},
            {**valid, 'expected_generation': old['generation'] + 1},
        ):
            status, _ = _response(_production_request(base, path, data=json.dumps(payload).encode()))
            assert status == 400, payload
            assert store.lp_preparation() == old
            assert not runtime._history_wakeup_event.is_set()
        status, result = _response(_production_request(base, path, data=json.dumps(valid).encode()))
        assert status == 200
        assert result['generation'] == old['generation'] + 1
        assert result['recovered_condition_ids'] == []
        assert runtime._history_wakeup_event.is_set()
        assert runtime._lp_candidate_refresh_requested.is_set()
        runtime._history_wakeup_event.clear()
        assert _response(_production_request(base, path, data=json.dumps(valid).encode()))[0] == 400
        assert not runtime._history_wakeup_event.is_set()
        # The old body retains its all-market contract.
        store.lp_record_preparation_failure('paused-market', generation=result['generation'], stage='metadata',
                                           error='event_read_RequestRejectedError', failed_at=T)
        status, result = _response(_production_request(base, path, data=b'{"manual_recovery":true}'))
        assert status == 200
        assert result['recovered_condition_ids'] == ['paused-market']


@pytest.mark.parametrize('stage', ['metadata', 'history', 'history-write'])
def test_database_failure_at_preparation_boundaries_is_not_silently_retried(tmp_path, stage):
    clock = [T]
    store = PredictionArbitrageStore(tmp_path / 'data')
    exchange = Exchange(clock)
    def unknown(*args, **kwargs):
        raise sqlite3.OperationalError('unknown private SQL detail')
    if stage == 'metadata':
        exchange.lp_market_metadata = unknown
    elif stage == 'history':
        exchange.lp_price_history = unknown
    else:
        # Real SQLite statement failure at the durable batch publication.
        with sqlite3.connect(store.path) as connection:
            connection.execute("CREATE TRIGGER fail_history BEFORE INSERT ON lp_price_history_cache BEGIN SELECT RAISE(ABORT, 'private failure'); END")
    result = PolymarketLPService(store, exchange, clock=lambda: clock[0]).refresh_price_history()
    assert result['preparation']['paused'] is True
    assert result['preparation']['last_error_category'] == 'operator_attention'
    assert 'private' not in str(result)
    assert store.lp_price_history_summary('healthy', 'token-healthy', now=T) is None


def test_global_recovery_transaction_failure_rolls_back_and_does_not_wake(tmp_path):
    from open_trader.prediction_runtime import PredictionRuntime
    clock = [T]
    store = PredictionArbitrageStore(tmp_path / 'data')
    exchange = Exchange(clock)
    exchange.error = sqlite3.OperationalError('old unknown')
    service = PolymarketLPService(store, exchange, clock=lambda: clock[0])
    service.refresh_price_history()
    before = store.lp_preparation()
    store.lp_record_preparation_failure('other', generation=before['generation'], stage='metadata',
                                       error='event_read_RequestRejectedError', failed_at=T)
    items = store.lp_preparation_items()
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TABLE unrelated(value)")
        connection.execute("CREATE TRIGGER reject_recovery AFTER UPDATE ON lp_preparation BEGIN INSERT INTO unrelated VALUES (1); END")
    runtime = PredictionRuntime(data_dir=tmp_path, prediction_config_path=tmp_path / 'unused.json',
                                dashboard_url='http://127.0.0.1/', enable_n_leg_background=False)
    runtime.lp = service
    with pytest.raises(sqlite3.DatabaseError):
        runtime.recover_lp_preparation(scope='global', expected_generation=before['generation'])
    assert store.lp_preparation() == before
    assert store.lp_preparation_items() == items
    assert not runtime._history_wakeup_event.is_set()
    assert not runtime._lp_candidate_refresh_requested.is_set()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute('SELECT * FROM unrelated').fetchall() == []
        connection.execute('DROP TRIGGER reject_recovery')
    assert runtime.recover_lp_preparation(scope='global', expected_generation=before['generation'])['paused'] is False


@pytest.mark.parametrize('stage', ['metadata', 'history'])
@pytest.mark.parametrize('code', [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_ERROR])
def test_structured_database_failures_use_global_policy(tmp_path, stage, code):
    clock = [T]
    store = PredictionArbitrageStore(tmp_path / 'data')
    exchange = Exchange(clock)
    facts = dict(error_type='OperationalError', error_chain=('OperationalError',),
                 sqlite_errorcode=code, sqlite_errorname='SQLITE_BUSY' if code == 5 else 'SQLITE_ERROR')
    if stage == 'metadata':
        exchange.lp_market_metadata_batch = lambda ids, **kwargs: dict(state='unknown', markets={},
            failed_ids={c: 'market_read_OperationalError' for c in ids}, failure_facts={c: facts for c in ids})
    else:
        exchange.lp_price_history = lambda ids, **kwargs: dict(state='unknown', history={},
            errors={t: 'OperationalError' for t in ids}, **facts)
    result = PolymarketLPService(store, exchange, clock=lambda: clock[0]).refresh_price_history()
    assert result['preparation']['paused'] is (code != sqlite3.SQLITE_BUSY)
    assert result['preparation']['state'] == ('waiting_retry' if code == sqlite3.SQLITE_BUSY else 'paused')
    assert result['preparation']['last_sqlite_errorcode'] == code
    assert result['preparation']['last_sqlite_errorname'] == facts['sqlite_errorname']


def test_normal_monitors_resume_preparation_scan_and_facts_after_repeated_contention(runtime, tmp_path):
    import json
    from contextlib import contextmanager
    from open_trader.prediction_runtime import PredictionRuntime
    from tests.test_lp_auto_refill_contract import RefillPublic

    cache = tmp_path / 'sdk-cache.sqlite3'
    class Public(RefillPublic):
        catalog_calls = 0
        probe_calls = 0
        def dependency(self):
            with sqlite3.connect(cache, timeout=0) as connection:
                connection.execute('BEGIN IMMEDIATE')
        def list_current_rewards(self, *, sponsored=False):
            self.catalog_calls += 1
            rows = super().list_current_rewards(sponsored=sponsored)
            public = self
            class Pages:
                def iter_items(self):
                    public.dependency()
                    yield from rows
                def first_page(self):
                    from types import SimpleNamespace
                    public.probe_calls += 1
                    public.dependency()
                    return SimpleNamespace(items=rows, has_more=False)
            return Pages()
    public = Public(runtime.clock)
    store, adapter, account, lp, execution = runtime(public_client=public)
    @contextmanager
    def histories(request, **kwargs):
        from types import SimpleNamespace
        body = json.loads(request.data)
        yield SimpleNamespace(read=lambda: json.dumps({'history': {
            t: [dict(t=body['start_ts'], p='0.40'), dict(t=body['end_ts'], p='0.401')]
            for t in body['markets']}}).encode())
    adapter._urlopen_fn = histories
    store.lp_competitiveness_upsert((_refill_identity(i)[1], Decimal('2.5'), runtime.clock[0]) for i in range(1, 7))
    waiting = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    history_ready, facts_ready = threading.Event(), threading.Event()
    waits = []
    def history_wait(stop, seconds):
        waits.append(seconds)
        if lp.preparation_snapshot().get('stage') == 'complete':
            history_ready.set()
            assert stop.wait(5), 'Independent history shutdown watchdog'
            return True
        index = len(waits) - 1
        assert index < 2, (waits, lp.preparation_snapshot())
        waiting[index].set()
        assert release[index].wait(5), 'Independent retry scheduling watchdog'
        runtime.clock[0] += timedelta(seconds=seconds)
        return stop.is_set()
    runner = PredictionRuntime(data_dir=tmp_path, prediction_config_path=tmp_path / 'unused.json',
                               dashboard_url='http://127.0.0.1/', enable_n_leg_background=False,
                               history_clock=lambda: runtime.clock[0], history_wait=history_wait)
    runner.lp, runner.execution = lp, execution
    class ScanWake(threading.Event):
        def wait(self, timeout=None):
            if lp._candidate_qualification_facts:
                facts_ready.set()
            return super().wait(timeout)
    runner._lp_candidate_refresh_requested = ScanWake()
    holder = sqlite3.connect(cache)
    holder.execute('CREATE TABLE cache(value)')
    holder.commit()
    holder.execute('BEGIN IMMEDIATE')
    try:
        runner._start_candidate_scan_monitor()
        runner._start_history_monitor(data_only=True)
        assert waiting[0].wait(5)
        assert lp.preparation_snapshot()['state'] == 'waiting_retry'
        assert public.catalog_calls == 1
        assert not lp._candidate_qualification_facts
        release[0].set()  # First scheduled probe still encounters the lock.
        assert waiting[1].wait(5)
        assert public.probe_calls == 1
        assert lp.preparation_snapshot()['paused'] is False
        holder.rollback()
        release[1].set()  # Normal scheduling, no manual recovery or refresh.
        assert history_ready.wait(5), lp.preparation_snapshot()
        assert facts_ready.wait(5), lp.candidate_snapshot()
        assert lp.preparation_snapshot()['last_success_at'] is not None
        assert len(lp._candidate_qualification_facts) == 6
        assert public.probe_calls == 2
        assert public.catalog_calls == 5  # Initial failure, two probes, native/sponsored full read.
        assert waits[:2] == [60, 60]
        assert account.position_reads >= 1
        assert account.posts == account.cancels == []
    finally:
        holder.rollback()
        holder.close()
        runner._history_stop_event.set()
        runner._reward_stop_event.set()
        for gate in release:
            gate.set()
        runner._lp_candidate_refresh_requested.set()
        for thread in (runner._history_thread, runner._candidate_scan_thread):
            if thread:
                thread.join(5)
                assert not thread.is_alive()


def test_history_monitor_retries_busy_when_failure_writeback_is_also_locked(tmp_path, monkeypatch):
    import open_trader.prediction_arbitrage_store as store_module
    from open_trader.prediction_runtime import PredictionRuntime
    monkeypatch.setattr(store_module, '_BUSY_TIMEOUT_MS', 0)
    clock = [T]
    store = PredictionArbitrageStore(tmp_path / 'data')
    store.lp_save_preparation(dict(generation=1, state='ready', paused=False))
    exchange = Exchange(clock)
    service = PolymarketLPService(store, exchange, clock=lambda: clock[0])
    waiting, release, success = threading.Event(), threading.Event(), threading.Event()
    waits = []
    def wait(stop, seconds):
        waits.append(seconds)
        if service.preparation_snapshot().get('stage') == 'complete':
            success.set()
            assert stop.wait(5), 'Independent shutdown watchdog'
            return True
        waiting.set()
        assert release.wait(5), 'Independent writeback retry watchdog'
        clock[0] += timedelta(seconds=seconds)
        return stop.is_set()
    runner = PredictionRuntime(data_dir=tmp_path, prediction_config_path=tmp_path / 'unused.json',
                               dashboard_url='http://127.0.0.1/', enable_n_leg_background=False,
                               history_clock=lambda: clock[0], history_wait=wait)
    runner.lp = service
    holder = sqlite3.connect(store.path)
    holder.execute('BEGIN IMMEDIATE')
    try:
        runner._start_history_monitor(data_only=True)
        assert waiting.wait(5)
        assert waits == [60]
        assert exchange.calls == 0
        assert store.lp_preparation() == dict(generation=1, state='ready', paused=False)
        holder.rollback()
        release.set()
        assert success.wait(5)
        assert exchange.calls == 1
        assert store.lp_price_history_summary('healthy', 'token-healthy', now=clock[0])['state'] == 'known'
    finally:
        holder.rollback()
        holder.close()
        runner._history_stop_event.set()
        release.set()
        if runner._history_thread:
            runner._history_thread.join(5)
            assert not runner._history_thread.is_alive()


@pytest.mark.parametrize('recover', [False, True])
@pytest.mark.parametrize('boundary', ['begin-write', 'items-read'])
def test_begin_preparation_write_failure_cannot_pause_recovered_generation(tmp_path, monkeypatch, recover, boundary):
    """A real SQLite statement failure at the initial store write stays in its attempt."""
    store = PredictionArbitrageStore(tmp_path / 'data')
    store.lp_save_preparation(dict(generation=1, state='waiting_retry', paused=False, next_retry_at=None))
    exchange = Exchange([T])
    service = PolymarketLPService(store, exchange, clock=lambda: T)
    entered, release = threading.Event(), threading.Event()
    writer = store.lp_save_preparation
    def fail_after_release():
        entered.set()
        assert release.wait(5), 'Independent initial-store failure watchdog'
        # Inject an actual statement error at the selected store boundary.
        # This identifies the exception window, not a production writer.
        with store._transaction() as connection:
            connection.execute('SELECT * FROM missing_private_fixture_table')
    def delayed_writer(payload, *, expected_generation=None):
        if payload.get('state') == 'preparing':
            assert expected_generation == 1
            fail_after_release()
        return writer(payload, expected_generation=expected_generation)
    if boundary == 'begin-write':
        monkeypatch.setattr(store, 'lp_save_preparation', delayed_writer)
    else:
        monkeypatch.setattr(store, 'lp_preparation_items', fail_after_release)
    results, errors = [], []
    def refresh():
        try:
            results.append(service.refresh_price_history())
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=refresh)
    worker.start()
    try:
        assert entered.wait(5)
        if recover:
            paused = {**store.lp_preparation(), 'state': 'paused', 'paused': True,
                      'last_error': 'OperationalError'}
            writer(paused, expected_generation=1)
            replacement = PolymarketLPService(store, Exchange([T]), clock=lambda: T)
            assert replacement.recover_preparation(scope='global', expected_generation=1)['generation'] == 2
            recovered = store.lp_preparation()
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert errors == []
        assert len(results) == 1
        assert exchange.calls == 0
        if recover:
            assert store.lp_preparation() == recovered
            assert recovered['paused'] is False
            assert recovered['state'] == 'ready'
        else:
            assert store.lp_preparation()['generation'] == 1
            assert store.lp_preparation()['paused'] is True
            assert store.lp_preparation()['last_sqlite_errorcode'] == sqlite3.SQLITE_ERROR
        assert 'missing_private_fixture_table' not in str(results)
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()


@pytest.mark.parametrize('entry', ['factory', 'event-numeric', 'event-direct'])
@pytest.mark.parametrize('fault', ['busy', 'locked', 'unknown', 'auth', 'schema', 'integrity', 'wrapped-busy'])
def test_real_metadata_adapter_keeps_database_chain_for_global_policy(runtime, tmp_path, entry, fault):
    """Factory and both event readers preserve real SQLite facts through LP."""
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace
    from tests.test_lp_auto_refill_contract import RefillPublic
    from polymarket.models.gamma.market import MarketEvent

    uri = f'file:{tmp_path}/metadata-dependency.sqlite3?cache=shared' if fault == 'locked' else str(tmp_path / 'metadata-dependency.sqlite3')
    holder = sqlite3.connect(uri, uri=fault == 'locked')
    try:
        holder.execute('CREATE TABLE cache(value UNIQUE)')
        holder.execute('INSERT INTO cache VALUES (1)')
        holder.commit()
        if fault in {'busy', 'wrapped-busy', 'locked'}:
            holder.execute('BEGIN IMMEDIATE')
            if fault == 'locked':
                holder.execute('UPDATE cache SET value=2')
        captured_errors, metadata_results = [], []
        def dependency():
            connection = sqlite3.connect(uri, uri=fault == 'locked', timeout=0)
            try:
                if fault == 'unknown':
                    raise sqlite3.OperationalError('private unknown database detail')
                if fault in {'busy', 'wrapped-busy'}:
                    connection.execute('BEGIN IMMEDIATE')
                elif fault == 'locked':
                    connection.execute('SELECT * FROM cache').fetchall()
                elif fault == 'schema':
                    connection.execute('SELECT * FROM missing_private_fixture_table')
                elif fault == 'integrity':
                    connection.execute('INSERT INTO cache VALUES (1)')
                else:
                    connection.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
                    connection.execute('SELECT * FROM cache')
                raise AssertionError('SQLite fault did not occur')
            except sqlite3.Error as exc:
                captured_errors.append(exc)
                if fault == 'wrapped-busy':
                    raise RuntimeError('private adapter wrapper detail') from exc
                raise
            finally:
                connection.rollback()
                connection.close()
        condition = _refill_identity(1)[1]
        conditions = tuple(_refill_identity(i)[1] for i in range(1, 7))
        event_id = '123' if entry == 'event-numeric' else 'fixture-event'
        class Public(RefillPublic):
            def list_markets(self, **kwargs):
                rows = super().list_markets(**kwargs)
                return [row.model_copy(update={'events': (MarketEvent(id=event_id),)})
                        if row.condition_id == condition and entry != 'factory' else row for row in rows]
            def list_events(self, **kwargs):
                dependency()
            def get_event(self, **kwargs):
                dependency()
        public = Public(runtime.clock)
        store, adapter, account, lp, _ = runtime(public_client=public)
        factory_calls = []
        def factory():
            factory_calls.append(1)
            # Catalog succeeds through the SDK; the subsequent metadata factory fails.
            if entry == 'factory' and len(factory_calls) > 1:
                dependency()
            return public
        adapter._public_client_factory = factory
        metadata_reader = adapter.lp_market_metadata_batch
        def record_metadata(*args, **kwargs):
            result = metadata_reader(*args, **kwargs)
            metadata_results.append(result)
            return result
        adapter.lp_market_metadata_batch = record_metadata
        @contextmanager
        def histories(request, **kwargs):
            body = json.loads(request.data)
            yield SimpleNamespace(read=lambda: json.dumps({'history': {
                token: [dict(t=body['start_ts'], p='0.40'), dict(t=body['end_ts'], p='0.401')]
                for token in body['markets']}}).encode())
        adapter._urlopen_fn = histories
        result = lp.refresh_price_history()
        preparation = result['preparation']
        transient = fault in {'busy', 'locked', 'wrapped-busy'}
        assert preparation['paused'] is (not transient)
        assert preparation['state'] == ('waiting_retry' if transient else 'paused')
        assert preparation['last_error_category'] == ('transient' if transient else 'operator_attention')
        assert captured_errors and len(metadata_results) == 1
        error = captured_errors[0]
        assert preparation['last_sqlite_errorcode'] == getattr(error, 'sqlite_errorcode', None)
        assert preparation['last_sqlite_errorname'] == getattr(error, 'sqlite_errorname', None)
        expected_chain = (['RuntimeError'] if fault == 'wrapped-busy' else []) + [type(error).__name__]
        assert preparation['last_error_chain'][:len(expected_chain)] == expected_chain
        metadata = metadata_results[0]
        failed = conditions if entry == 'factory' else (condition,)
        assert set(metadata['failed_ids']) == set(failed)
        for cid in failed:
            assert list(metadata['failure_facts'][cid]['error_chain']) == expected_chain
            assert metadata['failure_facts'][cid].get('sqlite_errorcode') == getattr(error, 'sqlite_errorcode', None)
        if entry != 'factory':
            assert metadata['state'] == 'partial'
            assert set(metadata['markets']) == set(conditions)
            assert set(metadata['failed_ids']).isdisjoint(set(conditions[1:]))
        assert store.lp_preparation_items() == []
        assert account.posts == account.cancels == []
        assert 'private' not in str(result) + str(metadata)
    finally:
        try:
            holder.rollback()
        finally:
            holder.close()


@pytest.mark.parametrize('fault', ['busy', 'locked'])
def test_metadata_setup_failure_closes_holder_and_releases_lock(runtime, tmp_path, monkeypatch, fault):
    connect = sqlite3.connect
    holders = []
    setup_error = RuntimeError('controlled fixture setup failure')
    uri = f'file:{tmp_path}/metadata-dependency.sqlite3?cache=shared' if fault == 'locked' else str(tmp_path / 'metadata-dependency.sqlite3')

    def track_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        holders.append(connection)
        return connection

    def fail_setup(**kwargs):
        probe = connect(uri, uri=fault == 'locked', timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError) as caught:
                probe.execute('BEGIN IMMEDIATE')
            assert caught.value.sqlite_errorcode in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED_SHAREDCACHE}
        finally:
            probe.close()
        raise setup_error

    fail_setup.clock = runtime.clock
    monkeypatch.setattr(sqlite3, 'connect', track_connect)
    try:
        with pytest.raises(RuntimeError) as caught:
            test_real_metadata_adapter_keeps_database_chain_for_global_policy(fail_setup, tmp_path, 'factory', fault)
        assert caught.value is setup_error
        assert len(holders) == 1
        try:
            holders[0].execute('SELECT 1')
            closed = False
        except sqlite3.ProgrammingError:
            closed = True
        probe = connect(uri, uri=fault == 'locked', timeout=0)
        try:
            try:
                probe.execute('BEGIN IMMEDIATE')
                released = True
            except sqlite3.OperationalError:
                released = False
        finally:
            probe.rollback()
            probe.close()
        assert (closed, released) == (True, True)
    finally:
        # The negative baseline must also reclaim the leaked connection.
        for holder in holders:
            holder.close()


def test_global_recovery_before_begin_does_not_reassign_old_attempt(tmp_path, monkeypatch):
    store = PredictionArbitrageStore(tmp_path / 'data')
    store.lp_save_preparation(dict(generation=1, state='waiting_retry', paused=False, next_retry_at=None))
    exchange = Exchange([T])
    service = PolymarketLPService(store, exchange, clock=lambda: T)
    entered, release = threading.Event(), threading.Event()
    reader = store.lp_preparation_items
    def delayed_items():
        entered.set()
        assert release.wait(5), 'Independent before-begin recovery watchdog'
        return reader()
    monkeypatch.setattr(store, 'lp_preparation_items', delayed_items)
    results, errors = [], []
    def refresh():
        try:
            results.append(service.refresh_price_history())
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=refresh)
    worker.start()
    try:
        assert entered.wait(5)
        paused = {**store.lp_preparation(), 'state': 'paused', 'paused': True, 'last_error': 'OperationalError'}
        store.lp_save_preparation(paused, expected_generation=1)
        replacement = PolymarketLPService(store, Exchange([T]), clock=lambda: T)
        assert replacement.recover_preparation(scope='global', expected_generation=1)['generation'] == 2
        recovered = store.lp_preparation()
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert errors == []
        assert results[0]['preparation_outcome'] == 'superseded'
        assert results[0]['reason'] == 'preparation_generation_changed'
        assert store.lp_preparation() == recovered
        assert exchange.calls == 0
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()
