"""Bounded causal evidence uses offline adapters, real SQLite and controlled gates."""
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from open_trader import polymarket_trading
from tests.test_lp_read_diagnostics import wait_read_logs
from tests.test_lp_account_reservation_reconciliation import runtime


@pytest.fixture(autouse=True)
def drain_diagnostics():
    wait_read_logs()
    yield
    wait_read_logs()


def events(caplog, name):
    return [record.args[1] for record in caplog.records
            if record.msg == 'lp_causal_event event=%s facts=%s' and record.args[0] == name]


def test_read_publish_and_auto_use_share_timestamps_during_sqlite_wait(runtime, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    store, adapter, account, lp, execution = runtime()
    started, release = Event(), Event()
    connect = store._connection
    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == 'BEGIN IMMEDIATE':
                started.set()
                assert release.wait(5), 'Independent SQLite publication watchdog'
            return super().execute(sql, *args)
    def observed():
        connection = sqlite3.connect(store.path, isolation_level=None, factory=ObservedConnection)
        connection.row_factory = sqlite3.Row
        return connection
    # The holder is a real writer. Gate the attempt so the assertion has no
    # dependency on scheduler speed or the production busy timeout.
    holder = connect()
    holder.execute('BEGIN IMMEDIATE')
    monkeypatch.setattr(store, '_connection', observed)
    with ThreadPoolExecutor(1) as workers:
        job = workers.submit(execution._auto_pool.run_once)
        try:
            assert started.wait(5)
            wait_read_logs()
            assert not job.done()
            assert events(caplog, 'account_read_begin')
            assert events(caplog, 'account_read_end')
            assert events(caplog, 'account_publish_wait')
            assert not events(caplog, 'account_publish_end')
        finally:
            holder.rollback()
            holder.close()
            release.set()
        result = job.result(timeout=5)
    wait_read_logs()
    read = events(caplog, 'account_read_end')[-1]
    for name in ('account_publish_wait', 'account_publish_begin', 'account_publish_end', 'auto_account_use'):
        matching = [row for row in events(caplog, name) if row.get('read_started_at') == read['read_started_at']]
        assert matching, (name, events(caplog, name))
        assert matching[-1]['read_ended_at'] == read['read_ended_at']
        assert matching[-1]['task_id'] == read['task_id']
    assert events(caplog, 'account_publish_end')[-1]['outcome'] == 'registered'
    assert result['desired_running'] is False
    assert account.posts == account.cancels == []
    assert adapter.config.wallet_address not in caplog.text


def test_execution_busy_reports_exact_local_owner_stage_and_release(runtime, caplog):
    caplog.set_level(logging.INFO)
    _, _, account, _, execution = runtime()
    with polymarket_trading._lp_read_task('offline_owner') as task:
        with polymarket_trading._lp_read_stage('facts_publish'):
            owner = execution._acquire_global_lock()
            assert owner is not None
            try:
                with ThreadPoolExecutor(1) as workers:
                    assert workers.submit(execution._acquire_global_lock).result(timeout=2) is None
                wait_read_logs()
                acquired = events(caplog, 'execution_lock_acquired')[-1]
                busy = events(caplog, 'execution_lock_busy')[-1]
                assert busy['owner_id'] == acquired['owner_id']
                assert busy['owner_task_id'] == task['task_id']
                assert busy['owner_stage'] == 'facts_publish'
                assert busy['owner_thread'] == task['thread']
                assert busy['owner_scope'] == 'process_local'
            finally:
                execution._release_global_lock(owner)
    wait_read_logs()
    assert events(caplog, 'execution_lock_released')[-1]['owner_id'] == acquired['owner_id']
    again = execution._acquire_global_lock()
    assert again is not None
    execution._release_global_lock(again)
    assert account.posts == account.cancels == []


def test_causal_overflow_and_sink_failure_cannot_block_account_read(runtime, monkeypatch):
    _, adapter, _, _, _ = runtime()
    entered, release = Event(), Event()
    def blocked(*args):
        entered.set()
        assert release.wait(5), 'Independent logging watchdog'
        raise RuntimeError('sink unavailable')
    monkeypatch.setattr(polymarket_trading.logger, 'info', blocked)
    try:
        polymarket_trading._lp_causal_event('test_start')
        assert entered.wait(2)
        with ThreadPoolExecutor(1) as workers:
            def read():
                for _ in range(100):
                    polymarket_trading._lp_causal_event('test_overflow')
                return adapter.lp_account_snapshot()
            snapshot = workers.submit(read).result(timeout=2)
        assert snapshot['authenticated'] is True
        assert polymarket_trading._lp_read_log_queue.qsize() <= 32
        assert polymarket_trading._lp_read_log_dropped > 0
    finally:
        release.set()


@pytest.mark.parametrize('handoff', [False, True], ids=['same-owner', 'later-owner'])
def test_failed_execution_attempt_never_attributes_a_later_owner(runtime, monkeypatch, caplog, handoff):
    from threading import Lock
    from open_trader import prediction_arbitrage_execution as execution_module

    caplog.set_level(logging.INFO)
    _, _, _, _, execution = runtime()
    original = dict(id='owner-A', thread=1, started=1.0, entry='offline_owner', task=None)
    later = dict(id='owner-B', thread=2, started=2.0, entry='offline_owner', task=None)
    underlying = Lock()
    underlying.acquire()
    attempts = []
    class HandoffLock:
        def acquire(self, blocking):
            attempts.append(blocking)
            observed = underlying.acquire(blocking)
            assert observed is False
            if handoff:
                # Fail against A, then A releases and B obtains the real lock
                # before the caller resumes to observe the diagnostic owner.
                underlying.release()
                assert underlying.acquire(False)
                execution_module._PROCESS_LOCK_OWNER = later
            return observed
    monkeypatch.setattr(execution, '_process_lock', HandoffLock())
    monkeypatch.setattr(execution_module, '_PROCESS_LOCK_OWNER', original)
    try:
        assert execution._acquire_global_lock_once() is None
        assert attempts == [False]
        assert underlying.locked(), 'A failed waiter must not release either owner'
        assert execution_module._PROCESS_LOCK_OWNER is (later if handoff else original)
        wait_read_logs()
        busy = events(caplog, 'execution_lock_busy')[-1]
        if handoff:
            assert busy['owner_scope'] == 'unknown'
            assert 'owner_id' not in busy
        else:
            assert busy['owner_scope'] == 'process_local'
            assert busy['owner_id'] == original['id']
    finally:
        underlying.release()


@pytest.mark.parametrize('reject', [False, True], ids=['sent-once', 'sendtime-funds-rejected'])
def test_both_presend_account_uses_log_the_actual_distinct_reads(runtime, monkeypatch, caplog, reject):
    from copy import deepcopy
    from tests.test_lp_auto_refill_contract import prepare

    caplog.set_level(logging.INFO)
    _, _, account, lp, execution, _ = prepare(runtime, count=1, target=1)
    used = []
    reader = lp._read_candidate_snapshot
    def record_use(request, **kwargs):
        used.append(dict(account=deepcopy(kwargs['account']), used_at=lp._now(),
                         sending=bool(kwargs.get('ignore_session_id'))))
        return reader(request, **kwargs)
    monkeypatch.setattr(lp, '_read_candidate_snapshot', record_use)
    if reject:
        sign = account.create_limit_order
        balance = account.get_balance_allowance
        def depleted(**kwargs):
            return balance(**kwargs).__class__(balance='100000000',
                allowances={account.environment.standard_exchange: '0'})
        def sign_then_deplete(**kwargs):
            signed = sign(**kwargs)
            monkeypatch.setattr(account, 'get_balance_allowance', depleted)
            return signed
        monkeypatch.setattr(account, 'create_limit_order', sign_then_deplete)
    before = account.position_reads
    state = execution.lp_auto_run_once(round_id='causal-two-presend-reads')
    wait_read_logs()
    assert len(used) == 2 and [row['sending'] for row in used] == [False, True]
    assert used[0]['account']['read_started_at'] != used[1]['account']['read_started_at']
    assert account.position_reads - before == 4, 'Initial, two necessary per-BUY reads and final refresh'
    for name, actual in zip(('auto_presend_account_use', 'auto_send_account_use'), used, strict=True):
        records = events(caplog, name)
        assert len(records) == 1, (name, records)
        for key in ('read_started_at', 'read_ended_at', 'checked_at'):
            assert records[0][key] == actual['account'][key].isoformat()
        assert records[0]['used_at'] == actual['used_at'].isoformat()
    assert len(account.posts) == (0 if reject else 1), state['last_round']
    if reject:
        assert state['last_round']['actions'][0]['state'] == 'rejected'
        assert state['last_round']['actions'][0]['request_state'] == 'entry_rejected'
        assert state['last_round']['actions'][0]['reason'] == 'balance_insufficient'
    else:
        replay = execution.lp_auto_run_once(round_id='causal-two-presend-reads')
        assert replay['slots']['occupied'] == 1
        assert len(account.posts) == 1


@pytest.mark.parametrize('failed', [False, True], ids=['success', 'exception'])
def test_causal_stage_begin_end_use_current_stage_and_restore_outer_stage(caplog, failed):
    caplog.set_level(logging.INFO)
    with polymarket_trading._lp_read_task('stage-regression') as task:
        task['stage'] = 'outer'
        try:
            with polymarket_trading._lp_read_stage('facts_publish'):
                assert task['stage'] == 'facts_publish'
                if failed:
                    raise ValueError('controlled original exception')
        except ValueError as exc:
            assert failed and str(exc) == 'controlled original exception'
        assert task['stage'] == 'outer'
    wait_read_logs()
    begin, end = events(caplog, 'facts_publish_begin')[-1], events(caplog, 'facts_publish_end')[-1]
    assert begin['stage'] == end['stage'] == 'facts_publish'
    assert begin['task_id'] == end['task_id'] == task['task_id']
    assert end['outcome'] == ('failed' if failed else 'complete')
