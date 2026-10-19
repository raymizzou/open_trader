"""Frozen base/fix contention contract; only pre-existing public LP ports.

Keep this file and its literal workload byte-identical for baseline and fix.
The child owns only its temporary database; its real-time watchdog is separate
from business clocks and from every writer participating in the scenario.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import multiprocessing
from pathlib import Path
import socket
import sqlite3
import threading
import time
import traceback
from types import SimpleNamespace

from open_trader.lp_business_writer import LPBusinessWriter, RequestRecord, ReceiptRecord
from open_trader.lp_metadata_writer import LPMetadataWriter
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


class _Rewards:
    def lp_reward_snapshot(self, reward_date, condition_id):
        return dict(state='known', reward_date=reward_date, condition_id=condition_id,
                    account_amount=Decimal('1.1'), market_amount=Decimal('.62'))


class _Account:
    wallet_address = 'fcore-offline-account'
    config = SimpleNamespace(wallet_address=wallet_address)

    def lp_open_orders_snapshot(self):
        return dict(authenticated=True, open_orders_complete=True, checked_at=datetime.now(UTC),
                    open_orders=(dict(order_id='share-buy', condition_id='share-condition',
                        market_title='Offline BUY', token_id='share-token', side='BUY', status='LIVE',
                        price=Decimal('.4'), original_size=Decimal('20'), size_matched=Decimal('5')),))

    def lp_reward_percentages(self):
        return dict(state='known', scope='account', maker_address=self.wallet_address,
                    percentages={'share-condition': Decimal('8.5')}, checked_at=datetime.now(UTC))


class _Notifier:
    def __init__(self):
        self.calls = []

    def notify(self, title, message):
        self.calls.append((title, message))


def _scenario(root):
    def forbidden_network(*args, **kwargs):
        raise AssertionError('offline contention fixture attempted network access')
    socket.socket.connect = forbidden_network
    start = time.monotonic()
    now = datetime.now(UTC)
    store = PredictionArbitrageStore(Path(root))
    store.initialize_lp_metadata_cache()
    store.lp_save_preparation(dict(state='partial', stage='history', generation=1, paused=False))
    store.lp_create_session('reward', 'reward-key', state='entry_open', payload=dict(
        condition_id='reward-condition', reward_date=now.date().isoformat(),
        paid_rewards=Decimal(0), trade_pnl=Decimal(0)))
    for identity, error, failed_at in (
        ('due', 'TransportError', now),
        ('future', 'TransportError', now + timedelta(seconds=600)),
        ('paused', 'RequestRejectedError', now),
        ('recovered', 'TransportError', now),
    ):
        store.lp_record_preparation_failure(identity, generation=1, stage='history',
                                            error=error, failed_at=failed_at)
    store.lp_recover_preparation_items(('recovered',))
    store.lp_record_preparation_failure('other-generation', generation=2, stage='metadata',
                                        error='TransportError', failed_at=now + timedelta(seconds=600))
    controls = {row['condition_id']: row for row in store.lp_preparation_items() if row['condition_id'] != 'due'}
    assert store.lp_try_acquire_preparation_owner()
    rewards = PolymarketLPService(store, _Rewards(), clock=lambda: now)
    notifier = _Notifier()
    shares = PredictionExecutionService(store=store, monitor=object(), trading=_Account(),
                                        notifier=notifier, lock_path=Path(root) / 'offline.lock')
    metadata, business = LPMetadataWriter(store), LPBusinessWriter(store)
    entered, release = threading.Event(), threading.Event()
    real_connect = sqlite3.connect
    pause_once = threading.Event()

    class _NativeBoundary(sqlite3.Connection):
        def executemany(self, sql, parameters):
            if 'INSERT OR REPLACE INTO lp_market_competitiveness' in sql and not pause_once.is_set():
                pause_once.set()
                rows = iter(parameters)
                first = next(rows)
                super().executemany(sql, (first,))
                entered.set()
                if not release.wait(20):
                    raise TimeoutError('native holder independent watchdog')
                return super().executemany(sql, rows)
            return super().executemany(sql, parameters)

    def connect(*args, **kwargs):
        return real_connect(*args, **(kwargs | dict(factory=_NativeBoundary)))

    errors, results = {}, {}
    error_lock = threading.Lock()
    ready = threading.Barrier(4)

    def call(name, operation):
        ready.wait(timeout=10)
        try:
            value = operation()
            with error_lock:
                results[name] = value
        except Exception as error:
            with error_lock:
                errors[name] = dict(type=type(error).__name__, code=getattr(error, 'sqlite_errorcode', None),
                                    message=str(error))

    def competition():
        for batch in range(2):
            rows = ((f'competition-{batch}-{i}', Decimal('.1234567890123456789'), now) for i in range(500))
            store.lp_competitiveness_upsert(rows, progress={'next_start_cursor': f'cursor-{batch + 1}'})

    timer = None
    sqlite3.connect = connect
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            holder = pool.submit(competition)
            assert entered.wait(10), 'competition did not reach native INSERT'
            timer = threading.Timer(6.2, release.set)
            timer.start()
            work = [pool.submit(call, 'reward', lambda: rewards.refresh_rewards('reward')),
                    pool.submit(call, 'share', shares.refresh_lp_share_watch),
                    pool.submit(call, 'claim', lambda: store.lp_claim_preparation_retries(
                        now=now + timedelta(seconds=301), condition_ids=('due', *controls)))]
            expiry = now.timestamp() + 43200
            metadata.enqueue({f'metadata-{i}': (expiry, dict(condition_id=f'metadata-{i}')) for i in range(32)})
            for i in range(4):
                identity = dict(account_id='audit-account', process_epoch='frozen-epoch', request_id=f'request-{i}',
                                session_id='audit-session', condition_id='audit-condition', token_id='audit-token')
                business.offer(RequestRecord(**identity, kind='BUY', price=Decimal('.4'), quantity=Decimal('10'),
                                             requested_at=now, order_id=f'order-{i}'))
                business.offer(ReceiptRecord(**identity, order_id=f'order-{i}', outcome='accepted', received_at=now))
            ready.wait(timeout=10)
            for future in work:
                future.result(timeout=30)
            holder.result(timeout=30)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            states = [metadata.status(), business.status()]
            if all(s['inflight_entries'] == s['pending_entries'] == 0 for s in states):
                break
            threading.Event().wait(.005)
        else:
            raise AssertionError(f'writer completion watchdog: {states}')
        assert errors == {}, f'synchronous LP failures: {errors}'
        for state in states:
            assert state['failed_entries'] == state['unknown_outcome_entries'] == 0, state
            assert all(value == 0 for key, value in state.items() if key.startswith('dropped_')), state
        assert metadata.status()['saved_entries'] == 32
        assert business.status()['saved_entries'] == 8
        assert set(store.lp_metadata_cache_entries(now=now)) == {f'metadata-{i}' for i in range(32)}
        assert store.lp_competitiveness_count() == 1000
        facts = store.lp_competitiveness_map()
        assert set(facts) == {f'competition-{b}-{i}' for b in range(2) for i in range(500)}
        assert all(value == (Decimal('.1234567890123456789'), now) for value in facts.values())
        assert store.lp_competitiveness_progress()['next_start_cursor'] == 'cursor-2'
        assert [row['condition_id'] for row in results['claim']] == ['due']
        assert store.lp_claim_preparation_retries(now=now + timedelta(seconds=301), condition_ids=('due',)) == []
        after = {row['condition_id']: row for row in store.lp_preparation_items()}
        assert {key: after[key] for key in controls} == controls
        assert Decimal(str(store.lp_session('reward')['reward_observation']['account_amount'])) == Decimal('1.1')
        alert = shares.lp_share_watch_state()['share-condition']
        assert Decimal(str(alert['last_share_percentage'])) == Decimal('8.5')
        assert alert.get('notification_sent') is not True
        assert notifier.calls == []
        with real_connect(store.path) as connection:
            records = {key: json.loads(payload) for key, payload in connection.execute(
                'SELECT action_key,payload FROM lp_actions WHERE session_id=?', ('audit-session',))}
        assert set(records) == {f'request-{i}' for i in range(4)}
        for i in range(4):
            assert records[f'request-{i}']['receipt']['order_id'] == f'order-{i}'
            assert records[f'request-{i}']['price'] == '0.4'
        return dict(seconds=time.monotonic() - start, metadata=metadata.status(), business=business.status())
    finally:
        release.set()
        if timer is not None:
            timer.cancel()
            timer.join()
        metadata.stop(timeout=10)
        business.close(timeout=10)
        store.lp_release_preparation_owner()
        sqlite3.connect = real_connect


def _child(root, output):
    try:
        output.put(('ok', _scenario(root)))
    except BaseException:
        output.put(('error', traceback.format_exc()))


def test_real_lp_writers_survive_finite_native_contention(tmp_path):
    context = multiprocessing.get_context('spawn')
    output = context.Queue()
    child = context.Process(target=_child, args=(str(tmp_path), output))
    started = time.monotonic()
    child.start()
    child.join(timeout=55)
    try:
        assert not child.is_alive(), 'independent 55-second core watchdog expired'
        assert child.exitcode == 0
        status, result = output.get(timeout=2)
        assert status == 'ok', result
        assert time.monotonic() - started < 60
        print('S01_CORE_RESULT', result)
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=3)
        output.close()
        output.join_thread()
