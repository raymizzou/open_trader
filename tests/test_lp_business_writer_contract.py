"""Disposable logging and reliable control at the real SQLite boundary."""
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
import json
import sqlite3
from threading import Event
import time
import weakref

import pytest
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

STAMP = datetime(2026, 10, 16, tzinfo=UTC)


def request(identity='R1', **changes):
    from open_trader.lp_business_writer import RequestRecord
    values = dict(account_id='account-A', process_epoch='epoch-1', request_id=identity,
        session_id='session-1', condition_id='c1', token_id='t1', kind='BUY',
        price=Decimal('.40'), quantity=Decimal('10'), requested_at=STAMP,
        order_id='order-A')
    return RequestRecord(**(values | changes))


def wait_status(writer, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = writer.status()
        if predicate(state): return state
        time.sleep(.002)
    pytest.fail(f'writer completion watchdog: {writer.status()}')


def blocked_database(store, monkeypatch):
    """Real independent write lock; tracing observes native BEGIN entry."""
    real_connect = sqlite3.connect
    blocker = real_connect(store.path, isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    entered = Event()
    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(lambda sql: entered.set() if sql == 'BEGIN IMMEDIATE' else None)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', connect)
    return blocker, entered


def stored_actions(store):
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute('SELECT action_key,payload FROM lp_actions ORDER BY rowid').fetchall()
    return [(key, json.loads(payload)) for key, payload in rows]


def test_offer_returns_while_sqlite_write_is_blocked(tmp_path, monkeypatch):
    from open_trader.lp_business_writer import LPBusinessWriter
    store = PredictionArbitrageStore(tmp_path)
    generation = store.lp_trade_generation()
    notifications = []
    store._lp_trade_change_listener = notifications.append
    blocker, entered = blocked_database(store, monkeypatch)
    writer = LPBusinessWriter(store)
    try:
        result = writer.offer(request())
        assert result is None  # disposable admission carries no permission
        assert entered.wait(2)
        status = writer.status()
        assert status['inflight_entries'] == 1 and status['pending_entries'] == 0
        assert stored_actions(store) == []
        with pytest.raises(TypeError): writer.offer(lambda: None)
        with pytest.raises(TypeError): writer.offer({'desired_running': True})
        blocker.execute('COMMIT')
        wait_status(writer, lambda s: s['saved_entries'] == 1)
        [(identity, payload)] = stored_actions(store)
        assert identity == 'R1'
        assert payload['request_id'] == 'R1' and payload['account_id'] == 'account-A'
        assert payload['price'] == '0.40' and payload['quantity'] == '10'
        assert payload['requested_at'] == STAMP.isoformat()
        assert store.lp_trade_generation() == generation and notifications == []
    finally:
        if blocker.in_transaction: blocker.execute('ROLLBACK')
        blocker.close()
        writer.close(timeout=3)


def test_fifo_capacity_counts_inflight_and_drops_oldest_pending(tmp_path, monkeypatch):
    from open_trader.lp_business_writer import LPBusinessWriter
    from open_trader.lp_metadata_writer import DEFAULT_POLICY as METADATA_POLICY
    store = PredictionArbitrageStore(tmp_path)
    blocker, entered = blocked_database(store, monkeypatch)
    writer = LPBusinessWriter(store, policy=dict(max_entries=3, max_encoded_bytes=100_000))
    try:
        caller_values = dict(quantity=Decimal('10'))
        first = request(**caller_values)
        writer.offer(first)
        assert entered.wait(2)
        size = writer.status()['inflight_bytes']
        assert size > 0
        caller_values['quantity'] = Decimal('999')
        with pytest.raises(FrozenInstanceError): first.quantity = Decimal('999')
        writer.offer(request('R2'))
        writer.offer(request('R2', quantity=Decimal('11')))
        assert writer.status()['pending_entries'] == 2  # repeated IDs are not merged
        writer.offer(request('R4'))
        status = writer.status()
        assert status['inflight_entries'] == 1 and status['pending_entries'] == 2
        assert status['dropped_overflow'] == 1
        writer.offer(request('oversize-' + '界' * 40_000))
        assert writer.status()['dropped_oversize'] == 1
        assert writer.status()['pending_entries'] == 2
        blocker.execute('COMMIT')
        wait_status(writer, lambda s: s['saved_entries'] == 3)
        rows = stored_actions(store)
        assert [key for key, _ in rows] == ['R1', 'R2', 'R4']
        assert rows[0][1]['quantity'] == '10' and rows[1][1]['quantity'] == '11'
        assert METADATA_POLICY == dict(max_entries=3000, max_encoded_bytes=16*1024*1024, batch_rows=400)
    finally:
        if blocker.in_transaction: blocker.execute('ROLLBACK')
        blocker.close()
        writer.close(timeout=3)

    # A separate count limit leaves only an actual in-flight item: drop incoming.
    blocker, entered = blocked_database(store, monkeypatch)
    writer = LPBusinessWriter(store, policy=dict(max_entries=1, max_encoded_bytes=100_000))
    try:
        writer.offer(request('S1'))
        assert entered.wait(2)
        writer.offer(request('S2'))
        status = writer.status()
        assert status['pending_entries'] == 0 and status['inflight_entries'] == 1
        assert status['dropped_inflight_capacity'] == 1
    finally:
        blocker.execute('COMMIT'); blocker.close()
        wait_status(writer, lambda s: s['saved_entries'] == 1)
        writer.close(timeout=3)

    # Same-length UTF-8 values at the measured public byte boundary, ample count.
    blocker, entered = blocked_database(store, monkeypatch)
    writer = LPBusinessWriter(store, policy=dict(max_entries=10, max_encoded_bytes=size*2))
    try:
        writer.offer(request('B1'))
        assert entered.wait(2)
        writer.offer(request('B2'))
        assert writer.status()['pending_bytes'] + writer.status()['inflight_bytes'] == size*2
        writer.offer(request('B3'))
        status = writer.status()
        assert status['pending_entries'] == 1 and status['dropped_overflow'] == 1
        assert status['pending_bytes'] + status['inflight_bytes'] == size*2
    finally:
        blocker.execute('COMMIT'); blocker.close()
        wait_status(writer, lambda s: s['saved_entries'] == 2)
        writer.close(timeout=3)
    assert [key for key, _ in stored_actions(store)][-2:] == ['B1', 'B3']


def test_failed_write_is_visible_and_not_retried(tmp_path, monkeypatch, caplog):
    from open_trader.lp_business_writer import LPBusinessWriter
    store = PredictionArbitrageStore(tmp_path)
    real_connect = sqlite3.connect
    commits = []
    class UncertainConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql == 'COMMIT':
                commits.append(sql)
                if len(commits) == 1:
                    raise sqlite3.OperationalError('private-token-must-not-escape')
            return result
    def connect(*args, **kwargs):
        return real_connect(*args, **(kwargs | dict(factory=UncertainConnection)))
    monkeypatch.setattr(sqlite3, 'connect', connect)
    writer = LPBusinessWriter(store)
    try:
        writer.offer(request('F1'))
        status = wait_status(writer, lambda s: s.get('unknown_outcome_entries') == 1 or not s['thread_alive'])
        assert status.get('failed_entries') == 1 and status.get('unknown_outcome_entries') == 1
        assert [key for key, _ in stored_actions(store)] == ['F1']  # commit fact survives raised result
        writer.offer(request('F2'))
        wait_status(writer, lambda s: s['saved_entries'] == 1)
        assert [key for key, _ in stored_actions(store)] == ['F1', 'F2']
        assert len(commits) == 2  # no automatic retry of uncertain F1
        assert 'private-token-must-not-escape' not in '\n'.join(
            r.getMessage() for r in caplog.records if r.name == 'open_trader.lp_business_writer')
    finally:
        writer.close(timeout=3)


def test_close_drops_pending_and_waits_for_actual_writer(tmp_path, monkeypatch):
    from open_trader.lp_business_writer import LPBusinessWriter
    store = PredictionArbitrageStore(tmp_path)
    blocker, entered = blocked_database(store, monkeypatch)
    writer = LPBusinessWriter(store)
    try:
        current = request('C1')
        retained = weakref.ref(current)
        writer.offer(current)
        del current
        assert entered.wait(2)
        assert retained() is not None
        writer.offer(request('C2'))
        with pytest.raises(TimeoutError): writer.close(timeout=0)
        state = writer.status()
        assert state['state'] == 'STOPPING' and state['thread_alive'] is True
        assert state['inflight_entries'] == 1 and state['pending_entries'] == state['pending_bytes'] == 0
        assert state.get('dropped_shutdown') == 1
        writer.offer(request('C3'))
        assert writer.status().get('dropped_closed') == 1
        blocker.execute('COMMIT')
        writer.close(timeout=3)
        writer.close(timeout=0)
        state = writer.status()
        assert state['state'] == 'STOPPED' and state['thread_alive'] is False
        assert state['inflight_entries'] == state['inflight_bytes'] == state['pending_entries'] == 0
        assert state['saved_entries'] == 1 and state['dropped_shutdown'] == 1
        assert [key for key, _ in stored_actions(store)] == ['C1']
        assert retained() is None  # actual completed writer releases its payload
    finally:
        if blocker.in_transaction: blocker.execute('ROLLBACK')
        blocker.close()
        writer.close(timeout=3)


def test_record_parent_matching_and_current_api_progress_are_independent_of_receipt_history(tmp_path):
    from open_trader.lp_business_writer import LPBusinessWriter, ReceiptRecord, ProgressRecord
    store = PredictionArbitrageStore(tmp_path)
    writer = LPBusinessWriter(store)
    try:
        writer.offer(ReceiptRecord(account_id='account-A', process_epoch='epoch-1', request_id='missing',
            session_id='session-1', condition_id='c1', token_id='t1', order_id='order-A',
            outcome='accepted', received_at=STAMP))
        wait_status(writer, lambda s: s.get('dropped_parent') == 1)
        assert stored_actions(store) == [] and store.lp_sessions() == []
        writer.offer(request('R'))
        wait_status(writer, lambda s: s['saved_entries'] == 1)
        evidence = dict(account_id='account-A', process_epoch='epoch-1', request_id='R',
            session_id='session-1', condition_id='c1', token_id='t1', order_id='order-A',
            original_size=Decimal('10'), size_matched=Decimal('3'), price=Decimal('.40'),
            status='LIVE', checked_at=STAMP)
        writer.offer(ProgressRecord(**evidence))
        wait_status(writer, lambda s: s['saved_entries'] == 2)
        [(identity, payload)] = stored_actions(store)
        assert identity == 'R' and payload['quantity'] == '10'
        assert payload['progress']['remaining'] == '7' and payload['progress']['reserved_usd'] == '2.80'
        assert 'receipt' not in payload  # later API does not invent lost receipt history
        for changes in (dict(account_id='account-B'), dict(order_id='wrong-order'),
                        dict(process_epoch='other-epoch'), dict(status='ABSENT'),
                        dict(price=Decimal('.41')), dict(original_size=Decimal('11'))):
            writer.offer(ProgressRecord(**(evidence | changes)))
        wait_status(writer, lambda s: s['dropped_parent'] == 7)
        assert stored_actions(store)[0][1] == payload
        writer.offer(ReceiptRecord(account_id='account-A', process_epoch='epoch-1', request_id='R',
            session_id='session-1', condition_id='c1', token_id='t1', order_id='wrong-order',
            outcome='accepted', received_at=STAMP))
        wait_status(writer, lambda s: s['dropped_parent'] == 8)
        assert stored_actions(store)[0][1] == payload
        # Truthful matching receipt may be logged later without erasing API progress.
        writer.offer(ReceiptRecord(account_id='account-A', process_epoch='epoch-1', request_id='R',
            session_id='session-1', condition_id='c1', token_id='t1', order_id='order-A',
            outcome='accepted', received_at=STAMP))
        wait_status(writer, lambda s: s['saved_entries'] == 3)
        saved = stored_actions(store)[0][1]
        assert saved['progress'] == payload['progress'] and saved['receipt']['outcome'] == 'accepted'
        assert store.lp_trade_generation() == 0
    finally:
        writer.close(timeout=3)


def test_narrow_control_save_preserves_unrelated_fields_and_requires_commit(tmp_path, monkeypatch):
    from threading import Thread
    store = PredictionArbitrageStore(tmp_path)
    enabled = datetime(2026, 10, 15, tzinfo=UTC)
    original = dict(account_id='account-A', desired_running=True, ever_enabled=True,
        enabled_at=enabled.isoformat(), updated_at=enabled.isoformat(), marker='original',
        config_version=9, allocations=[dict(token_id='untouched')])
    with sqlite3.connect(store.path) as connection:
        connection.execute('INSERT INTO lp_auto_pool VALUES(1,?)', (json.dumps(original),))
    blocker, entered = blocked_database(store, monkeypatch)
    blocker.execute('UPDATE lp_auto_pool SET payload=json_set(payload,\'$.marker\',\'concurrent\') WHERE singleton=1')
    write = dict(account_id='account-A', process_epoch='epoch-1', request_version=7,
        request_id='C7', desired_running=False, requested_at=STAMP, actor='operator')
    completed = Event()
    results, errors = [], []
    def save():
        try: results.append(store.lp_save_current_control(write))
        except Exception as exc: errors.append(exc)
        finally: completed.set()
    thread = Thread(target=save)
    thread.start()
    try:
        assert entered.wait(2), errors
        assert not completed.is_set() and results == []
        release_at = datetime.now(UTC)
        blocker.execute('COMMIT')
        assert completed.wait(3)
        assert errors == []
        [receipt] = results
        assert receipt['request_id'] == 'C7' and receipt['request_version'] == 7
        assert receipt['desired_running'] is False
        assert receipt['committed_at'] >= release_at
        with pytest.raises(TypeError): receipt['desired_running'] = True
        loaded = store.lp_load_current_control('account-A')
        assert loaded['account_id'] == 'account-A' and loaded['desired_running'] is False
        assert loaded['ever_enabled'] is True and loaded['enabled_at'] == enabled
        assert loaded['updated_at'].tzinfo is not None
        with sqlite3.connect(store.path) as connection:
            document = json.loads(connection.execute('SELECT payload FROM lp_auto_pool').fetchone()[0])
        assert document['marker'] == 'concurrent' and document['config_version'] == 9
        assert document['allocations'] == original['allocations']
        assert store.lp_trade_generation() == 0
        with pytest.raises(ValueError): store.lp_load_current_control('account-B')
        for changes in (dict(account_id='account-B'), dict(request_version=True),
                        dict(desired_running='yes'), dict(request_id=''), dict(process_epoch='')):
            with pytest.raises(ValueError): store.lp_save_current_control(write | changes)
        with sqlite3.connect(store.path) as connection:
            connection.execute("UPDATE lp_auto_pool SET payload=json_set(payload,'$.desired_running','yes')")
        with pytest.raises(ValueError): store.lp_load_current_control('account-A')
        with pytest.raises(ValueError): store.lp_save_current_control(write)
    finally:
        if blocker.in_transaction: blocker.execute('ROLLBACK')
        blocker.close()
        thread.join(3)
        assert not thread.is_alive()


def test_audit_containers_are_absent_from_legacy_session_views(tmp_path):
    from open_trader.lp_business_writer import LPBusinessWriter
    from open_trader.prediction_arbitrage_store import LP_RESERVED_MANUAL_SESSION_ID

    store = PredictionArbitrageStore(tmp_path / 'mixed')
    for sid, state, payload in (
        ('real-complete', 'complete', dict(account_id='account-A', condition_id='c1', token_id='t1')),
        ('real-active', 'entry_open', dict(account_id='account-A', condition_id='c1', token_id='t1')),
        (LP_RESERVED_MANUAL_SESSION_ID, 'complete', dict(context='manual_cancel_audit')),
        ('numeric-marker', 'complete', dict(d2_audit_only=1)),
        ('text-marker', 'complete', dict(d2_audit_only='true')),
        ('false-marker', 'complete', dict(d2_audit_only=False)),
    ):
        store.lp_create_session(sid, 'key-' + sid, state=state, payload=payload)
    # Give every existing row a controlled older creation time; audit is newest.
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE lp_sessions SET created_at='2026-10-15T00:00:00+00:00'")
    def views(db):
        return (db.lp_latest_session(), db.lp_sessions(), db.lp_active_sessions(),
                db.lp_active_sessions_with_revisions())
    before = views(store)
    assert any(s['session_id'] == 'real-complete' for s in before[1])
    assert any(s['session_id'] == LP_RESERVED_MANUAL_SESSION_ID for s in before[1])
    assert before[0]['session_id'] != LP_RESERVED_MANUAL_SESSION_ID
    generation = store.lp_trade_generation()
    notifications = []
    store._lp_trade_change_listener = notifications.append
    writer = LPBusinessWriter(store)
    try:
        writer.offer(request('audit-request', session_id='audit-container'))
        wait_status(writer, lambda s: s['saved_entries'] == 1)
        assert views(store) == before
        exact = store.lp_session('audit-container')
        assert exact['state'] == 'complete' and exact['d2_audit_only'] is True
        assert exact['process_epoch'] == 'epoch-1'
        with sqlite3.connect(store.path) as connection:
            root_before = connection.execute('SELECT state,payload FROM lp_sessions WHERE session_id=?',
                ('audit-container',)).fetchone()
            assert set(json.loads(root_before[1])) == {
                'account_id', 'process_epoch', 'condition_id', 'token_id', 'd2_audit_only'}
            assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        # Same group and exact existing legacy session ID is still not an audit parent.
        legacy_before = store.lp_session('real-complete')
        writer.offer(request('legacy-collision', session_id='real-complete'))
        wait_status(writer, lambda s: s.get('dropped_parent') == 1)
        assert store.lp_session('real-complete') == legacy_before
        assert store.lp_actions('real-complete') == []
        # A genuine marked complete parent in this process/group accepts another record.
        writer.offer(request('audit-request-2', session_id='audit-container'))
        wait_status(writer, lambda s: s['saved_entries'] == 2)
        for changes in (dict(account_id='account-B'), dict(process_epoch='other-epoch'),
                        dict(condition_id='other-condition'), dict(token_id='other-token')):
            writer.offer(request('identity-collision', session_id='audit-container', **changes))
        wait_status(writer, lambda s: s['dropped_parent'] == 5)
        # Even a marked row is not borrowable when it has legacy state or ownership fields.
        for state, extra in (('entry_rejected', {}), ('complete', dict(order_history={})),
                             ('complete', dict(queue_protection={}))):
            with sqlite3.connect(store.path) as connection:
                values = json.loads(root_before[1]) | extra
                connection.execute('UPDATE lp_sessions SET state=?,payload=? WHERE session_id=?',
                    (state, json.dumps(values), 'audit-container'))
            writer.offer(request('invalid-parent', session_id='audit-container'))
            expected = 6 + (0 if state == 'entry_rejected' else 1 if 'order_history' in extra else 2)
            wait_status(writer, lambda s: s['dropped_parent'] == expected)
        with sqlite3.connect(store.path) as connection:
            connection.execute('UPDATE lp_sessions SET state=?,payload=? WHERE session_id=?',
                (*root_before, 'audit-container'))
        assert len(store.lp_actions('audit-container')) == 2
        assert views(store) == before
        assert store.lp_trade_generation() == generation and notifications == []
    finally:
        writer.close(timeout=3)

    only = PredictionArbitrageStore(tmp_path / 'only-audit')
    writer = LPBusinessWriter(only)
    try:
        writer.offer(request('only-audit', session_id='only-audit-container'))
        wait_status(writer, lambda s: s['saved_entries'] == 1)
        assert views(only) == (None, [], [], [])
        assert len(only.lp_actions('only-audit-container')) == 1
    finally:
        writer.close(timeout=3)


def test_audit_write_during_account_registration_preserves_real_fences(tmp_path):
    from threading import Thread
    from open_trader.lp_business_writer import LPBusinessWriter
    from tests.test_lp_order_registration_contract import (
        _runtime, _SDKPublicClient, _open_order, WALLET, CONDITION_ID, TOKEN_ID, NOW,
    )
    entered, release = Event(), Event()
    class GatedPublicClient(_SDKPublicClient):
        def get_order_books(self, *, token_ids):
            entered.set()
            assert release.wait(3), 'external book release watchdog'
            return super().get_order_books(token_ids=token_ids)
    order_id = 'exact-current-buy'
    store, adapter, account, lp, execution = _runtime(tmp_path,
        orders=(_open_order(order_id, 'BUY', price='.40', original='10'),),
        public_client=GatedPublicClient(NOW))
    writer = LPBusinessWriter(store)
    completed = Event()
    results, errors = [], []
    snapshot = adapter.lp_account_snapshot_shared(max_age_seconds=0,
        trade_generation_provider=store.lp_trade_generation)
    assert snapshot['account_id'] == WALLET
    assert all(snapshot[key] is True for key in ('balance_complete', 'open_orders_complete',
        'trades_complete', 'positions_complete', 'pagination_complete'))
    generation = store.lp_trade_generation()
    def register():
        try: results.append(lp.register_account_snapshot(snapshot))
        except Exception as exc: errors.append(exc)
        finally: completed.set()
    thread = Thread(target=register)
    thread.start()
    try:
        assert entered.wait(2), errors
        assert not completed.is_set()
        writer.offer(request('concurrent-audit', account_id=WALLET, condition_id=CONDITION_ID,
            token_id=TOKEN_ID, session_id='unrelated-audit', order_id='historical-audit-order'))
        wait_status(writer, lambda s: s['saved_entries'] == 1)
        audit_before = store.lp_session('unrelated-audit')
        assert store.lp_trade_generation() == generation
        release.set()
        assert completed.wait(3), 'registration completion watchdog'
        assert errors == []
        assert results[0]['state'] == 'registered', results[0]
        assert store.lp_trade_generation() == generation
        dashboard = execution.refresh_lp_dashboard_snapshot()
        assert dashboard['state'] == 'ready', dashboard
        assert {row['order_id'] for row in dashboard['orders']} == {order_id}
        assert {row['management'] for row in dashboard['orders']} == {'system_managed'}
        [real] = store.lp_sessions()
        assert real['session_id'] != 'unrelated-audit'
        assert real['account_id'] == WALLET and real['condition_id'] == CONDITION_ID
        assert real['token_id'] == TOKEN_ID and real['owned_order_ids'] == [order_id]
        store.lp_recover_interrupted_submissions()
        assert store.lp_trade_generation() == generation
        assert store.lp_session('unrelated-audit') == audit_before
        assert len(store.lp_actions('unrelated-audit')) == 1
        with sqlite3.connect(store.path) as connection:
            document = json.loads(connection.execute('SELECT payload FROM lp_auto_pool').fetchone()[0])
        facts = document['account_financial_facts']
        assert facts['financial_status'] == 'known'
        assert facts['pending_buy_actions'] == [] and facts['retained_reservation_ids'] == []
        assert len(facts['buys']) == 1 and facts['buys'][0]['order_id'] == order_id
        assert Decimal(str(facts['buys'][0]['quantity'])) == Decimal('10')
        assert Decimal(str(facts['buys'][0]['price'])) == Decimal('.40')
        assert account.posts == account.cancels == []
        # Real trading state still invalidates the earlier captured account fence.
        stale = adapter.lp_account_snapshot_shared(max_age_seconds=0,
            trade_generation_provider=store.lp_trade_generation)
        store.lp_register_trade_change(real['session_id'])
        invalid = lp.register_account_snapshot(stale)
        assert invalid['state'] == 'skipped' and invalid['reason'] == 'account_round_invalid'
        assert store.lp_trade_generation() == generation + 1
        assert store.lp_session('unrelated-audit') == audit_before
    finally:
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        writer.close(timeout=3)
        adapter.close()
