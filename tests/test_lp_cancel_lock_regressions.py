"""BUY cancellation contracts at public D2, SDK and real SQLite boundaries."""
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
import json
import fcntl
import socket
import sqlite3
import threading
import time

import pytest

from open_trader.polymarket_lp_live_execution import RotationDecision
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.lp_business_writer import LPBusinessWriter, ReceiptRecord
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_lp_business_writer_contract import request
from tests.test_lp_d2_execution_bridge import attach, rotation_runtime
from tests.test_lp_d2_integration import _d2_protection_runtime, settle_control
from tests.test_lp_order_registration_contract import WALLET, CONDITION_ID, TOKEN_ID, _open_order


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('cancel regression fixtures must not connect to a socket')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def finished(handle):
    result = handle.result(timeout=8)
    handle._thread.join(timeout=3)
    assert handle.done(), 'actual native completion watchdog'
    return result


def drained(writer):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = writer.status()
        if not status['pending_entries'] and not status['inflight_entries']:
            return status
        time.sleep(.001)
    pytest.fail(f'audit writer completion watchdog: {writer.status()}')


def action_rows(store):
    with sqlite3.connect(store.path) as connection:
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM lp_actions ORDER BY rowid")]


@contextmanager
def safety_sql(monkeypatch, *, hold=False, fail=False):
    """Control the real SQLite safety read of the actual native sender only."""
    connect = sqlite3.connect
    entered, release = threading.Event(), threading.Event()
    settings = {'fail': fail, 'hold': hold}

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if (threading.current_thread().name == 'lp-d2-independent-cancel'
                    and sql.startswith('SELECT * FROM executions WHERE state NOT IN')):
                if settings['hold']:
                    entered.set()
                    assert release.wait(8), 'external safety SQL release watchdog'
                if settings['fail']:
                    # Native SQLite failure, not a stub of the mutation gate.
                    return super().execute('SELECT * FROM missing_cancel_safety_fixture')
            return super().execute(sql, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(sqlite3, 'connect', lambda *a, **kw:
            connect(*a, **(kw | {'factory': ObservedConnection})))
        try:
            yield settings, entered, release
        finally:
            release.set()


class PermissionPort:
    """Injected external mutation-admission port, never an internal gate patch."""
    admit = True
    allowed = True

    def acquire(self, kind):
        assert kind in ('BUY', 'CANCEL', 'MANUAL_BUY')
        if not self.admit:
            return None
        port = self

        class Lease:
            released = False

            def allowed(self):
                return not self.released and port.allowed

            def release(self):
                self.released = True

        return Lease()


def current_rotation(live, key):
    snapshot = live.state.snapshot()
    order = next(o for o in snapshot.active_orders if o.key == key)
    observation = snapshot.observation
    decision = RotationDecision(live.account_id, observation.cycle_generation,
        observation.observation_id, (order,))
    return decision, order


@pytest.mark.parametrize('kind', ['queue', 'scoring', 'unknown_yield'])
def test_protective_buy_cancels_ignore_rotation_backoff_and_keep_guards(tmp_path, monkeypatch, kind):
    live, sdk, public, clock = _d2_protection_runtime(
        tmp_path, monkeypatch, submit=kind != 'unknown_yield')
    permission = PermissionPort()
    live.bind_mutation_admission(permission, permission.acquire)
    key = (WALLET, CONDITION_ID, TOKEN_ID, 'entry-A' if kind != 'unknown_yield' else 'risk-A')
    try:
        if kind == 'unknown_yield':
            sdk.orders = (_open_order('risk-A', 'BUY', price='.30', original='10'),)
            public.failed = True
        elif kind == 'scoring':
            sdk.get_order_scoring = lambda **kw: False
        live.run_once()
        assert live.run_protection_once() == ()
        clock[0] = 599 if kind == 'unknown_yield' else 59 if kind == 'scoring' else 0
        live.run_once()
        permission.allowed = False
        blocked = live.cancel_independent(key, rotation_origin=current_rotation(live, key))
        assert finished(blocked).status.value == 'not_sent'
        assert sdk.cancels == []
        permission.allowed = True
        permission.admit = False
        clock[0] += 1
        if kind == 'queue':
            public.level = Decimal('4000')
        live.run_once()
        assert live.run_protection_once() == ()
        assert sdk.cancels == []  # a risk trigger never bypasses admission

        # Invalid account identity is still rejected, without a new request.
        permission.admit = True
        with pytest.raises(ValueError, match='independent_cancel_identity'):
            live.cancel_independent(('wrong-account', *key[1:]))
        before = tuple(sdk.orders)
        sdk.orders = None
        live.run_once()
        try:
            assert live.run_protection_once() == ()
        except ValueError as error:
            assert str(error) == 'independent_cancel_not_current'
        assert sdk.cancels == []  # incomplete current orders still cannot send
        sdk.orders = before
        live.run_once()
        attempts = live.run_protection_once()
        assert len(attempts) == 1
        assert finished(attempts[0]).status.value == 'accepted'
        assert sdk.cancels == [(key[3],)]
        live.run_protection_once()
        assert sdk.cancels == [(key[3],)]
        assert all(order.side == 'BUY' for order in before)
    finally:
        assert live.close(timeout=5)
        live.trading.close()


@pytest.mark.parametrize('gate', ['admission', 'identity', 'currentness', 'capacity', 'origin'])
def test_unattempted_cancel_deferral_has_reason_without_fabricated_request(tmp_path, monkeypatch, gate):
    live, execution, sdk, ids, pools, order, qualify = rotation_runtime(tmp_path, monkeypatch)
    sdk.orders = tuple(order(k) for k in 'ABC')
    live.run_once()
    key = (WALLET, *ids['C'], 'C')
    origin = current_rotation(live, key)
    expected = dict(admission='mutation_not_admitted', identity='independent_cancel_identity',
        currentness='independent_cancel_not_current', capacity='native_capacity',
        origin='rotation_not_current')[gate]
    release, entered = threading.Event(), {oid: threading.Event() for oid in 'AB'}
    handles = []
    lock = None

    def waiting_cancel(*, order_ids):
        sdk.cancels.append(order_ids)
        entered[order_ids[0]].set()
        assert release.wait(8), 'external capacity SDK release watchdog'
        return dict(canceled=order_ids, not_canceled={})

    try:
        if gate == 'admission':
            lock = (tmp_path/'execution.lock').open('a')
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif gate == 'identity':
            key = ('wrong-account', *key[1:])
        elif gate == 'currentness':
            sdk.orders = None
            live.run_once()
        elif gate == 'capacity':
            sdk.cancel_orders = waiting_cancel
            for oid in 'AB':
                handle = live.cancel_independent((WALLET, *ids[oid], oid))
                handles.append(handle)
                assert entered[oid].wait(3)
        else:
            live.run_once()  # a different accepted observation invalidates the old origin
        drained(live.writer)
        before = action_rows(live.store)
        before_native = live.status()['native_inflight']
        before_targets = tuple(t for p in live.state.snapshot().progress.values() for t in p.cancel_targets)
        for _ in range(25):
            if gate == 'origin':
                assert live.cancel_independent(key, rotation_origin=origin) is None
            else:
                with pytest.raises((ValueError, RuntimeError), match=expected):
                    live.cancel_independent(key)
        assert live.status()['native_inflight'] == before_native
        assert tuple(t for p in live.state.snapshot().progress.values() for t in p.cancel_targets) == before_targets
        assert action_rows(live.store) == before
        assert sdk.cancels == ([('A',), ('B',)] if gate == 'capacity' else [])
        diagnostic = execution.lp_auto_state()['diagnostics']['cancel_blocks']
        assert diagnostic['total'] == 25
        assert diagnostic['by_reason'] == {expected: 25}
        assert len(diagnostic['recent']) == 20
        assert all(r['reason'] == expected and r.get('request_id') is None for r in diagnostic['recent'])
        if gate == 'identity':
            assert all(r.get('order_key') is None and r.get('session_id') is None for r in diagnostic['recent'])
        assert execution.lp_auto_state()['diagnostics']['cancel_blocks'] == diagnostic
        assert action_rows(live.store) == before
    finally:
        release.set()
        for handle in handles:
            finished(handle)
        if lock is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        assert live.close(timeout=5)
        live.trading.close()


@pytest.mark.parametrize('case', ['native_pending', 'native_unknown', 'fresh_decision'])
def test_unknown_cancel_is_not_a_never_sent_backoff_retry(tmp_path, monkeypatch, case):
    live, execution, sdk, ids, pools, order, qualify = rotation_runtime(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    handle = None

    def uncertain(*, order_ids):
        sdk.cancels.append(order_ids)
        entered.set()
        assert release.wait(8), 'actual UNKNOWN SDK release watchdog'
        raise TimeoutError('offline actual native cancellation has unknown outcome')

    sdk.cancel_orders = uncertain
    try:
        sdk.orders = tuple(order(k) for k in 'ABCDE')
        pools['F'] = Decimal('200')
        qualify('F')
        first = execution.lp_auto_scheduled_check()
        outcome, = first.rotation_outcomes
        handle = outcome.handle
        assert entered.wait(3)
        original_id = handle.identity.request_id
        sdk.advance(11)
        if case == 'native_pending':
            assert live.cancel_independent(outcome.order_key) is handle
            assert not handle.done()
        release.set()
        assert finished(handle).status.value == 'unknown_error'
        live.dispatch_rotation(first)
        assert sdk.cancels == [('E',)]
        drained(live.writer)
        row, = [r for r in action_rows(live.store) if r['request_id'] == original_id]
        assert row['receipt']['outcome'] == 'unknown'
        assert next(o for o in live.state.snapshot().active_orders if o.key[3] == 'E').terminal_proof is None
        if case == 'fresh_decision':
            qualify('F')
            fresh = execution.lp_auto_scheduled_check()
            new, = fresh.rotation_outcomes
            assert finished(new.handle).status.value == 'unknown_error'
            assert new.handle.identity.request_id != original_id
            assert sdk.cancels == [('E',), ('E',)]
    finally:
        release.set()
        if handle is not None:
            handle._thread.join(3)
        assert live.close(timeout=5)
        live.trading.close()


@pytest.mark.parametrize('change', ['new_order_id', 'new_terms', 'new_generation'])
def test_cancel_backoff_does_not_hide_new_order_or_terms(tmp_path, monkeypatch, change):
    live, execution, sdk, ids, pools, order, qualify = rotation_runtime(tmp_path, monkeypatch)
    try:
        sdk.orders = tuple(order(k) for k in 'ABCDE')
        pools['F'] = Decimal('200')
        qualify('F')
        with safety_sql(monkeypatch, fail=True):
            first = execution.lp_auto_scheduled_check()
            previous, = first.rotation_outcomes
            assert finished(previous.handle).status.value == 'not_sent'
        assert sdk.cancels == []
        sdk.advance(1)
        if change == 'new_generation':
            live.request_running(False)
            settle_control(live)
            live.request_running(True)
            settle_control(live)
        else:
            sdk.orders = tuple(order(k, 'E2' if k == 'E' and change == 'new_order_id' else k,
                original='21' if k == 'E' and change == 'new_terms' else '20') for k in 'ABCDE')
        qualify('F')
        fresh = execution.lp_auto_scheduled_check()
        handles = [r.handle for r in fresh.rotation_outcomes if r.handle]
        for handle in handles:
            assert finished(handle).status.value == 'accepted'
            assert handle.identity.request_id != previous.handle.identity.request_id
        expected = 'E2' if change == 'new_order_id' else 'E'
        assert sdk.cancels.count((expected,)) == 1
        if change == 'new_generation':
            assert {ids[0] for ids in sdk.cancels} == set('ABCDE')
        else:
            assert sdk.cancels == [(expected,)]
        count = len(sdk.cancels)
        live.dispatch_rotation(first)
        assert len(sdk.cancels) == count
    finally:
        assert live.close(timeout=5)
        live.trading.close()


@pytest.mark.parametrize('reason', [
    'mutation_not_admitted', 'mutation_lease_not_current', 'independent_cancel_identity',
    'independent_cancel_not_current', 'independent_cancel_inflight', 'rotation_not_current',
    'protection_cancel_owned', 'protection_cancel_not_current',
    'protection_evidence_not_current', 'native_capacity',
])
def test_cancel_block_reason_codes_round_trip_without_schema_change(tmp_path, reason):
    store = PredictionArbitrageStore(tmp_path)
    writer = LPBusinessWriter(store)
    try:
        with sqlite3.connect(store.path) as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
        record = request('blocked-'+reason, kind='CANCEL')
        receipt = ReceiptRecord(record.account_id, record.process_epoch, record.request_id,
            record.session_id, record.condition_id, record.token_id, record.order_id,
            'not_sent', record.requested_at, reason)
        with pytest.raises(ValueError, match='record_reason_code_invalid'):
            ReceiptRecord(record.account_id, record.process_epoch, record.request_id,
                record.session_id, record.condition_id, record.token_id, record.order_id,
                'not_sent', record.requested_at, 'private-token-must-not-escape')
        writer.offer(record)
        writer.offer(receipt)
        assert drained(writer)['saved_entries'] == 2
        reopened = PredictionArbitrageStore(tmp_path, initialize=False)
        row, = reopened.lp_actions(record.session_id)
        assert row['request_id'] == record.request_id and row['order_id'] == 'order-A'
        assert row['receipt']['outcome'] == 'not_sent'
        assert row['receipt']['reason_code'] == reason

        # Old nullable receipts remain readable; no invented historical reason.
        legacy = request('legacy', kind='CANCEL', session_id='legacy-session')
        writer.offer(legacy)
        writer.offer(ReceiptRecord(legacy.account_id, legacy.process_epoch, legacy.request_id,
            legacy.session_id, legacy.condition_id, legacy.token_id, legacy.order_id,
            'not_sent', legacy.requested_at))
        assert drained(writer)['saved_entries'] == 4
        old, = reopened.lp_actions(legacy.session_id)
        assert old['receipt']['reason_code'] is None
        with sqlite3.connect(store.path) as connection:
            assert connection.execute('PRAGMA user_version').fetchone()[0] == version
    finally:
        writer.close(timeout=5)


@pytest.mark.parametrize('gate', ['lease', 'control', 'rotation', 'protection_origin'])
def test_recorded_cancel_denial_keeps_actual_gate_reason(tmp_path, monkeypatch, gate):
    expected = dict(lease='mutation_lease_not_current', control='control_or_native_not_current',
        rotation='rotation_not_current', protection_origin='protection_cancel_not_current')[gate]
    if gate == 'protection_origin':
        live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch, submit=False)
        execution = attach(live, PolymarketLPService(live.store, live.trading), tmp_path)
        sdk.orders = (_open_order('entry-A', 'BUY', price='.30', original='2000'),)
        live.run_once()
        assert live.run_protection_once() == ()
        public.level, clock[0] = Decimal('4000'), 1
        live.run_once()
    else:
        live, execution, sdk, ids, pools, order, qualify = rotation_runtime(tmp_path, monkeypatch)
        sdk.orders = tuple(order(k) for k in 'ABCDE')
        pools['F'] = Decimal('200')
        qualify('F')
    handle = None
    try:
        with safety_sql(monkeypatch, hold=True) as (settings, entered, release):
            if gate == 'protection_origin':
                handle, = live.run_protection_once()
            else:
                result = execution.lp_auto_scheduled_check()
                dispatched, = result.rotation_outcomes
                handle = dispatched.handle
            assert entered.wait(3), 'actual final sender safety SQL watchdog'
            assert sdk.cancels == []
            if gate == 'lease':
                settings['fail'] = True
            elif gate == 'control':
                live.request_running(False)
                settle_control(live)
            elif gate == 'rotation':
                sdk.orders = tuple(order(k, original='21' if k == 'E' else '20') for k in 'ABCDE')
                live.run_once()
            else:
                clock[0] += 1
                live.run_once()
            release.set()
            outcome = finished(handle)
        assert outcome.status.value == 'not_sent' and sdk.cancels == []
        drained(live.writer)
        rows = [r for r in action_rows(live.store) if r['request_id'] == handle.identity.request_id]
        row, = rows
        assert row['receipt']['outcome'] == 'not_sent'
        assert row['receipt']['reason_code'] == expected
        diagnostic = execution.lp_auto_state()['diagnostics']
        detail, = [r for r in diagnostic['local_outcomes']['recent']
            if r['request_id'] == handle.identity.request_id]
        assert detail['reason'] == expected
        if gate == 'protection_origin':
            assert detail['trigger_reason'] == 'queue_threshold'
            assert diagnostic['protection']['cancel_outcomes']['queue_threshold']['not_sent'] == 1
        else:
            rotation = diagnostic['rotation']['outcomes']
            assert rotation['reason_counts'] == {expected: 1}
            assert rotation['samples'][0]['reason'] == expected
    finally:
        if handle is not None:
            handle._thread.join(3)
        assert live.close(timeout=5)
        live.trading.close()


def test_rotation_never_sent_backoff_suppresses_request_and_receipt_churn(tmp_path, monkeypatch):
    live, execution, sdk, ids, pools, order, qualify = rotation_runtime(tmp_path, monkeypatch)
    sdk.orders = tuple(order(k) for k in 'ABCDE')
    pools['F'] = Decimal('200')
    request_ids = []
    now = 0.
    try:
        with safety_sql(monkeypatch, fail=True) as (settings, entered, release):
            for at, count in [(0., 1), (1., 1), (5., 1), (9.999, 1),
                              (10., 2), (19.999, 2), (20., 3)]:
                sdk.advance(at-now)
                now = at
                if at == 20.:
                    settings['fail'] = False
                qualify('F')
                result = execution.lp_auto_scheduled_check()
                for dispatched in result.rotation_outcomes:
                    if dispatched.handle is not None:
                        outcome = finished(dispatched.handle)
                        assert outcome.status.value == ('accepted' if at == 20. else 'not_sent')
                        assert dispatched.handle.identity.request_id not in request_ids
                        request_ids.append(dispatched.handle.identity.request_id)
                    else:
                        assert (dispatched.status, dispatched.reason) == ('suppressed', 'cancel_backoff')
                drained(live.writer)
                rows = [r for r in action_rows(live.store) if r['kind'] == 'CANCEL']
                assert len(rows) == count
                assert len(request_ids) == count
                assert {r['request_id'] for r in rows} == set(request_ids)
                assert all(r['receipt']['outcome'] == 'not_sent' for r in rows[:2 if at == 20. else count])
                if at < 20.:
                    assert sdk.cancels == []
                    assert live.state.snapshot().buy_reserved.value == Decimal('50')
                    assert live.state.snapshot().new_capacity.value == Decimal('50')
                else:
                    assert sdk.cancels == [('E',)]
                    # The ID-only ACK cannot invent final-fill proof or release funds.
                    assert live.state.snapshot().new_capacity.state == 'unknown'
                if at in (1., 5., 9.999, 19.999):
                    diagnostic = execution.lp_auto_state()['diagnostics']['cancel_blocks']
                    detail = diagnostic['recent'][-1]
                    assert detail['reason'] == 'cancel_backoff'
                    assert detail['original_reason'] == 'mutation_lease_not_current'
                    assert detail['remaining_seconds'] == pytest.approx(10.-(at if at < 10 else at-10))
                    assert detail['suppressed_count'] == ({1.: 1, 5.: 2, 9.999: 3, 19.999: 1}[at])
                    assert execution.lp_auto_state()['diagnostics']['cancel_blocks'] == diagnostic
                    assert action_rows(live.store) == rows
            assert len(set(request_ids)) == 3
    finally:
        assert live.close(timeout=5)
        live.trading.close()
