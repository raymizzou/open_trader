"""Log facts follow public offline operations and durable trading truth."""
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from open_trader import polymarket_trading
from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
from tests.test_lp_read_diagnostics import wait_read_logs
from tests.test_polymarket_trading import FakeClient

SECRET = 'SECRET-wallet-order-token-password-301'


@pytest.fixture(autouse=True)
def drain_logs():
    wait_read_logs()
    yield
    wait_read_logs()


def diagnostic_events(caplog, **match):
    wait_read_logs()
    rows = [json.loads(record.getMessage().removeprefix('lp_diagnostic '))
            for record in caplog.records if record.getMessage().startswith('lp_diagnostic ')]
    assert SECRET not in caplog.text
    return [row for row in rows if all(row.get(k) == v for k, v in match.items())]


def test_selected_reward_failure_logs_safe_cause_and_identity(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    from open_trader import polymarket_lp_diagnostics
    episode_time = [0.0]
    monkeypatch.setattr(polymarket_lp_diagnostics, 'monotonic', lambda: episode_time[0])
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(polymarket_trading, 'datetime', Frozen)
    failed = {False, True}
    reads, closes = [], []
    class Public:
        def list_market_rewards(self, *, condition_id, sponsored):
            reads.append((condition_id, sponsored))
            if sponsored in failed:
                try:
                    raise httpx.ReadTimeout(SECRET)
                except httpx.ReadTimeout as exc:
                    raise httpx.TransportError(SECRET) from exc
            return ()
        def close(self):
            closes.append(True)
    adapter = PolymarketTradingClient(TradingConfig(SECRET, SECRET), FakeClient(), public_client_factory=Public)
    try:
        first = adapter.lp_reward_catalog(condition_ids=(SECRET,))
        assert first['complete'] is False
        assert 'reward_read_failed' in first['markets'][0]['reason_codes']
        assert first['markets'][0]['error_type'] == 'TransportError'
        failures = diagnostic_events(caplog, event='failed')
        assert {r['stage'] for r in failures} == {'reward_native', 'reward_sponsored'}
        assert all(r['exception_types'] == ['TransportError', 'ReadTimeout'] for r in failures)
        assert all(r['event_at'] == now.isoformat() and r['cause_code'] == 'reward_read_failed' for r in failures)
        assert len({r['scope_id'] for r in failures}) == 2
        assert all(r['attempt_id'] != 'unknown' for r in failures)
        episode_time[0] = 59
        adapter.lp_reward_catalog(condition_ids=(SECRET,))
        assert len(diagnostic_events(caplog, event='failed')) == 2
        failed.remove(False)
        adapter.lp_reward_catalog(condition_ids=(SECRET,))
        recovered = diagnostic_events(caplog, event='recovered')
        assert len(recovered) == 1 and recovered[0]['stage'] == 'reward_native'
        assert recovered[0]['scope_id'] == next(r['scope_id'] for r in failures if r['stage'] == 'reward_native')
        episode_time[0] = 60
        adapter.lp_reward_catalog(condition_ids=(SECRET,))
        summary, = diagnostic_events(caplog, event='failed', stage='reward_sponsored', failure_count=4)
        assert summary['repeat_count'] == 3
        failed.clear()
        assert adapter.lp_reward_catalog(condition_ids=(SECRET,))['complete'] is True
        recovered = diagnostic_events(caplog, event='recovered')
        assert len(recovered) == 2
        sponsored = next(r for r in recovered if r['stage'] == 'reward_sponsored')
        assert sponsored['first_cause_code'] == sponsored['latest_cause_code'] == 'reward_read_failed'
        assert sponsored['failure_count'] == 4
        assert sponsored['first_exception_types'] == ['TransportError', 'ReadTimeout']
        assert len(reads) == len(closes) == 10
    finally:
        adapter.close()


from copy import deepcopy
from decimal import Decimal
from tests.test_lp_account_reservation_reconciliation import runtime
from tests.test_lp_account_immutable_facts import fresh_snapshot
from tests.test_lp_order_registration_contract import _open_order


def test_account_publication_result_logs_specific_reason(runtime, caplog):
    caplog.set_level(logging.INFO)
    order_id = "order-identity-301"
    original = _open_order(order_id, 'BUY', price='0.40', original='20')
    store, adapter, account, lp, execution = runtime(orders=(original,))
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    before = deepcopy(execution._auto_pool._read())
    sid = store.lp_active_sessions()[0]['session_id']
    for conflict, reason in [({'original': '10'}, 'order_original_quantity_conflict'),
                             ({'price': '0.20'}, 'order_limit_price_conflict')]:
        account.orders = (_open_order(order_id, 'BUY', **{'price': '0.40', 'original': '20', **conflict}),)
        result = lp.register_account_snapshot(fresh_snapshot(runtime, adapter, store))
        assert result['state'] == 'failed' and result['reason'] == reason
        assert execution._auto_pool._read() == before
        durable = store.lp_session(sid)['order_history'][order_id]
        assert Decimal(durable['quantity']) == 20 and Decimal(durable['price']) == Decimal('0.4')
        state = execution.lp_auto_state()
        assert Decimal(state['funds']['buy_reserved_usd']) == 8
        assert 'account_order_sync_unknown' in state['admission_block_reasons']
        rows = diagnostic_events(caplog, event='failed', stage='account_publish', cause_code=reason)
        assert len(rows) == 1
        assert order_id not in caplog.text
        assert adapter.config.wallet_address not in caplog.text
        assert rows[0]['summary_reason'] == 'account_order_sync_unknown'
        assert rows[0]['request_id'] == 'unknown' and 'post_started' not in rows[0]
    account.orders = (original,)
    assert lp.register_account_snapshot(fresh_snapshot(runtime, adapter, store))['state'] == 'registered'
    recovered, = diagnostic_events(caplog, event='recovered', stage='account_publish')
    assert recovered['first_cause_code'] == 'order_original_quantity_conflict'
    assert recovered['latest_cause_code'] == 'order_limit_price_conflict'
    assert recovered['failure_count'] == 2
    assert account.posts == account.cancels == []


def test_failure_wait_and_recovery_remain_distinct(runtime, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    store, adapter, account, lp, execution = runtime()
    valid = fresh_snapshot(runtime, adapter, store)
    native = account.list_account_trades
    def failure(**kwargs):
        raise httpx.ReadTimeout(SECRET)
    monkeypatch.setattr(account, 'list_account_trades', failure)
    execution.lp_auto_reconcile_unknown()
    failed, = diagnostic_events(caplog, event='failed', stage='account_read')
    assert failed['exception_types'][-1] == 'ReadTimeout'
    assert failed['event_at'] == runtime.clock[0].isoformat()
    assert failed['summary_reason'] == 'account_order_sync_unknown'
    monkeypatch.setattr(account, 'list_account_trades', native)
    runtime.clock[0] += timedelta(seconds=1)
    store.lp_advance_trade_generation(store.lp_trade_generation())
    assert lp.register_account_snapshot(valid)['reason'] == 'account_round_invalid'
    waiting, = diagnostic_events(caplog, event='waiting', stage='account_publish')
    assert waiting['cause_code'] == 'account_round_invalid'
    assert waiting['first_cause_code'] == failed['cause_code']
    assert waiting['scope_id'] == failed['scope_id'] and waiting['episode_id'] == failed['episode_id']
    assert waiting['attempt_id'] != failed['attempt_id']
    wait_record, = [r for r in caplog.records if r.getMessage().startswith('lp_diagnostic ')
                    and json.loads(r.getMessage()[14:]).get('event') == 'waiting']
    assert wait_record.levelno == logging.INFO
    # Public Auto planning uses another recovery scope, even with complete history.
    execution.lp_auto_run_once(round_id=SECRET)
    assert not diagnostic_events(caplog, event='recovered', scope_id=failed['scope_id'])
    assert lp._account_order_sync_error is not None
    runtime.clock[0] += timedelta(seconds=1)
    assert lp.register_account_snapshot(fresh_snapshot(runtime, adapter, store))['state'] == 'registered'
    recovered, = diagnostic_events(caplog, event='recovered', scope_id=failed['scope_id'])
    assert recovered['episode_id'] == failed['episode_id']
    assert recovered['failure_count'] == 2
    assert recovered['first_stage'] == 'account_read' and recovered['latest_stage'] == 'account_publish'
    assert recovered['first_cause_code'] == failed['cause_code']
    assert recovered['latest_cause_code'] == 'account_round_invalid'
    assert recovered['first_event_at'] == failed['event_at']
    assert recovered['latest_event_at'] == waiting['event_at']
    assert recovered['event_at'] == runtime.clock[0].isoformat()
    native_balance = account.get_balance_allowance
    def planning_read_failure(**kwargs):
        raise httpx.ReadTimeout(SECRET)
    monkeypatch.setattr(account, 'get_balance_allowance', planning_read_failure)
    runtime.clock[0] += timedelta(seconds=1)
    execution.lp_auto_run_once(round_id='planning-read-failure')
    planning_failure, = diagnostic_events(caplog, event='failed', stage='account_read', scope='account_planning')
    assert planning_failure['component'] == 'balance'
    assert planning_failure['exception_types'][-1] == 'ReadTimeout'
    assert planning_failure['cause_code'] == 'account_read_failed'
    assert planning_failure['scope_id'] != failed['scope_id']
    assert not diagnostic_events(caplog, event='recovered', scope_id=planning_failure['scope_id'])
    monkeypatch.setattr(account, 'get_balance_allowance', native_balance)
    runtime.clock[0] += timedelta(seconds=61)
    execution.lp_auto_run_once(round_id='planning-recovery')
    planning_recovery, = diagnostic_events(caplog, event='recovered', scope_id=planning_failure['scope_id'])
    assert planning_recovery['first_cause_code'] == 'account_read_failed'
    assert planning_recovery['first_exception_types'][-1] == 'ReadTimeout'
    assert account.posts == account.cancels == []


def test_older_success_cannot_close_newer_diagnostic_failure(runtime, monkeypatch, caplog):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread
    caplog.set_level(logging.INFO)
    store, adapter, account, lp, execution = runtime()
    assert lp.register_account_snapshot({'authenticated': False})['reason'] == 'account_snapshot_unknown'
    for newer_first in (True, False):
        old_snapshot = fresh_snapshot(runtime, adapter, store)
        entered, release = Event(), Event()
        class Connection(sqlite3.Connection):
            def execute(self, sql, *args):
                if ('SELECT session_id,json_extract' in sql
                        and current_thread().name.startswith('older-public') and not entered.is_set()):
                    entered.set()
                    assert release.wait(5), 'Independent native SQLite admission watchdog'
                return super().execute(sql, *args)
        def connect():
            connection = sqlite3.connect(store.path, isolation_level=None, factory=Connection)
            connection.row_factory = sqlite3.Row
            return connection
        with monkeypatch.context() as patch:
            patch.setattr(store, '_connection', connect)
            with ThreadPoolExecutor(1, thread_name_prefix='older-public') as workers:
                old = workers.submit(lp.register_account_snapshot, old_snapshot)
                try:
                    assert entered.wait(3)
                    before = diagnostic_events(caplog, event='recovered')
                    if not newer_first:
                        release.set()
                        assert old.result(timeout=3)['state'] == 'registered'
                    runtime.clock[0] += timedelta(seconds=1)
                    new = lp.register_account_snapshot({**old_snapshot, 'trade_generation': -1})
                    assert new['reason'] == 'account_round_invalid'
                    waiting = diagnostic_events(caplog, event='waiting')[-1]
                finally:
                    release.set()
                assert old.result(timeout=3)['state'] == 'registered'
        if newer_first:
            assert diagnostic_events(caplog, event='recovered') == before
            late, = diagnostic_events(caplog, event='complete', current_attempt=False)
            assert late['attempt_id'] != waiting['attempt_id']
            assert late['scope_id'] == waiting['scope_id']
        else:
            # An older success completed before B was admitted can close only
            # the prior episode. B then starts a new waiting episode.
            assert diagnostic_events(caplog, event='recovered')[-1]['event_at'] <= waiting['event_at']
        assert lp.register_account_snapshot(fresh_snapshot(runtime, adapter, store))['state'] == 'registered'
        recovered = diagnostic_events(caplog, event='recovered')[-1]
        assert recovered['episode_id'] == waiting['episode_id']
        assert recovered['latest_cause_code'] == 'account_round_invalid'
        assert recovered['attempt_id'] != waiting['attempt_id']
    assert execution.lp_auto_state()['funds']['status'] == 'known'
    assert account.posts == account.cancels == []


def test_submission_logs_match_durable_send_boundary(tmp_path, runtime, monkeypatch, caplog):
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from tests.test_polymarket_lp import _Exchange, _lp163_book, _request
    from tests.test_lp_auto_refill_contract import prepare
    from tests.test_lp_auto_plan_execution import _native_unbound_receipt
    from tests.test_lp_account_reservation_reconciliation import _refill_identity
    caplog.set_level(logging.INFO)
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    request_ids = set()
    for mode in ('pre_send', 'timeout', 'venue_reject'):
        exchange = _Exchange()
        exchange.snapshot_value = _lp163_book(now, Decimal('.29'))
        if mode == 'pre_send':
            exchange.snapshot_value['account'].update(balance=Decimal('1'), allowance=Decimal('1'))
        elif mode == 'timeout':
            exchange.post_failures = [httpx.ReadTimeout(SECRET)]
        else:
            exchange.reject_entry = True
            native_post = exchange.post_order
            def reject(signed):
                return {**native_post(signed), 'message': SECRET, 'errorMsg': SECRET,
                        'headers': {'Authorization': SECRET}}
            exchange.post_order = reject
        store = PredictionArbitrageStore(tmp_path / mode)
        lp = PolymarketLPService(store, exchange, clock=lambda: now)
        request = {**_request(now), 'price': Decimal('.29'), 'quantity': Decimal('20'),
                   'candidate_policy': 'best_bid_minimum'}
        store.lp_save_price_history(request['condition_id'], request['token_id'], [],
            {'state': 'known', 'amplitude': Decimal('.005'), 'checked_at': now,
             'valid_until': now + timedelta(days=1)})
        key = SECRET + mode
        caplog.clear()
        first = lp.submit_entry(request, key)
        rows = diagnostic_events(caplog, event='failed')
        result, = [r for r in rows if r.get('result') in ('rejected', 'unknown')]
        assert result['post_started'] is (mode != 'pre_send'), (mode, first)
        assert result['request_id'] != 'unknown'
        request_ids.add(result['request_id'])
        if mode == 'pre_send':
            assert first == {'state': 'rejected', 'reason': 'balance_insufficient'}
            assert result['stage'] == 'entry_validate' and result['result'] == 'rejected'
            assert exchange.posts == [] and store.lp_session_by_idempotency(key) is None
        else:
            audit, = store.lp_actions(first['session_id'])
            assert audit['post_started'] is True
            assert audit['state'] == ('unknown' if mode == 'timeout' else 'rejected')
            assert result['result'] == audit['state'] and result['stage'] == 'entry_send'
            assert datetime.fromisoformat(result['event_at']) == datetime.fromisoformat(audit['submit_finished_at'])
            assert result['cause_code'] == ('send_unknown' if mode == 'timeout' else 'exchange_rejected')
            if mode == 'timeout':
                assert result['exception_types'] == ['ReadTimeout']
            sent, = diagnostic_events(caplog, event='sent')
            assert sent['post_started'] is True and sent['request_id'] == result['request_id']
            assert datetime.fromisoformat(sent['event_at']) == datetime.fromisoformat(audit['submit_post_started_at'])
            baseline = deepcopy(audit)
            count = len(diagnostic_events(caplog))
            second = lp.submit_entry(request, key)
            assert second['state'] == first['state']
            assert len(exchange.posts) == 1 and store.lp_actions(first['session_id']) == [baseline]
            assert len(diagnostic_events(caplog)) == count
    assert len(request_ids) == 3
    caplog.clear()
    store, adapter, account, lp, execution, _ = prepare(runtime, count=2, target=2)
    native_post = account.post_order
    def accepted_with_private_message(signed):
        return {**native_post(signed), 'message': SECRET, 'credentials': SECRET}
    monkeypatch.setattr(account, 'post_order', accepted_with_private_message)
    with _native_unbound_receipt(store, _refill_identity(1)[2]):
        state = execution.lp_auto_run_once(round_id='diag-original-round')
    pending, = [a for a in state['last_round']['actions'] if a['state'] == 'unknown']
    sibling, = [a for a in state['last_round']['actions'] if a['state'] == 'success']
    audit, = store.lp_actions(pending['session_id'])
    assert audit['state'] == 'accepted' and audit['post_started'] is True
    waiting, = diagnostic_events(caplog, event='waiting', stage='receipt_apply')
    assert waiting['result'] == 'accepted' and waiting['post_started'] is True
    assert waiting['cause_code'] == 'receipt_apply_pending'
    assert waiting['round_id'] != 'unknown' and waiting['request_id'] != 'unknown'
    accepted = diagnostic_events(caplog, event='sent', result='accepted')
    matching, = [r for r in accepted if r['request_id'] == waiting['request_id']]
    assert datetime.fromisoformat(matching['event_at']) == datetime.fromisoformat(audit['submit_receipt_at'])
    assert not diagnostic_events(caplog, event='recovered', scope_id=waiting['scope_id'])
    assert len(account.posts) == 2
    original = deepcopy(store.lp_actions(pending['session_id']))
    execution.lp_auto_set_desired_running(False)
    runtime.clock[0] += timedelta(seconds=1)
    execution.refresh_lp_dashboard_snapshot()
    execution.lp_auto_run_once(round_id='apply-original-receipt')
    recovered, = diagnostic_events(caplog, event='recovered', scope_id=waiting['scope_id'])
    assert recovered['request_id'] == waiting['request_id']
    assert recovered['latest_cause_code'] == 'receipt_apply_pending'
    assert store.lp_session(pending['session_id'])['submit_status'] == 'accepted'
    assert store.lp_actions(pending['session_id']) == original
    assert store.lp_actions(sibling['session_id'])[0]['state'] == 'accepted'
    assert len(account.posts) == 2 and account.cancels == []


def test_account_recovery_does_not_resolve_unknown_order(runtime, monkeypatch, caplog):
    from tests.test_lp_auto_refill_contract import prepare
    caplog.set_level(logging.INFO)
    store, adapter, account, lp, execution, _ = prepare(runtime, count=1, target=1)
    def timeout(signed):
        account.posts.append(signed)
        raise httpx.ReadTimeout(SECRET)
    monkeypatch.setattr(account, 'post_order', timeout)
    state = execution.lp_auto_run_once(round_id='unknown-original')
    action, = state['last_round']['actions']
    sid = action['session_id']
    original = deepcopy(store.lp_actions(sid))
    assert original[0]['state'] == 'unknown' and original[0]['post_started'] is True
    unknown, = diagnostic_events(caplog, event='failed', result='unknown')
    native = account.list_account_trades
    def unreadable(**kwargs):
        raise httpx.TransportError(SECRET)
    monkeypatch.setattr(account, 'list_account_trades', unreadable)
    execution.lp_auto_reconcile_unknown()
    failed, = diagnostic_events(caplog, event='failed', stage='account_read')
    monkeypatch.setattr(account, 'list_account_trades', native)
    runtime.clock[0] += timedelta(seconds=1)
    assert lp.register_account_snapshot(fresh_snapshot(runtime, adapter, store))['state'] == 'registered'
    recovered, = diagnostic_events(caplog, event='recovered', scope_id=failed['scope_id'])
    assert recovered['scope'] == 'account_full'
    assert not diagnostic_events(caplog, event='recovered', scope_id=unknown['scope_id'])
    assert store.lp_actions(sid) == original
    assert store.lp_session(sid)['submit_status'] == 'unknown'
    session = store.lp_session(sid)
    request = {k: session[k] for k in ('market_id', 'condition_id', 'token_id', 'outcome', 'price', 'quantity', 'review_at')}
    result = lp.submit_entry(request, session['idempotency_key'])
    assert result['submit_status'] == 'unknown'
    assert store.lp_actions(sid) == original
    assert len(account.posts) == 1 and account.cancels == []
    assert not diagnostic_events(caplog, result='rejected', scope_id=unknown['scope_id'])
    assert not diagnostic_events(caplog, result='accepted', scope_id=unknown['scope_id'])


def test_diagnostic_sink_failure_preserves_business_outcome(tmp_path, runtime, monkeypatch, caplog):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from open_trader import polymarket_lp_diagnostics as diagnostics
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from tests.test_polymarket_lp import _Exchange, _lp163_book, _request
    caplog.set_level(logging.INFO)
    store, adapter, account, lp, execution = runtime()
    valid = fresh_snapshot(runtime, adapter, store)
    failed_sources = [True]
    reads, closes = [], []
    class Public:
        def list_market_rewards(self, *, condition_id, sponsored):
            reads.append((condition_id, sponsored))
            if failed_sources[0]:
                raise httpx.TransportError(SECRET)
            return ()
        def close(self):
            closes.append(True)
    rewards = PolymarketTradingClient(TradingConfig(SECRET, SECRET), FakeClient(), public_client_factory=Public)
    try:
        for blocked in (True, False):
            entered, release = Event(), Event()
            class Sink(logging.Handler):
                def emit(self, record):
                    entered.set()
                    if blocked:
                        assert release.wait(5), 'Independent sink watchdog'
                    raise RuntimeError('offline diagnostic sink unavailable')
            sink = Sink()
            diagnostics.logger.addHandler(sink)
            try:
                first = rewards.lp_reward_catalog(condition_ids=('original-source' if blocked else 'raising-source',))
                assert first['complete'] is False
                assert entered.wait(2)
                with ThreadPoolExecutor(1) as workers:
                    def business():
                        rejected = lp.register_account_snapshot({'authenticated': False})
                        registered = lp.register_account_snapshot(valid)
                        # The sink must not retain a publication transaction.
                        with sqlite3.connect(store.path, timeout=.2) as connection:
                            connection.execute('BEGIN IMMEDIATE')
                            connection.rollback()
                        return rejected, registered
                    rejected, registered = workers.submit(business).result(timeout=2)
                    assert rejected == {'state': 'skipped', 'reason': 'account_snapshot_unknown'}
                    assert registered['state'] == 'registered'
                    # Preserve a genuine escaping business exception by identity.
                    original = RuntimeError(SECRET)
                    class Broken(sqlite3.Connection):
                        def execute(self, sql, *args):
                            if 'SELECT session_id,json_extract' in sql:
                                raise original
                            return super().execute(sql, *args)
                    def connect():
                        connection = sqlite3.connect(store.path, isolation_level=None, factory=Broken)
                        connection.row_factory = sqlite3.Row
                        return connection
                    with monkeypatch.context() as patch:
                        patch.setattr(store, '_connection', connect)
                        future = workers.submit(lp.register_account_snapshot, valid)
                        with pytest.raises(RuntimeError) as observed:
                            future.result(timeout=2)
                        assert observed.value is original
                    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
                    exchange = _Exchange()
                    exchange.snapshot_value = _lp163_book(now, Decimal('.29'))
                    exchange.post_failures = [httpx.ReadTimeout(SECRET)]
                    submission_store = PredictionArbitrageStore(tmp_path / ('blocked' if blocked else 'raising'))
                    service = PolymarketLPService(submission_store, exchange, clock=lambda: now)
                    request = {**_request(now), 'price': Decimal('.29'), 'quantity': Decimal('20')}
                    submission_store.lp_save_price_history(request['condition_id'], request['token_id'], [],
                        {'state': 'known', 'amplitude': Decimal('.005'), 'checked_at': now,
                         'valid_until': now + timedelta(days=1)})
                    sent = workers.submit(service.submit_entry, request, SECRET).result(timeout=2)
                    assert sent['submit_status'] == 'unknown' and len(exchange.posts) == 1
                    audit, = submission_store.lp_actions(sent['session_id'])
                    assert audit['state'] == 'unknown' and audit['post_started'] is True
                    assert service.submit_entry(request, SECRET)['submit_status'] == 'unknown'
                    assert len(exchange.posts) == 1
                    if blocked:
                        result = workers.submit(rewards.lp_reward_catalog,
                            condition_ids=tuple(f'burst-{i}' for i in range(140))).result(timeout=2)
                        assert result['complete'] is False
                        assert polymarket_trading._lp_read_log_queue.qsize() <= 32
                        assert polymarket_trading._lp_read_log_dropped > 0
            finally:
                release.set()
                wait_read_logs()
                diagnostics.logger.removeHandler(sink)
                sink.close()
        assert account.posts == account.cancels == []
        assert len(reads) == len(closes)
        caplog.clear()
        # Original failure was evicted. Success must not invent its recovery.
        failed_sources[0] = False
        rewards.lp_reward_catalog(condition_ids=('original-source',))
        assert not diagnostic_events(caplog, event='recovered')
        failed_sources[0] = True
        rewards.lp_reward_catalog(condition_ids=('new-source-after-overflow',))
        rows = diagnostic_events(caplog, event='failed')
        assert len(rows) == 2
        assert all(r['continuity'] == 'incomplete' and r['evicted_scopes'] >= 28 for r in rows)
        # A native validation exception becomes a returned raw reason in the
        # existing public API. Logging accepts only its controlled fallback.
        class UnsafeReason(sqlite3.Connection):
            def execute(self, sql, *args):
                if self.in_transaction and sql == 'SELECT generation FROM lp_trade_generation WHERE singleton=1':
                    raise ValueError(SECRET * 10000)
                return super().execute(sql, *args)
        def unsafe_connect():
            connection = sqlite3.connect(store.path, isolation_level=None, factory=UnsafeReason)
            connection.row_factory = sqlite3.Row
            return connection
        with monkeypatch.context() as patch:
            patch.setattr(store, '_connection', unsafe_connect)
            result = lp.register_account_snapshot(valid)
        assert result['reason'] == SECRET * 10000
        unknown = diagnostic_events(caplog, cause_code='unknown')
        assert unknown and all(len(json.dumps(r)) < 4096 for r in unknown)
    finally:
        rewards.close()


def test_older_dashboard_read_cannot_recover_newer_auto_failure(runtime, monkeypatch, caplog):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from tests import test_lp_order_registration_contract as sdk
    import time as real_time

    caplog.set_level(logging.INFO)
    entered, release = Event(), Event()
    cache_time = [100.0]
    class CacheClock:
        def monotonic(self):
            return cache_time[0]
        def __getattr__(self, name):
            return getattr(real_time, name)
    monkeypatch.setattr(polymarket_trading, 'time', CacheClock())
    class Public(sdk._SDKPublicClient):
        def get_market(self, *, id):
            if not entered.is_set():
                entered.set()
                assert release.wait(5), 'Independent old Dashboard enrichment watchdog'
            return super().get_market(id=id)
    original = _open_order('dashboard-existing-buy', 'BUY', price='.40', original='20')
    store, adapter, account, lp, execution = runtime(orders=(original,), public_client=Public(runtime.clock[0]))
    execution.lp_auto_configure({'budget_usd': '100', 'target_buy_count': 5})
    old_started = runtime.clock[0]
    native = account.list_account_trades
    with ThreadPoolExecutor(1) as workers:
        old = workers.submit(execution.refresh_lp_dashboard_snapshot)
        try:
            assert entered.wait(3)
            runtime.clock[0] += timedelta(seconds=1)
            def fail(**kwargs):
                raise httpx.ReadTimeout(SECRET)
            monkeypatch.setattr(account, 'list_account_trades', fail)
            execution.lp_auto_reconcile_unknown()
            failure, = diagnostic_events(caplog, event='failed', stage='account_read', scope='account_full')
            assert failure['event_at'] == runtime.clock[0].isoformat()
            assert failure['exception_types'][-1] == 'ReadTimeout'
        finally:
            monkeypatch.setattr(account, 'list_account_trades', native)
            release.set()
        old_result = old.result(timeout=3)
    def drain_dashboard_workers():
        for name in ('_lp_reward_refresh_thread', '_lp_orders_today_refresh_thread'):
            thread = getattr(execution, name)
            if thread is not None:
                thread.join(3)
                assert not thread.is_alive(), 'Independent Dashboard worker cleanup watchdog'
    drain_dashboard_workers()
    assert old_result['state'] == 'ready'
    assert old_result['orders'][0]['order_id'] == 'dashboard-existing-buy'
    assert not diagnostic_events(caplog, event='recovered', scope_id=failure['scope_id'])
    old_completion, = diagnostic_events(caplog, event='complete', stage='account_publish')
    assert old_completion['current_attempt'] is False
    assert old_completion['attempt_id'] != failure['attempt_id']
    assert old_completion['read_started_at'] == old_started.isoformat()
    reads = (account.balance_reads, account.order_reads, account.trade_reads, account.position_reads)
    runtime.clock[0] += timedelta(seconds=1)
    cached = execution.refresh_lp_dashboard_snapshot()
    drain_dashboard_workers()
    assert cached['state'] == 'ready'
    assert (account.balance_reads, account.order_reads, account.trade_reads, account.position_reads) == reads
    assert not diagnostic_events(caplog, event='recovered', scope_id=failure['scope_id'])
    # TTL expiry permits a genuinely newer native read; the old cache was
    # still returned by the previous public call, without altering its API.
    cache_time[0] += 11
    runtime.clock[0] += timedelta(seconds=1)
    fresh = execution.refresh_lp_dashboard_snapshot()
    drain_dashboard_workers()
    assert fresh['state'] == 'ready'
    recovered, = diagnostic_events(caplog, event='recovered', scope_id=failure['scope_id'])
    assert recovered['episode_id'] == failure['episode_id']
    assert recovered['read_started_at'] == runtime.clock[0].isoformat()
    assert account.balance_reads == reads[0] + 1
    assert account.order_reads == reads[1] + 1
    assert Decimal(execution.lp_auto_state()['funds']['buy_reserved_usd']) == 8
    assert account.posts == account.cancels == []


def test_reward_validation_failure_cannot_emit_recovery(caplog):
    caplog.set_level(logging.INFO)
    mode = ['timeout']
    reads, closes = [], []
    condition = 'condition-validation-301'
    def valid(amount):
        return {'condition_id': condition, 'rewards_config': [{
            'id': 301, 'asset_address': '0x2791bca1f2de4661ed88a30c99a7a9449aa84174',
            'start_date': '2020-01-01', 'end_date': '2500-12-31', 'rate_per_day': amount}]}
    class Public:
        def list_market_rewards(self, *, condition_id, sponsored):
            assert condition_id == condition
            reads.append((mode[0], sponsored))
            if mode[0] == 'timeout':
                raise httpx.ReadTimeout(SECRET)
            if mode[0] == 'malformed':
                return ({'condition_id': condition, 'rewards_config': SECRET} if sponsored else
                        {'condition_id': SECRET, 'rewards_config': []},)
            if mode[0] == 'inconsistent':
                return (valid('1' if sponsored else '3'),)
            if mode[0] == 'bad-config':
                return ({'condition_id': condition, 'rewards_config': [{'rate_per_day': SECRET}]}
                        if sponsored else valid('3'),)
            assert mode[0] == 'empty'
            return ()
        def close(self):
            closes.append(True)
    adapter = PolymarketTradingClient(TradingConfig(SECRET, SECRET), FakeClient(), public_client_factory=Public)
    try:
        first = adapter.lp_reward_catalog(condition_ids=(condition,))
        assert first['state'] == 'unknown' and first['complete'] is False
        initial = diagnostic_events(caplog, event='failed')
        assert len(initial) == 2
        mode[0] = 'malformed'
        malformed = adapter.lp_reward_catalog(condition_ids=(condition,))
        assert malformed['state'] == 'unknown' and malformed['daily_pool_usd'] is None
        assert set(malformed['markets'][0]['reason_codes']) == {'reward_identity_unknown', 'reward_config_unknown'}
        assert not diagnostic_events(caplog, event='recovered')
        assert len(diagnostic_events(caplog, event='failed', cause_code='reward_identity_unknown')) == 1
        assert len(diagnostic_events(caplog, event='failed', cause_code='reward_config_unknown')) == 1
        mode[0] = 'inconsistent'
        inconsistent = adapter.lp_reward_catalog(condition_ids=(condition,))
        assert inconsistent['state'] == 'unknown' and inconsistent['complete'] is False
        assert inconsistent['markets'][0]['reason_codes'] == ['reward_totals_inconsistent']
        assert inconsistent['daily_pool_usd'] is None
        native, = diagnostic_events(caplog, event='recovered')
        assert native['stage'] == 'reward_native' and native['failure_count'] == 2
        assert native['latest_cause_code'] == 'reward_identity_unknown'
        assert len(diagnostic_events(caplog, event='failed', cause_code='reward_totals_inconsistent', stage='reward_sponsored')) == 1
        mode[0] = 'bad-config'
        invalid_config = adapter.lp_reward_catalog(condition_ids=(condition,))
        assert invalid_config['state'] == 'unknown'
        assert invalid_config['markets'][0]['reason_codes'] == ['reward_config_unknown']
        assert len(diagnostic_events(caplog, event='recovered')) == 1
        mode[0] = 'empty'
        empty = adapter.lp_reward_catalog(condition_ids=(condition,))
        assert empty['state'] == 'known' and empty['complete'] is True and empty['daily_pool_usd'] == 0
        assert empty['markets'][0]['reward_active'] is False
        sponsored, = diagnostic_events(caplog, event='recovered', stage='reward_sponsored')
        assert sponsored['failure_count'] == 4
        assert sponsored['first_cause_code'] == 'reward_read_failed'
        assert sponsored['latest_cause_code'] == 'reward_config_unknown'
        assert sponsored['scope_id'] == next(r['scope_id'] for r in initial if r['stage'] == 'reward_sponsored')
        assert len(reads) == len(closes) == 10
    finally:
        adapter.close()


def test_component_repeats_obey_log_aggregation_window(runtime, monkeypatch, caplog):
    from open_trader import polymarket_lp_diagnostics

    caplog.set_level(logging.INFO)
    monotonic_time = [0.0]
    monkeypatch.setattr(polymarket_lp_diagnostics, 'monotonic', lambda: monotonic_time[0])
    store, adapter, account, lp, execution = runtime()
    native_balance, native_positions = account.get_balance_allowance, account.list_positions
    calls = {'balance': 0, 'positions': 0}
    def fail_balance(**kwargs):
        calls['balance'] += 1
        raise httpx.ReadTimeout(SECRET)
    def fail_positions(**kwargs):
        calls['positions'] += 1
        raise httpx.ReadTimeout(SECRET)
    monkeypatch.setattr(account, 'get_balance_allowance', fail_balance)
    monkeypatch.setattr(account, 'list_positions', fail_positions)
    for second in range(4):
        monotonic_time[0] = float(second)
        execution.lp_auto_run_once(round_id=f'component-cycle-{second}')
        runtime.clock[0] += timedelta(seconds=1)
    rows = diagnostic_events(caplog, scope='account_planning')
    # Initial transport failures and the subsequent cached waits are distinct
    # states. Each channel emits once; other channels cannot reset its window.
    expected = {
        ('balance', 'account_read', 'failed', 'account_read_failed'),
        ('positions', 'account_read', 'failed', 'account_read_failed'),
        ('balance', 'account_read', 'waiting', 'account_read_cooling_down'),
        ('positions', 'account_read', 'waiting', 'account_read_cooling_down'),
        (None, 'account_publish', 'failed', 'account_snapshot_incomplete'),
    }
    signatures = [(r.get('component'), r['stage'], r['event'], r['cause_code']) for r in rows]
    assert set(signatures) == expected
    assert len(signatures) == 5
    assert calls == {'balance': 1, 'positions': 1}
    assert not diagnostic_events(caplog, event='recovered')
    monotonic_time[0] = 61
    execution.lp_auto_run_once(round_id='component-repeat-summary')
    summaries = diagnostic_events(caplog, scope='account_planning')[len(rows):]
    assert len(summaries) == 3
    assert {(r.get('component'), r['repeat_count']) for r in summaries} == {
        ('balance', 3), ('positions', 3), (None, 4)}
    assert summaries[-1]['failure_count'] == 15
    assert calls == {'balance': 1, 'positions': 1}
    monkeypatch.setattr(account, 'get_balance_allowance', native_balance)
    monkeypatch.setattr(account, 'list_positions', native_positions)
    runtime.clock[0] += timedelta(seconds=61)
    monotonic_time[0] = 62
    execution.lp_auto_run_once(round_id='component-recovery')
    recovered, = diagnostic_events(caplog, event='recovered', scope='account_planning')
    assert recovered['failure_count'] == 15
    assert recovered['first_cause_code'] == 'account_read_failed'
    assert recovered['latest_cause_code'] == 'account_snapshot_incomplete'
    assert recovered['first_stage'] == 'account_read'
    assert recovered['latest_stage'] == 'account_publish'
    assert recovered['first_exception_types'][-1] == 'ReadTimeout'
    assert account.balance_reads == account.position_reads == 1
    assert execution.lp_auto_state()['funds']['status'] == 'known'
    assert account.posts == account.cancels == []


def test_mixed_reward_errors_preserve_known_total_consistency(caplog):
    caplog.set_level(logging.INFO)
    condition = 'condition-mixed-reward-301'
    for scenario, reasons in (
        ('config', ['reward_config_unknown']),
        ('identity', ['reward_config_unknown', 'reward_identity_unknown']),
        ('asset', ['reward_config_unknown', 'reward_asset_unknown']),
    ):
        caplog.clear()
        mode = ['timeout']
        reads, closes = [], []
        def config(amount, *, id=301, asset='0x2791bca1f2de4661ed88a30c99a7a9449aa84174'):
            return {'id': id, 'asset_address': asset, 'start_date': '2020-01-01',
                    'end_date': '2500-12-31', 'rate_per_day': amount}
        def row(configs):
            return {'condition_id': condition, 'rewards_config': configs}
        class Public:
            def list_market_rewards(self, *, condition_id, sponsored):
                assert condition_id == condition
                reads.append((mode[0], sponsored))
                if mode[0] == 'timeout':
                    raise httpx.ReadTimeout(SECRET)
                if mode[0] == 'empty':
                    return ()
                if sponsored:
                    return (row([config('1' if mode[0] == 'inconsistent' else '4')]),)
                if mode[0] == 'valid':
                    return (row([config('3')]),)
                configs = [config('3'), {'rate_per_day': SECRET}]
                if scenario == 'asset':
                    # Unknown business total must not erase the independently
                    # valid USD3 contribution, including a later contribution.
                    configs = [configs[1], config('9', id=302, asset='0x' + 'a' * 40), configs[0]]
                rows = [row(configs)]
                if scenario == 'identity':
                    rows.append({'condition_id': SECRET, 'rewards_config': []})
                return tuple(rows)
            def close(self):
                closes.append(True)
        client = FakeClient()
        adapter = PolymarketTradingClient(TradingConfig(SECRET, SECRET), client,
                                          public_client_factory=Public)
        try:
            first = adapter.lp_reward_catalog(condition_ids=(condition,))
            assert first['state'] == 'unknown' and first['complete'] is False
            assert first['daily_pool_usd'] is None
            assert first['markets'][0]['reason_codes'] == ['reward_read_failed']
            initial = diagnostic_events(caplog, event='failed')
            assert {r['stage'] for r in initial} == {'reward_native', 'reward_sponsored'}
            assert len(initial) == 2
            mode[0] = 'inconsistent'
            mixed = adapter.lp_reward_catalog(condition_ids=(condition,))
            assert mixed['state'] == 'unknown' and mixed['complete'] is False
            assert mixed['daily_pool_usd'] is None
            assert mixed['markets'][0]['reason_codes'] == reasons
            assert mixed['markets'][0]['native_daily_pool_usd'] == 0
            assert not diagnostic_events(caplog, event='recovered'), scenario
            native, = diagnostic_events(caplog, event='failed', stage='reward_native',
                                        cause_code='reward_config_unknown')
            sponsored, = diagnostic_events(caplog, event='failed', stage='reward_sponsored',
                                           cause_code='reward_totals_inconsistent')
            for failure in (native, sponsored):
                original, = [r for r in initial if r['stage'] == failure['stage']]
                assert failure['scope_id'] == original['scope_id']
                assert failure['episode_id'] == original['episode_id']
                assert failure['attempt_id'] != original['attempt_id']
            # USD4 is consistent with the proven lower bound3. The native
            # source is still invalid; do not invent its complete total.
            mode[0] = 'consistent'
            partial = adapter.lp_reward_catalog(condition_ids=(condition,))
            assert partial['state'] == 'unknown' and partial['complete'] is False
            assert partial['daily_pool_usd'] is None
            assert partial['markets'][0]['reason_codes'] == reasons
            recovered, = diagnostic_events(caplog, event='recovered')
            assert recovered['scope_id'] == sponsored['scope_id']
            assert recovered['episode_id'] == sponsored['episode_id']
            assert recovered['stage'] == 'reward_sponsored' and recovered['failure_count'] == 2
            assert recovered['latest_cause_code'] == 'reward_totals_inconsistent'
            mode[0] = 'valid'
            valid = adapter.lp_reward_catalog(condition_ids=(condition,))
            assert valid['state'] == 'known' and valid['complete'] is True
            assert valid['daily_pool_usd'] == 4 and valid['markets'][0]['reason_codes'] == []
            native_recovery, = diagnostic_events(caplog, event='recovered', stage='reward_native')
            assert native_recovery['scope_id'] == native['scope_id']
            assert native_recovery['episode_id'] == native['episode_id']
            assert native_recovery['failure_count'] == 3
            assert native_recovery['first_cause_code'] == 'reward_read_failed'
            assert native_recovery['latest_cause_code'] == 'reward_config_unknown'
            mode[0] = 'empty'
            empty = adapter.lp_reward_catalog(condition_ids=(condition,))
            assert empty['state'] == 'known' and empty['complete'] is True
            assert empty['daily_pool_usd'] == 0 and empty['markets'][0]['reward_active'] is False
            assert len(diagnostic_events(caplog, event='recovered')) == 2
            assert len(reads) == len(closes) == 10
            assert {source: sum(sponsored == source for _, sponsored in reads)
                    for source in (False, True)} == {False: 5, True: 5}
            assert client.create_calls == client.post_calls == client.cancel_calls == []
        finally:
            adapter.close()
