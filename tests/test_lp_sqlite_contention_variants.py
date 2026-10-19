"""F-core bounded real-time variants, each isolated by a 55s watchdog."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
import multiprocessing
from pathlib import Path
import sqlite3
import threading
import time
import traceback

import pytest

from open_trader.lp_persistence import LPWriteCoordinator, AdmissionTimeout
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


def _wait(read, predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        threading.Event().wait(.002)
    raise AssertionError(f'completion watchdog: {read()}')


def _execute_child(operation, root, output, args):
    try:
        output.put(('ok', operation(Path(root), *args)))
    except BaseException:
        output.put(('error', traceback.format_exc()))


def _isolated(operation, root, *args):
    context = multiprocessing.get_context('spawn')
    output = context.Queue()
    child = context.Process(target=_execute_child, args=(operation, str(root), output, args))
    started = time.monotonic()
    child.start()
    try:
        status, result = output.get(timeout=55)
        child.join(timeout=3)
        assert not child.is_alive() and child.exitcode == 0
        assert status == 'ok', result
        assert time.monotonic() - started < 60
        print('FCORE_VARIANT', result)
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=3)
        output.close()
        output.join_thread()


def _control_owner(store):
    from open_trader.polymarket_lp_control import LPControlOwner
    from open_trader.polymarket_lp_current_state import CurrentLPState, StoredControl, CommittedControl, ValidatedStartAdmission
    from test_lp_control_contract import ActivationPort, FatalPort

    class Port:
        def load(self, account_id):
            return StoredControl(account_id, True, True, None, None)

        def save_with_admission(self, write, attempt):
            return CommittedControl(**store.lp_save_current_control(asdict(write), admission=attempt))

        def save(self, write):
            return self.save_with_admission(write, None)

    state = CurrentLPState('A', 'variant-process')
    fatal = FatalPort()
    owner = LPControlOwner(state, Port(), ActivationPort(state), fatal)
    owner.initialize(ValidatedStartAdmission('variant-process', 'offline-start'))
    return owner, fatal


def _control_chain(root):
    from open_trader.lp_metadata_writer import LPMetadataWriter
    from open_trader.lp_business_writer import LPBusinessWriter, RequestRecord
    store = PredictionArbitrageStore(root)
    store.initialize_lp_metadata_cache()
    owner, fatal = _control_owner(store)
    coordinator = LPWriteCoordinator(store.path)
    real_connect = sqlite3.connect
    entered, release = threading.Event(), threading.Event()
    order = []
    now = datetime.now(UTC)

    class Boundary(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'BEGIN IMMEDIATE':
                order.append(threading.current_thread().name)
            return super().execute(sql, *args, **kwargs)

        def executemany(self, sql, parameters):
            result = super().executemany(sql, parameters)
            if 'lp_market_competitiveness' in sql:
                entered.set()
                if not release.wait(20):
                    raise TimeoutError('8.6 second holder watchdog')
            return result

    sqlite3.connect = lambda *a, **k: real_connect(*a, **(k | dict(factory=Boundary)))
    metadata, business = LPMetadataWriter(store), LPBusinessWriter(store)
    timer = None
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='native-holder') as pool:
            holding = pool.submit(store.lp_competitiveness_upsert,
                (('holder', Decimal('.12'), now),), progress={'next_start_cursor': 'P1'})
            assert entered.wait(3)
            timer = threading.Timer(8.6, release.set)
            timer.start()
            metadata.enqueue({'m': (now.timestamp()+43200, {'condition_id': 'm'})})
            _wait(coordinator.status, lambda s: s['waiting'] == 1)
            business.offer(RequestRecord(account_id='A', process_epoch='E', request_id='R', session_id='S',
                condition_id='C', token_id='T', kind='BUY', price=Decimal('.4'), quantity=Decimal(10), requested_at=now))
            _wait(coordinator.status, lambda s: s['waiting'] == 2)
            owner.request_running(False)
            _wait(coordinator.status, lambda s: s['waiting'] == 3)
            status = _wait(lambda: owner.poll(time.monotonic()), lambda s: s.save_state == 'saved', seconds=12)
            elapsed = time.monotonic() - status.attempt_started_monotonic
            assert elapsed < 10, elapsed
            assert status.saved_version == status.requested_version == 1
            assert not fatal.signals and status.fatal is None
            holding.result(timeout=3)
        _wait(metadata.status, lambda s: s['saved_entries'] == 1)
        _wait(business.status, lambda s: s['saved_entries'] == 1)
        assert order[0].startswith('native-holder')
        assert order[1] == 'lp-control-save', order
        assert coordinator.status()['lock_order_violations'] == 0
        return dict(case='S02', control_seconds=elapsed, native_begin_order=order)
    finally:
        release.set()
        if timer:
            timer.cancel(); timer.join()
        owner.close(3)
        metadata.stop(timeout=3)
        business.close(timeout=3)
        sqlite3.connect = real_connect


def test_control_save_beats_ten_seconds_behind_8_6_second_holder(tmp_path):
    _isolated(_control_chain, tmp_path)


def _over_budget_chain(root):
    store = PredictionArbitrageStore(root)
    coordinator = LPWriteCoordinator(store.path)
    real_connect = sqlite3.connect
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    timers, target_begins = [], []
    now = datetime.now(UTC)

    class Boundary(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'BEGIN IMMEDIATE' and threading.current_thread().name.startswith('target'):
                target_begins.append(sql)
            return super().execute(sql, *args, **kwargs)

        def executemany(self, sql, parameters):
            rows = tuple(parameters)
            result = super().executemany(sql, rows)
            if 'lp_market_competitiveness' in sql:
                index = int(rows[0][0][-1])
                entered[index].set()
                timer = threading.Timer(8.6, release[index].set)
                timers.append(timer); timer.start()
                if not release[index].wait(20):
                    raise TimeoutError('chained native holder watchdog')
            return result

    sqlite3.connect = lambda *a, **k: real_connect(*a, **(k | dict(factory=Boundary)))
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='holder') as holders, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix='target') as targets:
            first = holders.submit(store.lp_competitiveness_upsert, (('hold-0', Decimal('.1'), now),), progress={})
            assert entered[0].wait(3)
            second = holders.submit(store.lp_competitiveness_upsert, (('hold-1', Decimal('.1'), now),), progress={})
            _wait(coordinator.status, lambda s: s['waiting'] == 1)
            started = time.monotonic()
            target = targets.submit(store.update_lp_observation, 'A', 'C', {'must_not_write': True})
            with pytest.raises(AdmissionTimeout) as failure:
                target.result(timeout=18)
            failed_after = time.monotonic() - started
            assert failure.value.known_not_written is True
            assert target_begins == []
            assert store.lp_observations('A') == {}
            first.result(timeout=5); second.result(timeout=5)
        assert coordinator.status()['admission_timeouts'] == 1
        return dict(case='S03', failed_after_seconds=failed_after, chain_seconds=time.monotonic()-started,
                    target_begin_count=len(target_begins), target_dml_count=0)
    finally:
        for event in release: event.set()
        for timer in timers: timer.cancel(); timer.join()
        sqlite3.connect = real_connect


def test_chained_holders_exceeding_fifteen_seconds_fail_visibly_without_replay(tmp_path):
    _isolated(_over_budget_chain, tmp_path)


def _non_lp_residual(root):
    store = PredictionArbitrageStore(root)
    real_connect = sqlite3.connect
    entered, release = threading.Event(), threading.Event()
    timers = []

    class Boundary(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if 'DELETE FROM llm_usage' in sql:
                timer = threading.Timer(6.2, release.set)
                timers.append(timer); timer.start()
            return super().execute(sql, *args, **kwargs)

        def executemany(self, sql, parameters):
            result = super().executemany(sql, parameters)
            if 'lp_market_competitiveness' in sql:
                entered.set()
                if not release.wait(15):
                    raise TimeoutError('non-LP residual holder watchdog')
            return result

    sqlite3.connect = lambda *a, **k: real_connect(*a, **(k | dict(factory=Boundary)))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            holder = pool.submit(store.lp_competitiveness_upsert,
                (('holder', Decimal('.1'), datetime.now(UTC)),), progress={})
            assert entered.wait(3)
            non_lp = pool.submit(store.prune_llm_usage)
            with pytest.raises(sqlite3.OperationalError) as failure:
                non_lp.result(timeout=10)
            assert failure.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            holder.result(timeout=10)
        with real_connect(store.path) as connection:
            assert connection.execute('SELECT count(*) FROM llm_usage').fetchone()[0] == 0
        return dict(case='S04', non_lp_sqlite_errorcode=failure.value.sqlite_errorcode,
                    disposition='explicit residual; X-lite must change this outcome to unique success')
    finally:
        release.set()
        for timer in timers: timer.cancel(); timer.join()
        sqlite3.connect = real_connect


def test_non_lp_writer_busy_is_explicit_residual_until_x_lite(tmp_path):
    _isolated(_non_lp_residual, tmp_path)


def _external_holder(path, entered, release):
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute('BEGIN IMMEDIATE')
        entered.set()
        if not release.wait(35):
            raise TimeoutError('parent did not release second process')
    finally:
        if connection.in_transaction: connection.execute('ROLLBACK')
        connection.close()


def _second_process(root, duration):
    store = PredictionArbitrageStore(root)
    context = multiprocessing.get_context('spawn')
    entered, release = context.Event(), context.Event()
    child = context.Process(target=_external_holder, args=(str(store.path), entered, release))
    real_connect = sqlite3.connect
    begins, dml = [], []
    timer = None

    class Boundary(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            nonlocal timer
            if sql == 'BEGIN IMMEDIATE':
                begins.append(sql)
                if len(begins) == 1:
                    timer = threading.Timer(duration, release.set); timer.start()
            if 'INSERT INTO lp_market_observations' in sql: dml.append(sql)
            return super().execute(sql, *args, **kwargs)

    child.start()
    try:
        assert entered.wait(5)
        sqlite3.connect = lambda *a, **k: real_connect(*a, **(k | dict(factory=Boundary)))
        if duration < 15:
            assert store.update_lp_observation('A', 'C', {'once': True})['once'] is True
            assert len(begins) == 2 and len(dml) == 1
        else:
            with pytest.raises(sqlite3.OperationalError) as failure:
                store.update_lp_observation('A', 'C', {'once': True})
            assert failure.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            assert 1 <= len(begins) <= 3 and dml == []
        observations = store.lp_observations('A')
        if duration < 15:
            assert set(observations) == {'C'} and observations['C']['once'] is True
        else:
            assert observations == {}
        if timer: timer.join(timeout=20)
        child.join(timeout=3)
        assert child.exitcode == 0
        return dict(case='S05', external_seconds=duration, begin_attempts=len(begins), dml_count=len(dml))
    finally:
        release.set()
        if timer: timer.cancel(); timer.join()
        child.join(timeout=3)
        if child.is_alive(): child.terminate(); child.join(timeout=3)
        sqlite3.connect = real_connect


@pytest.mark.parametrize('duration', [5.8, 16.0])
def test_second_process_holder_is_bounded_and_never_replays_body(tmp_path, duration):
    _isolated(_second_process, tmp_path, duration)
