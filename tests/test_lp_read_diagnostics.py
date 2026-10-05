"""Offline #248 diagnostics: real workers/locks, no production I/O."""
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event, Lock, current_thread

import pytest

from open_trader import polymarket_lp, polymarket_trading
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_polymarket_lp import _SDKAccountClient


def wait_read_logs():
    # Wait for the actual consumer, never flush by hand or release business I/O.
    queue = polymarket_trading._lp_read_log_queue
    with queue.all_tasks_done:
        assert queue.all_tasks_done.wait_for(lambda: queue.unfinished_tasks == 0, timeout=2), 'independent diagnostic watchdog'


@pytest.fixture(autouse=True)
def diagnostic_consumer_cleanup(monkeypatch):
    wait_read_logs()
    yield
    wait_read_logs()


@pytest.mark.parametrize('launch', ['after_timeout', 'concurrent'])
def test_timed_out_account_workers_still_occupy_slots_and_report_actual_end(tmp_path, monkeypatch, caplog, launch):
    caplog.set_level(logging.INFO)
    entered, waiting, release = Event(), Event(), Event()
    client = _SDKAccountClient(datetime.now(UTC))
    adapter = PolymarketTradingClient(TradingConfig('offline-wallet', 'offline-funder'), client)
    lp = PolymarketLPService(PredictionArbitrageStore(tmp_path), adapter)
    futures = []
    worker_threads = []
    balance = client.get_balance_allowance

    def blocked(**kwargs):
        worker_threads.append(current_thread())
        entered.set()
        assert release.wait(5), 'independent wall-clock watchdog'
        return balance(**kwargs)

    class ObservedLock:
        def __init__(self):
            self.lock = Lock()
        def acquire(self):
            if self.lock.locked():
                waiting.set()
            return self.lock.acquire()
        def release(self):
            self.lock.release()
        def __enter__(self):
            self.acquire()
        def __exit__(self, *args):
            self.release()

    class ObservedFuture(Future):
        def __init__(self):
            super().__init__()
            self.index = len(futures)
            futures.append(self)
        def result(self, timeout=None):
            if not self.done() and timeout == .01:
                assert (entered if self.index == 0 else waiting).wait(2)
                if launch == 'concurrent' and self.index == 0:
                    assert waiting.wait(2)
            return super().result(timeout)

    monkeypatch.setattr(client, 'get_balance_allowance', blocked)
    monkeypatch.setattr(adapter, '_lp_account_read_lock', ObservedLock())
    monkeypatch.setattr(polymarket_lp, 'Future', ObservedFuture)
    lp._market_read_timeout = .01
    try:
        if launch == 'concurrent':
            with ThreadPoolExecutor(2) as callers:
                first = callers.submit(lp._market_read, {'condition_id': 'secret-token-1'}, adapter._account_read_facts)
                assert entered.wait(2)
                second = callers.submit(lp._market_read, {'condition_id': 'secret-token-2'}, adapter._account_read_facts)
                for caller in (first, second):
                    with pytest.raises(ValueError, match='^market_read_timeout$'):
                        caller.result(timeout=2)
        else:
            for key in ('secret-token-1', 'secret-token-2'):
                with pytest.raises(ValueError, match='^market_read_timeout$'):
                    lp._market_read({'condition_id': key}, adapter._account_read_facts)
        assert len([f for f in lp._market_reads.values() if not f.done()]) == 2
        for _ in range(50):
            with pytest.raises(ValueError, match='^market_read_capacity$'):
                lp._market_read({'condition_id': 'secret-token-3'}, adapter._account_read_facts)
        wait_read_logs()
        assert not release.is_set()
        assert len([future for future in lp._market_reads.values() if not future.done()]) == 2
        messages = [r.getMessage() for r in caplog.records]
        refusals = [m for m in messages if m.startswith('lp_market_read_diagnostic') and 'reason=market_read_capacity' in m]
        assert len(refusals) == 1
        assert 'inflight=2' in refusals[0]
        assert 'account_balance' in refusals[0] and 'account_lock_wait' in refusals[0]
        assert sum(m.startswith('lp_market_read_stack') for m in messages) == 2
        consumer_id = polymarket_trading._lp_read_log_thread.ident
        for future in futures:
            assert future.lp_diagnostic['thread'] != consumer_id
            assert any(f'task_id={future.lp_diagnostic["task_id"]} thread={future.lp_diagnostic["thread"]}' in m
                       for m in messages if m.startswith('lp_market_read_stack'))
        assert 'secret-token' not in caplog.text
        assert 'offline-wallet' not in caplog.text
        assert not any('assert release.wait' in m or 'locals=' in m for m in messages)
    finally:
        release.set()
        try:
            for future in futures:
                future.result(timeout=2)
        finally:
            for worker in worker_threads:
                worker.join(timeout=2)
                assert not worker.is_alive()
    wait_read_logs()
    completions = [r.getMessage() for r in caplog.records if r.getMessage().startswith('lp_read_task_end')]
    assert len(completions) == 2
    for future in futures:
        assert any(f'task_id={future.lp_diagnostic["task_id"]}' in m and 'caller_timed_out=True' in m
                   and 'started_at=' in m and 'ended_at=' in m for m in completions)
    lp._market_read_timeout = 2
    result = lp._market_read({'condition_id': 'secret-token-4'}, adapter._account_read_facts)
    assert result[0] > 0
    assert not [f for f in lp._market_reads.values() if not f.done()]
    assert lp.store.lp_sessions() == []


def test_stage_diagnostic_failure_preserves_exception_and_lock_cleanup(monkeypatch):
    from types import SimpleNamespace
    from threading import Thread
    clock = [0.0]
    lock = Lock()
    class TimedLock:
        def __enter__(self):
            lock.acquire()
            clock[0] += 61
        def __exit__(self, *args):
            lock.release()
    def failed(*args, **kwargs):
        raise RuntimeError('diagnostics failed')
    monkeypatch.setattr(polymarket_trading, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(polymarket_trading.logger, 'warning', failed)
    monkeypatch.setattr(polymarket_trading.logger, 'info', failed)
    original = ValueError('original business failure')
    with pytest.raises(ValueError) as caught:
        with polymarket_trading._lp_read_lock(TimedLock(), 'account_lock_wait'):
            raise original
    assert caught.value is original
    acquired = []
    def observer():
        acquired.append(lock.acquire(blocking=False))
        if acquired[-1]:
            lock.release()
    worker = Thread(target=observer)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert acquired == [True]


@pytest.mark.parametrize('business_error', [False, True])
def test_slow_wait_log_runs_after_real_lock_release_and_keeps_wait_time(monkeypatch, business_error):
    from types import SimpleNamespace
    lock = Lock()
    lock.acquire()
    initial_lock_held = True
    waiting, in_body, logging_entered, logging_release = Event(), Event(), Event(), Event()
    clock, logs, source_threads = [0.0], [], []
    original = ValueError('original business failure')
    class ObservedLock:
        def __enter__(self):
            waiting.set()
            lock.acquire()
        def __exit__(self, *args):
            lock.release()
    def slow_log(*args):
        logs.append(args)
        logging_entered.set()
        assert logging_release.wait(5), 'independent logging watchdog'
    def business():
        source_threads.append(current_thread().ident)
        with polymarket_trading._lp_read_lock(ObservedLock(), 'account_lock_wait'):
            in_body.set()
            clock[0] += 7
            if business_error:
                raise original
            return 'healthy'
    monkeypatch.setattr(polymarket_trading, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(polymarket_trading.logger, 'warning', slow_log)
    with ThreadPoolExecutor(1) as workers:
        result = workers.submit(business)
        try:
            assert waiting.wait(2)
            clock[0] = 61
            lock.release()
            initial_lock_held = False
            assert logging_entered.wait(2)
            assert in_body.is_set(), 'waiting diagnostics must not delay the lock body'
            assert lock.acquire(blocking=False), 'blocked logging must not retain the lock'
            lock.release()
            assert logs[0][1:3] == ('account_lock_wait', 61.0)
            assert logs[0][3] == source_threads[0]
            assert logs[0][3] != polymarket_trading._lp_read_log_thread.ident
        finally:
            logging_release.set()
            if initial_lock_held:
                lock.release()
        if business_error:
            with pytest.raises(ValueError) as caught:
                result.result(timeout=2)
            assert caught.value is original
        else:
            assert result.result(timeout=2) == 'healthy'


def test_account_pagination_counts_logical_reads_http_and_records_separately(monkeypatch, caplog):
    import httpx
    from types import SimpleNamespace
    caplog.set_level(logging.INFO, logger=polymarket_trading.__name__)
    client = _SDKAccountClient(datetime.now(UTC))
    http = httpx.Client(base_url='https://offline.invalid', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={}, request=request)))
    client._ctx = SimpleNamespace(secure_clob=SimpleNamespace(_client=http), data=SimpleNamespace(_client=http))
    def pages():
        for n in (1, 2):
            http.get('/data/orders', params={'cursor': 'secret-cursor', 'account': 'secret-wallet'})
            yield from [client.open_order] * n
    monkeypatch.setattr(client, 'list_open_orders', pages)
    adapter = PolymarketTradingClient(TradingConfig('offline-wallet', 'offline-funder'), client)
    try:
        with polymarket_trading._lp_read_task('test_read') as task:
            task['diagnose'] = True
            assert len(adapter._account_read_facts()[2]) == 3
        assert task['counts']['account_orders'] == dict(logical_reads=1, http_requests=2, http_responses=2, http_pages=2, records=3)
        assert task['counts']['account_trades'] == dict(logical_reads=1, records=1)
        assert not http.event_hooks['response'] and not http.event_hooks['request']
        assert 'secret-' not in caplog.text
        assert 'offline.invalid' not in caplog.text
    finally:
        http.close()


def test_database_diagnostics_separate_preparation_begin_body_commit_and_counts(tmp_path, monkeypatch, caplog):
    import sqlite3
    from decimal import Decimal
    from open_trader import prediction_arbitrage_store as store_module
    store = PredictionArbitrageStore(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(store_module, 'monotonic', lambda: clock[0])
    class TimedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith('BEGIN'):
                clock[0] += 2
            elif sql == 'COMMIT':
                clock[0] += 3
            return super().execute(sql, *args)
        def executemany(self, sql, *args):
            clock[0] += 4
            return super().executemany(sql, *args)
    def connection():
        clock[0] += 1
        conn = sqlite3.connect(store.path, isolation_level=None, factory=TimedConnection)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(store, '_connection', connection)
    def entries():
        clock[0] += 2
        yield ('secret-condition-1', Decimal('1'), datetime.now(UTC))
        yield ('secret-condition-2', Decimal('2'), datetime.now(UTC))
    assert store.lp_competitiveness_upsert(entries()) == 2
    message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith('prediction_store_transaction_timing'))
    for field in ('prepare_seconds=3.000', 'wait_seconds=2.000', 'body_seconds=4.000', 'commit_seconds=3.000',
                  "'input_rows': 2", 'sqlite_changes=2'):
        assert field in message
    assert 'secret-condition' not in caplog.text
    assert store.lp_competitiveness_count() == 2


def test_database_logging_failure_never_changes_commit_or_rollback(tmp_path, monkeypatch):
    from decimal import Decimal
    from open_trader import prediction_arbitrage_store as store_module
    store = PredictionArbitrageStore(tmp_path)
    def failed(*args, **kwargs):
        raise RuntimeError('diagnostics failed')
    monkeypatch.setattr(store_module.logger, 'warning', failed)
    monkeypatch.setattr(store_module, '_SLOW_TRANSACTION_SECONDS', 0)
    assert store.lp_competitiveness_upsert([('offline-condition', Decimal('1'), datetime.now(UTC))]) == 1
    original = ValueError('original transaction failure')
    with pytest.raises(ValueError) as caught:
        with store._transaction() as connection:
            connection.execute('DELETE FROM lp_market_competitiveness')
            raise original
    assert caught.value is original
    assert store.lp_competitiveness_count() == 1


def test_public_worker_is_linked_to_parent_and_keeps_late_cleanup(monkeypatch, caplog):
    from tests.test_polymarket_lp import _SDKPublicClient
    caplog.set_level(logging.INFO, logger=polymarket_trading.__name__)
    entered, release = Event(), Event()
    worker_threads = []
    now = datetime.now(UTC)
    public = _SDKPublicClient(now)
    closed = []
    monkeypatch.setattr(public, 'close', lambda: closed.append(True))
    market = public.get_market
    def blocked(**kwargs):
        worker_threads.append(current_thread())
        entered.set()
        assert release.wait(5)
        return market(**kwargs)
    monkeypatch.setattr(public, 'get_market', blocked)
    adapter = PolymarketTradingClient(TradingConfig('offline-wallet', 'offline-funder'),
        _SDKAccountClient(now), public_client_factory=lambda: public)
    adapter._lp_public_read_timeout = .01
    class ObservedFuture(Future):
        def result(self, timeout=None):
            if timeout == .01:
                assert entered.wait(2)
            return super().result(timeout)
    monkeypatch.setattr(polymarket_trading, 'Future', ObservedFuture)
    future = None
    try:
        with polymarket_trading._lp_read_task('test_parent') as parent:
            result = adapter._read_lp_public_snapshot('secret-key', 'market-1', '0x' + '1' * 64)
            assert result[3]['error_type'] == 'TimeoutError'
            future = adapter._lp_public_reads['secret-key']
            assert not future.done()
            assert future.lp_diagnostic['parent_task_id'] == parent['task_id']
            wait_read_logs()
            assert 'stage=market' in caplog.text or "'stage': 'market'" in caplog.text
            assert 'lp_market_read_stack' in caplog.text
            assert 'secret-key' not in caplog.text
    finally:
        release.set()
        try:
            if future is not None:
                future.result(timeout=2)
        finally:
            for worker in worker_threads:
                worker.join(timeout=2)
                assert not worker.is_alive()
            adapter.close()
    wait_read_logs()
    assert closed == [True]
    assert 'caller_timed_out=True' in caplog.text


def test_account_registration_reports_scans_and_cached_actions_without_identity(tmp_path, monkeypatch, caplog):
    from types import SimpleNamespace
    from open_trader import prediction_arbitrage_store as store_module
    from tests.test_lp_account_coverage_ledger import NOW, WALLET, seed, snapshot, read_pool
    store = PredictionArbitrageStore(tmp_path)
    original = seed(store)
    lp = PolymarketLPService(store, SimpleNamespace(config=SimpleNamespace(wallet_address=WALLET)), clock=lambda: NOW)
    monkeypatch.setattr(store_module, '_SLOW_TRANSACTION_SECONDS', 0)
    assert lp.register_account_snapshot(snapshot())['state'] == 'registered'
    message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith('prediction_store_transaction_timing'))
    for field in ("'session_scan_calls': 4", "'session_rows_scanned': 4", "'actions_query_calls': 1",
                  "'publish_input_buys': 0", 'apply_lock_wait_seconds', 'prepare_seconds=', 'commit_seconds='):
        assert field in message
    assert WALLET not in caplog.text
    assert read_pool(store)['events'] == original['events']
    assert store.lp_session('session')['submit_status'] == 'unknown'


def test_missing_stack_and_slow_diagnostic_logging_leave_scheduler_lock_free(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    entered, release, logging_entered, logging_release = Event(), Event(), Event(), Event()
    lp = PolymarketLPService(PredictionArbitrageStore(tmp_path), object())
    lp._market_read_timeout = .01
    futures = []
    class ObservedFuture(Future):
        def __init__(self):
            super().__init__()
            futures.append(self)
        def result(self, timeout=None):
            if timeout == .01:
                assert entered.wait(2)
            return super().result(timeout)
    def blocked():
        entered.set()
        assert release.wait(5)
        return 'late-read'
    def slow_log(*args, **kwargs):
        logging_entered.set()
        assert logging_release.wait(5)
    def missing_frames():
        raise RuntimeError('capture unavailable')
    monkeypatch.setattr(polymarket_lp, 'Future', ObservedFuture)
    monkeypatch.setattr(polymarket_trading.logger, 'warning', slow_log)
    monkeypatch.setattr(polymarket_trading.sys, '_current_frames', missing_frames)
    with ThreadPoolExecutor(1) as workers:
        caller = workers.submit(lp._market_read, {'condition_id': 'secret'}, blocked)
        try:
            assert logging_entered.wait(2)
            assert lp._market_reads_lock.acquire(blocking=False)
            lp._market_reads_lock.release()
            assert not futures[0].done()
        finally:
            logging_release.set()
            release.set()
        with pytest.raises(ValueError, match='^market_read_timeout$'):
            caller.result(timeout=2)
        assert futures[0].result(timeout=2) == 'late-read'


def test_database_counter_capture_failure_is_unknown_and_cannot_prevent_commit(tmp_path, monkeypatch, caplog):
    import sqlite3
    from open_trader import prediction_arbitrage_store as store_module
    store = PredictionArbitrageStore(tmp_path)
    class UnobservableChanges(sqlite3.Connection):
        @property
        def total_changes(self):
            raise RuntimeError('counter unavailable')
    monkeypatch.setattr(store, '_connection', lambda: sqlite3.connect(store.path, isolation_level=None, factory=UnobservableChanges))
    monkeypatch.setattr(store_module, '_SLOW_TRANSACTION_SECONDS', 0)
    with store._transaction(diagnostics={'account_id': 'secret-wallet', 'input_rows': 1}) as connection:
        connection.execute("INSERT INTO lp_market_competitiveness VALUES ('offline', '1', '2026-10-05T00:00:00Z')")
    assert 'sqlite_changes=unknown' in caplog.text
    assert 'secret-wallet' not in caplog.text
    assert store.lp_competitiveness_count() == 1


def test_account_future_join_reports_wait_separately_from_api_work(monkeypatch):
    entered, joined, release = Event(), Event(), Event()
    client = _SDKAccountClient(datetime.now(UTC))
    adapter = PolymarketTradingClient(TradingConfig('offline-wallet', 'offline-funder'), client)
    token = adapter.lp_account_round_begin()
    orders = client.list_open_orders
    def blocked():
        entered.set()
        assert release.wait(5)
        return orders()
    tasks = []
    class ObservedFuture(Future):
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)
    def join():
        with polymarket_trading._lp_read_task('test_join') as task:
            tasks.append(task)
            return adapter._lp_account_snapshot_for_round(token)
    monkeypatch.setattr(client, 'list_open_orders', blocked)
    monkeypatch.setattr(polymarket_trading, 'Future', ObservedFuture)
    with ThreadPoolExecutor(2) as workers:
        owner = workers.submit(adapter._lp_account_snapshot_for_round, token)
        try:
            assert entered.wait(2)
            waiter = workers.submit(join)
            assert joined.wait(2)
            assert tasks[0]['stage'] == 'account_future_wait'
        finally:
            release.set()
        assert owner.result(timeout=2) == waiter.result(timeout=2)
    adapter.lp_account_round_end(token)
    assert 'account_future_wait' in tasks[0]['timings']
    assert 'account_balance' not in tasks[0]['timings']


def test_rollback_failure_preserves_original_error_without_dumping_parameters(tmp_path, monkeypatch, caplog):
    import sqlite3
    store = PredictionArbitrageStore(tmp_path)
    class FailedRollback(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == 'ROLLBACK':
                raise sqlite3.OperationalError('secret-sql-parameter')
            return super().execute(sql, *args)
    monkeypatch.setattr(store, '_connection', lambda: sqlite3.connect(store.path, isolation_level=None, factory=FailedRollback))
    original = ValueError('secret-business-argument')
    with pytest.raises(ValueError) as caught:
        with store._transaction():
            raise original
    assert caught.value is original
    assert 'Rollback failed: OperationalError' in original.__notes__
    records = [r for r in caplog.records if 'prediction_store_rollback_failed' in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    assert 'secret-' not in caplog.text
    assert records[0].exc_info is None


@pytest.mark.parametrize('phase', ['install', 'remove'])
def test_http_diagnostic_hook_failure_does_not_change_account_result_or_cleanup(monkeypatch, phase):
    client = _SDKAccountClient(datetime.now(UTC))
    adapter = PolymarketTradingClient(TradingConfig('offline-wallet', 'offline-funder'), client)
    removed = []
    def install(*args, **kwargs):
        if phase == 'install':
            raise RuntimeError('diagnostic installation failed')
        def remove():
            removed.append(True)
            raise RuntimeError('diagnostic cleanup failed')
        return remove
    monkeypatch.setattr(polymarket_trading, '_install_lp_response_fact_hook', install)
    result = adapter._account_read_facts()
    assert result[0] == 100
    assert len(result[2]) == 1 and len(result[5]) == 1
    assert result[6] is True
    assert removed == ([True] * 4 if phase == 'remove' else [])
    assert adapter._lp_account_read_lock.acquire(blocking=False)
    adapter._lp_account_read_lock.release()


@pytest.mark.parametrize('caller', ['account', 'probe_rewards', 'probe_metadata', 'catalog'])
@pytest.mark.parametrize('append_then_fail', [False, True])
def test_partial_http_hook_installation_is_atomic_for_every_caller(caller, append_then_fail):
    from types import SimpleNamespace
    class FailedHooks(list):
        fail = True
        def append(self, hook):
            if not self.fail or append_then_fail:
                super().append(hook)
            if self.fail:
                raise RuntimeError('diagnostic request hook install failed')
    sentinel = lambda *args: None
    requests = FailedHooks([sentinel])
    responses = [sentinel]
    transport = SimpleNamespace(_client=SimpleNamespace(event_hooks={'request': requests, 'response': responses}))
    ctx = SimpleNamespace(secure_clob=transport, clob=transport, gamma=transport, data=transport)
    calls = []
    def request(path):
        calls.append(path)
        req = SimpleNamespace(url=SimpleNamespace(path=path))
        for hook in tuple(requests):
            hook(req)
        for hook in tuple(responses):
            hook(SimpleNamespace(request=req, status_code=200, headers={}))
    class Page:
        def __init__(self, path):
            self.path = path
        def first_page(self):
            request(self.path)
            return SimpleNamespace(items=(), has_more=False)
        def __iter__(self):
            yield self.first_page()
        def iter_items(self):
            self.first_page()
            return iter(())
    class Public:
        _ctx = ctx
        def list_current_rewards(self, **kwargs):
            return Page('/rewards/markets/current')
        def list_markets(self, **kwargs):
            return Page('/markets/keyset')
        def close(self):
            pass
    client = _SDKAccountClient(datetime.now(UTC))
    client._ctx = ctx
    orders = client.list_open_orders
    def open_orders():
        request('/data/orders')
        return orders()
    client.list_open_orders = open_orders
    adapter = PolymarketTradingClient(TradingConfig('offline-wallet', 'offline-funder'), client,
        public_client_factory=Public)
    def read():
        if caller == 'account':
            return adapter._account_read_facts()[0]
        if caller == 'catalog':
            return adapter.lp_reward_catalog()['state']
        return adapter.lp_preparation_probe(stage='metadata' if caller == 'probe_metadata' else 'rewards',
            condition_ids=['offline-condition'])['state']
    expected = 100 if caller == 'account' else 'known' if caller == 'catalog' else 'healthy'
    retained = []
    for _ in range(3):
        assert read() == expected, 'diagnostic failure must not skip the actual API'
        retained.append((len(requests) - 1, len(responses) - 1))
    assert calls, 'business API must still be called'
    requests.fail = False
    with polymarket_trading._lp_read_task('control') as task:
        assert read() == expected
    assert retained == [(0, 0)] * 3
    assert requests == [sentinel] and responses == [sentinel]
    if caller == 'account':
        assert task['counts']['account_orders']['http_pages'] == 1


@pytest.mark.parametrize('outcome', ['success', 'error'])
@pytest.mark.parametrize('log_kind', ['task_end', 'lock_wait'])
def test_finished_market_io_settles_futures_before_blocking_diagnostics(tmp_path, monkeypatch, outcome, log_kind):
    from types import SimpleNamespace
    from threading import current_thread, get_ident
    entered = [Event(), Event()]
    completed = [Event(), Event()]
    logging_entered = Event()
    release_io, release_logging, fresh_completed = Event(), Event(), Event()
    futures, threads, clocks = [], [], {}
    errors = [ValueError('original first'), ValueError('original second')]
    lp = PolymarketLPService(PredictionArbitrageStore(tmp_path), object())
    lp._market_read_timeout = .01
    class ObservedFuture(Future):
        def __init__(self):
            super().__init__()
            self.index = len(futures)
            futures.append(self)
        def result(self, timeout=None):
            if timeout == .01:
                assert (completed[self.index] if self.index < 2 else fresh_completed).wait(2), 'independent scheduling watchdog'
            return super().result(timeout)
    def slow_log(*args):
        target = (str(args[0]).startswith('lp_read_task_end') if log_kind == 'task_end' else
                  str(args[0]).startswith('lp_read_slow') and args[1] == 'account_lock_wait')
        if target:
            logging_entered.set()
            assert release_logging.wait(5), 'independent logging watchdog'
    class TimedLock:
        def __init__(self):
            self.lock = Lock()
        def __enter__(self):
            self.lock.acquire()
            clocks[get_ident()] = 61.0
        def __exit__(self, *args):
            self.lock.release()
    def read(index):
        threads.append(current_thread())
        def io():
            entered[index].set()
            assert release_io.wait(5)
            completed[index].set()
            if outcome == 'error':
                raise errors[index]
            return f'healthy-{index}'
        if log_kind == 'lock_wait':
            with polymarket_trading._lp_read_lock(TimedLock(), 'account_lock_wait'):
                return io()
        return io()
    def caller(index):
        try:
            return lp._market_read({'condition_id': str(index)}, lambda: read(index))
        except ValueError as exc:
            return exc
    monkeypatch.setattr(polymarket_lp, 'Future', ObservedFuture)
    monkeypatch.setattr(polymarket_trading.logger, 'info', slow_log)
    monkeypatch.setattr(polymarket_trading.logger, 'warning', slow_log)
    if log_kind == 'lock_wait':
        monkeypatch.setattr(polymarket_trading, 'time', SimpleNamespace(monotonic=lambda: clocks.get(get_ident(), 0.0)))
    with ThreadPoolExecutor(2) as callers:
        pending = [callers.submit(caller, index) for index in range(2)]
        try:
            assert all(event.wait(2) for event in entered)
            assert len([f for f in lp._market_reads.values() if not f.done()]) == 2
            with pytest.raises(ValueError, match='^market_read_capacity$'):
                lp._market_read({'condition_id': 'third'}, lambda: 'unused')
            release_io.set()
            assert logging_entered.wait(2)
            assert all(event.wait(2) for event in completed), 'independent I/O completion watchdog'
            results = [p.result(timeout=2) for p in pending]
            assert results == ([f'healthy-{i}' for i in range(2)] if outcome == 'success' else errors)
            if outcome == 'error':
                assert all(results[i] is errors[i] for i in range(2))
            assert all(f.done() for f in futures)
            assert not lp._market_read_retry, 'end logging must not create a timeout cooldown'
            def fresh():
                fresh_completed.set()
                return 'next-read'
            assert lp._market_read({'condition_id': 'fresh'}, fresh) == 'next-read'
        finally:
            release_io.set()
            release_logging.set()
            for thread in threads:
                thread.join(timeout=2)
                assert not thread.is_alive()


@pytest.mark.parametrize('outcome', ['success', 'error'])
def test_public_child_future_settles_after_real_cleanup_before_end_log(monkeypatch, outcome):
    from threading import current_thread
    from tests.test_polymarket_lp import _SDKPublicClient
    entered, io_completed, release_io, logging_entered, release_logging = (Event() for _ in range(5))
    closed, futures, threads = [], [], []
    now = datetime.now(UTC)
    public = _SDKPublicClient(now)
    book = public.get_order_book
    def blocked(**kwargs):
        threads.append(current_thread())
        entered.set()
        assert release_io.wait(5)
        io_completed.set()
        if outcome == 'error':
            raise ValueError('original public read failure')
        return book(**kwargs)
    def slow_log(*args):
        if str(args[0]).startswith('lp_read_task_end') and args[2] == 'public_snapshot':
            logging_entered.set()
            assert release_logging.wait(5)
    class ObservedFuture(Future):
        def __init__(self):
            super().__init__()
            futures.append(self)
        def result(self, timeout=None):
            if timeout == .01:
                assert logging_entered.wait(2)
            return super().result(timeout)
    monkeypatch.setattr(public, 'get_order_book', blocked)
    monkeypatch.setattr(public, 'close', lambda: closed.append(True))
    monkeypatch.setattr(polymarket_trading, 'Future', ObservedFuture)
    monkeypatch.setattr(polymarket_trading.logger, 'info', slow_log)
    adapter = PolymarketTradingClient(TradingConfig('offline-wallet', 'offline-funder'),
        _SDKAccountClient(now), public_client_factory=lambda: public)
    adapter._lp_public_read_timeout = .01
    def read():
        with polymarket_trading._lp_read_task('parent') as parent:
            result = adapter._read_lp_public_snapshot('offline', 'market-1', '0x' + '1' * 64)
            return result, parent['task_id']
    with ThreadPoolExecutor(1) as callers:
        caller = callers.submit(read)
        try:
            assert entered.wait(2)
            assert not futures[0].done() and adapter._lp_public_readers == 1
            futures[0].lp_diagnostic['diagnose'] = True
            adapter.close()
            assert closed == [], 'unfinished real I/O retains its client'
            release_io.set()
            assert logging_entered.wait(2) and io_completed.is_set()
            result, parent_id = caller.result(timeout=2)
            assert futures[0].lp_diagnostic['parent_task_id'] == parent_id
            assert futures[0].done() and adapter._lp_public_readers == 0
            assert closed == [True]
            if outcome == 'error':
                assert result[3]['error_type'] == 'ValueError'
            else:
                assert result[0] is not None and result[1] is not None and result[3] is None
        finally:
            release_io.set()
            release_logging.set()
            adapter.close()
            for thread in threads:
                thread.join(timeout=2)
                assert not thread.is_alive()


@pytest.mark.parametrize('outcome', ['success', 'error'])
def test_shared_account_future_delivers_to_joiner_before_owner_end_log(monkeypatch, outcome):
    from types import SimpleNamespace
    entered, joined, release_io, logging_entered, release_logging = (Event() for _ in range(5))
    clock, futures = [0.0], []
    client = _SDKAccountClient(datetime.now(UTC))
    def positions():
        entered.set()
        assert release_io.wait(5)
        clock[0] = 61
        if outcome == 'error':
            raise ValueError('original account I/O error')
        return []
    def slow_log(*args):
        if str(args[0]).startswith('lp_read_task_end'):
            logging_entered.set()
            assert release_logging.wait(5)
    class ObservedFuture(Future):
        def __init__(self):
            super().__init__()
            futures.append(self)
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)
    monkeypatch.setattr(client, 'list_positions', positions)
    monkeypatch.setattr(polymarket_trading, 'Future', ObservedFuture)
    monkeypatch.setattr(polymarket_trading, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(polymarket_trading.logger, 'info', slow_log)
    adapter = PolymarketTradingClient(TradingConfig('offline-wallet', 'offline-funder'), client)
    token = adapter.lp_account_round_begin()
    def read():
        try:
            return adapter._lp_account_snapshot_for_round(token)
        except Exception as exc:
            return exc
    with ThreadPoolExecutor(2) as callers:
        owner = callers.submit(read)
        try:
            assert entered.wait(2)
            assert not futures[0].done()
            joiner = callers.submit(read)
            assert joined.wait(2)
            release_io.set()
            assert logging_entered.wait(2)
            assert futures[0].done(), 'owner completion diagnostics must not retain the shared Future'
            joined_result = joiner.result(timeout=2)
            assert token.lock.acquire(blocking=False)
            token.lock.release()
            if outcome == 'error':
                assert joined_result is futures[0].exception(timeout=0)
            else:
                assert joined_result[0]['authenticated'] is True
        finally:
            release_io.set()
            release_logging.set()
        owner_result = owner.result(timeout=2)
        assert owner_result is joined_result if outcome == 'error' else owner_result == joined_result
    adapter.lp_account_round_end(token)


@pytest.mark.parametrize('outer_deferred', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'error'])
def test_nested_account_task_defers_lock_log_until_owning_future_completes(tmp_path, monkeypatch, outer_deferred, outcome):
    from types import SimpleNamespace
    entered, io_done, release_io, logging_entered, release_logging, joined = (Event() for _ in range(6))
    clock, shared, market, log_threads = [0.0], [], [], []
    lock = Lock()
    class TimedLock:
        def __enter__(self):
            lock.acquire()
            clock[0] = 61.0
        def __exit__(self, *args):
            lock.release()
    class SharedFuture(Future):
        def __init__(self):
            super().__init__()
            shared.append(self)
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)
    class MarketFuture(Future):
        def __init__(self):
            super().__init__()
            market.append(self)
        def result(self, timeout=None):
            if timeout == .01:
                assert logging_entered.wait(2), 'independent scheduling watchdog'
            return super().result(timeout)
    client = _SDKAccountClient(datetime.now(UTC))
    calls = []
    def positions():
        calls.append(True)
        if outer_deferred:
            log_threads.append(current_thread())
        entered.set()
        assert release_io.wait(5)
        io_done.set()
        if outcome == 'error':
            raise ValueError('original nested account I/O error')
        return []
    def slow_log(*args):
        if str(args[0]).startswith('lp_read_slow') and args[1] == 'account_lock_wait':
            logging_entered.set()
            assert release_logging.wait(5)
    monkeypatch.setattr(client, 'list_positions', positions)
    monkeypatch.setattr(polymarket_trading, 'Future', SharedFuture)
    monkeypatch.setattr(polymarket_trading, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(polymarket_trading.logger, 'warning', slow_log)
    monkeypatch.setattr(polymarket_lp, 'Future', MarketFuture)
    # Account orders trigger display metadata through the public snapshot entry.
    adapter = PolymarketTradingClient(TradingConfig('offline-wallet', 'offline-funder'), client,
        public_client_factory=lambda: SimpleNamespace(list_markets=lambda **kwargs: [], close=lambda: None))
    adapter._lp_account_read_lock = TimedLock()
    token = adapter.lp_account_round_begin()
    lp = PolymarketLPService(PredictionArbitrageStore(tmp_path), adapter)
    lp._market_read_timeout = .01
    def snapshot():
        # Public entry creates the real outer account_snapshot task.
        return adapter.lp_account_snapshot(account_round=token)
    def read_owner():
        try:
            return lp._market_read({'condition_id': 'offline'}, snapshot) if outer_deferred else snapshot()
        except Exception as exc:
            return exc
    def read_joiner():
        try:
            return snapshot()
        except Exception as exc:
            return exc
    with ThreadPoolExecutor(2) as callers:
        owner = callers.submit(read_owner)
        try:
            assert entered.wait(2)
            assert not shared[0].done()
            if outer_deferred:
                assert not market[0].done()
            joiner = callers.submit(read_joiner)
            assert joined.wait(2), "joiner must attach before an error clears the shared slot"
            release_io.set()
            assert logging_entered.wait(2)
            assert io_done.is_set()
            assert lock.acquire(blocking=False), 'actual account lock is already released'
            lock.release()
            assert shared[0].done(), 'nested task must respect the account owner deferred list'
            if outer_deferred:
                assert market[0].done(), 'existing outer list must not flush before the market Future settles'
                assert not lp._market_read_retry
            joined_result = joiner.result(timeout=2)
            assert calls == [True], 'joiner must consume the same account read'
            if outcome == 'error':
                assert joined_result is shared[0].exception(timeout=0)
            else:
                assert joined_result['authenticated'] is True
        finally:
            release_io.set()
            release_logging.set()
            if outer_deferred:
                for worker in log_threads:
                    worker.join(timeout=2)
                    assert not worker.is_alive()
        owner_result = owner.result(timeout=2)
        assert owner_result is joined_result if outcome == 'error' else owner_result == joined_result
    adapter.lp_account_round_end(token)
@pytest.mark.parametrize('through_market', [False, True])
@pytest.mark.parametrize('provider', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'error'])
def test_shared_snapshot_publishes_and_returns_before_blocked_diagnostics(tmp_path, monkeypatch, provider, outcome, through_market):
    from types import SimpleNamespace
    entered, release_io, io_done, logging_entered, release_logging = (Event() for _ in range(5))
    clock, workers = [0.0], []
    observed, caller_done = Event(), Event()
    original = ValueError('original shared snapshot error')
    account_lock = Lock()
    class TimedLock:
        def __enter__(self):
            account_lock.acquire()
            clock[0] += 61
        def __exit__(self, *args):
            account_lock.release()
    class BlockingHandler(logging.Handler):
        def emit(self, record):
            if record.msg.startswith('lp_read_slow') and record.args[0] == 'account_lock_wait':
                logging_entered.set()
                observed.set()
                assert release_logging.wait(10), 'independent logging watchdog'
    client = _SDKAccountClient(datetime.now(UTC))
    calls = []
    def positions():
        workers.append(current_thread())
        calls.append(True)
        entered.set()
        assert release_io.wait(5)
        io_done.set()
        if outcome == 'error':
            raise original
        return []
    monkeypatch.setattr(client, 'list_positions', positions)
    monkeypatch.setattr(polymarket_trading, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    # Account orders trigger display metadata through the public snapshot entry.
    adapter = PolymarketTradingClient(TradingConfig('offline-wallet', 'offline-funder'), client,
        public_client_factory=lambda: SimpleNamespace(list_markets=lambda **kwargs: [], close=lambda: None))
    adapter._lp_account_read_lock = TimedLock()
    handler = BlockingHandler()
    monkeypatch.setattr(polymarket_trading.logger, 'level', logging.INFO)
    polymarket_trading.logger.addHandler(handler)
    lp = PolymarketLPService(PredictionArbitrageStore(tmp_path), adapter)
    def read():
        return adapter.lp_account_snapshot_shared(trade_generation_provider=(lambda: 7) if provider else None)
    def caller():
        try:
            return lp._market_read({'condition_id': 'shared-offline'}, read) if through_market else read()
        except Exception as exc:
            return exc
        finally:
            caller_done.set()
            observed.set()
    try:
        with ThreadPoolExecutor(1) as callers:
            future = callers.submit(caller)
            try:
                assert entered.wait(2)
                assert adapter._lp_account_shared_cache is None
                release_io.set()
                assert observed.wait(2)
                assert logging_entered.wait(2)
                assert io_done.is_set()
                for lock in (account_lock, adapter._lp_account_shared_lock):
                    assert lock.acquire(blocking=False), 'diagnostic output must retain no account/shared lock'
                    lock.release()
                result = future.result(timeout=2)
                if outcome == 'error':
                    assert isinstance(result, polymarket_trading.PolymarketTradingError)
                    assert result.error_code == 'invalid'
                    assert adapter._lp_account_shared_cache is None
                else:
                    assert result['authenticated'] is True
                    assert adapter._lp_account_shared_cache['snapshot'] == result
                    assert read() == result, 'published shared cache must be immediately reusable'
                    if provider:
                        assert result['trade_generation'] == 7
                assert calls == [True]
                assert not lp._market_read_retry
            finally:
                release_io.set()
                release_logging.set()
                for worker in workers:
                    if worker.name.startswith('lp-market-read'):
                        worker.join(timeout=2)
                        assert not worker.is_alive()
    finally:
        polymarket_trading.logger.removeHandler(handler)
        handler.close()


@pytest.mark.parametrize('outcome', ['success', 'error'])
def test_blocked_standard_handler_bounds_completed_workers_and_backlog(tmp_path, monkeypatch, outcome, caplog):
    logging_entered, release_logging = Event(), Event()
    workers, futures, errors = [], [], []
    class BlockingHandler(logging.Handler):
        def emit(self, record):
            if record.msg.startswith('lp_read_task_end'):
                logging_entered.set()
                assert release_logging.wait(10), 'independent logging watchdog'
    class SettledFuture(Future):
        def __init__(self):
            super().__init__()
            self.settled = Event()
            futures.append(self)
        def set_result(self, result):
            super().set_result(result)
            self.settled.set()
        def set_exception(self, exc):
            super().set_exception(exc)
            self.settled.set()
        def result(self, timeout=None):
            assert self.settled.wait(2), 'independent worker watchdog'
            return super().result(timeout)
    monkeypatch.setattr(polymarket_lp, 'Future', SettledFuture)
    caplog.set_level(logging.INFO)
    handler = BlockingHandler()
    polymarket_trading.logger.addHandler(handler)
    lp = PolymarketLPService(PredictionArbitrageStore(tmp_path), object())
    def read():
        workers.append(current_thread())
        polymarket_trading._lp_read_local.task['diagnose'] = True
        if outcome == 'error':
            error = ValueError('original completed read error')
            errors.append(error)
            raise error
        return 'healthy'
    try:
        for index in range(40):
            if outcome == 'error':
                with pytest.raises(ValueError) as caught:
                    lp._market_read({'condition_id': f'secret-{index}'}, read)
                assert caught.value is errors[-1]
            else:
                assert lp._market_read({'condition_id': f'secret-{index}'}, read) == 'healthy'
            if index == 0:
                assert logging_entered.wait(2)
            assert all(f.done() for f in futures)
            assert not lp._market_reads
        # At least 8 real reads completed while the same standard Handler is blocked.
        for worker in workers:
            worker.join(timeout=2)
            assert not worker.is_alive(), 'completed business workers must never wait on logging'
        assert sum(worker.is_alive() for worker in workers) == 0
        from threading import enumerate as enumerate_threads
        consumers = [thread for thread in enumerate_threads() if thread.name == 'lp-read-diagnostics']
        assert consumers == [polymarket_trading._lp_read_log_thread]
        assert consumers[0].is_alive()
        assert polymarket_trading._lp_read_log_queue.qsize() <= 32
        assert polymarket_trading._lp_read_log_dropped >= 7
        assert not lp._market_read_retry
        io_entered = [Event(), Event()]
        release_io = Event()
        pending_workers = []
        class PendingFuture(Future):
            def result(self, timeout=None):
                if timeout == .01:
                    assert all(event.wait(2) for event in io_entered), 'independent I/O-start watchdog'
                return super().result(timeout)
        monkeypatch.setattr(polymarket_lp, 'Future', PendingFuture)
        lp._market_read_timeout = .01
        def pending_io(index):
            pending_workers.append(current_thread())
            io_entered[index].set()
            assert release_io.wait(5)
            return 'actual-io-finished'
        with ThreadPoolExecutor(2) as callers:
            blocked = [callers.submit(lp._market_read, {'condition_id': f'pending-{index}'},
                       lambda index=index: pending_io(index)) for index in range(2)]
            try:
                for caller in blocked:
                    with pytest.raises(ValueError, match='^market_read_timeout$'):
                        caller.result(timeout=2)
                assert len([future for future in lp._market_reads.values() if not future.done()]) == 2
                for _ in range(8):
                    with pytest.raises(ValueError, match='^market_read_capacity$'):
                        lp._market_read({'condition_id': 'third-pending'}, lambda: 'must-not-run')
                assert len(pending_workers) == 2
                assert polymarket_trading._lp_read_log_queue.qsize() <= 32
                assert [thread for thread in enumerate_threads() if thread.name == 'lp-read-diagnostics'] == consumers
            finally:
                release_io.set()
                for worker in pending_workers:
                    worker.join(timeout=2)
                    assert not worker.is_alive()
    finally:
        release_logging.set()
        for worker in workers:
            worker.join(timeout=2)
            assert not worker.is_alive()
        polymarket_trading.logger.removeHandler(handler)
        handler.close()
    # Releasing the Handler alone lets the one consumer recover; no business I/O or manual flush.
    wait_read_logs()
    assert polymarket_trading._lp_read_log_queue.empty()
    recovery_workers = []
    def recovery():
        recovery_workers.append(current_thread())
        polymarket_trading._lp_read_local.task['diagnose'] = True
        return 'healthy-after-release'
    before_records = len(caplog.records)
    assert lp._market_read({'condition_id': 'post-logger-recovery'}, recovery) == 'healthy-after-release'
    for worker in recovery_workers:
        worker.join(timeout=2)
        assert not worker.is_alive()
    wait_read_logs()
    assert any(r.getMessage().startswith('lp_read_task_end') and 'failed=False' in r.getMessage()
               for r in caplog.records[before_records:])
    assert polymarket_trading._lp_read_log_thread is consumers[0] and consumers[0].is_alive()
    assert consumers[0].daemon
    assert any('lp_read_log_limited' in record.getMessage() and 'dropped=' in record.getMessage()
               and 'pending_limit=32' in record.getMessage() for record in caplog.records)
    assert 'secret-' not in caplog.text


def test_healthy_stages_only_aggregate_without_buffering_empty_callbacks(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(polymarket_trading, 'time', SimpleNamespace(monotonic=lambda: 0.0))
    pending = []
    with polymarket_trading._lp_read_task('quiet', deferred_logs=pending) as task:
        for _ in range(100):
            with polymarket_trading._lp_read_stage('account_orders', deferred_logs=pending):
                polymarket_trading._lp_read_count(records=2)
    assert pending == [], 'healthy stages must not evict real diagnostics with no-op callbacks'
    assert task['timings'] == {'account_orders': 0.0}
    assert task['counts'] == {'account_orders': {'logical_reads': 100, 'records': 200}}
    assert polymarket_trading._lp_read_log_dropped == 0


def test_broken_handler_does_not_kill_consumer_or_change_original_results(tmp_path, caplog):
    failed, recovered = Event(), Event()
    records = []
    class FaultyHandler(logging.Handler):
        def emit(self, record):
            if record.msg.startswith('lp_read_task_end'):
                records.append(record)
                if len(records) == 1:
                    failed.set()
                    raise RuntimeError('offline diagnostic sink failure')
                recovered.set()
    caplog.set_level(logging.INFO)
    handler = FaultyHandler()
    polymarket_trading.logger.addHandler(handler)
    lp = PolymarketLPService(PredictionArbitrageStore(tmp_path), object())
    workers = []
    original = ValueError('original business exception')
    def fail():
        workers.append(current_thread())
        raise original
    def succeed():
        workers.append(current_thread())
        polymarket_trading._lp_read_local.task['diagnose'] = True
        return 'healthy'
    consumer = polymarket_trading._lp_read_log_thread
    try:
        with pytest.raises(ValueError) as caught:
            lp._market_read({'condition_id': 'offline-failure'}, fail)
        assert caught.value is original
        assert failed.wait(2)
        assert lp._market_read({'condition_id': 'offline-recovery'}, succeed) == 'healthy'
        assert recovered.wait(2)
        for record, worker in zip(records, workers, strict=True):
            assert record.args[3] == worker.ident
            assert record.args[3] != consumer.ident
        wait_read_logs()
        assert consumer.is_alive() and polymarket_trading._lp_read_log_thread is consumer
        assert any('lp_read_log_limited' in r.getMessage() and 'output_errors=1' in r.getMessage()
                   for r in caplog.records)
        assert not lp._market_read_retry
    finally:
        for worker in workers:
            worker.join(timeout=2)
            assert not worker.is_alive()
        polymarket_trading.logger.removeHandler(handler)
        handler.close()
