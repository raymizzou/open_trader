"""Plan-time account approval and durable execution, through public entrypoints."""
from datetime import datetime, timedelta
from decimal import Decimal
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import fcntl

import pytest

from open_trader.polymarket_lp_scheduler import LPAutoScheduler
from tests import test_lp_auto_pool as pool
from tests.test_lp_auto_target_convergence import plan_setup, live_orders
from tests.test_lp_account_reservation_reconciliation import runtime


def install_order_result_reader(exchange, monkeypatch):
    """The external venue returns order facts without calling its balance API."""
    def result_reader(request):
        started = pool.NOW
        pool.NOW += timedelta(microseconds=1)
        return dict(authenticated=True, wallet_address='test-wallet',
            read_started_at=started, read_ended_at=pool.NOW, checked_at=pool.NOW,
            open_orders=deepcopy([o for o in exchange.orders if o['status'] == 'LIVE']),
            orders=deepcopy(exchange.orders), positions=deepcopy(exchange.positions),
            trades=deepcopy(exchange.trades), open_orders_complete=True,
            pagination_complete=True, trades_complete=True, positions_complete=True,
            order_read_errors={})
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', result_reader, raising=False)
    return result_reader


@pytest.mark.parametrize('invalidation', ['generation', 'ttl', 'timing_only',
    'incomplete_publication', 'registration_inflight'])
def test_first_plan_approval_is_atomic_with_account_facts(tmp_path, monkeypatch, invalidation):
    """#329 commits controls atomically without revoking acquired finances."""
    from collections import Counter
    import json
    import sqlite3
    from pathlib import Path
    from threading import get_ident

    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, budget='39')
    install_order_result_reader(exchange, monkeypatch)
    entered, release = Event(), Event()
    caller, armed, intercepted = [], [False], []
    barrier_errors, signed, account_reads, market_reads = [], [], [], []
    incomplete, financial_reads, signed_plans = [False], [], []
    registration_entered, registration_release = Event(), Event()
    registration_caller, registration_armed, registration_intercepted = [], [False], []
    connect = sqlite3.connect
    books = exchange.lp_order_books
    account = exchange.lp_account_snapshot_shared
    sign = exchange.lp_create_limit_order

    def observed_connect(database, *args, **kwargs):
        connection = connect(database, *args, **kwargs)
        if Path(database).resolve() == store.path.resolve():
            def trace(statement):
                if (caller and get_ident() == caller[0] and armed[0]
                        and statement.strip().upper() == 'BEGIN IMMEDIATE'):
                    armed[0] = False
                    intercepted.append(statement)
                    entered.set()
                    try:
                        if not release.wait(5):
                            barrier_errors.append('Independent SQLite approval watchdog expired')
                    except BaseException as exc:
                        barrier_errors.append(type(exc).__name__)
                elif (registration_caller and get_ident() == registration_caller[0]
                        and registration_armed[0] and statement.strip().upper() == 'BEGIN IMMEDIATE'):
                    registration_armed[0] = False
                    registration_intercepted.append(dict(thread=get_ident(), statement=statement))
                    registration_entered.set()
                    try:
                        if not registration_release.wait(5):
                            barrier_errors.append('Independent registration watchdog expired')
                    except BaseException as exc:
                        barrier_errors.append(type(exc).__name__)
            connection.set_trace_callback(trace)
        return connection

    def observed_books(ids, **kwargs):
        result = books(ids, **kwargs)
        market_reads.append(tuple(ids))
        if tuple(ids) == ('G',) and not intercepted:
            armed[0] = True
        return result

    def observed_account(**kwargs):
        financial_reads.append(bool(engine.lp_auto_state()['active_plan']))
        result = account(**kwargs)
        if incomplete[0]:
            result['pagination_complete'] = False
        account_reads.append(deepcopy(result))
        return result

    def observed_sign(**kwargs):
        signed_plans.append(deepcopy(engine.lp_auto_state()['active_plan']))
        signed.append(deepcopy(kwargs))
        return sign(**kwargs)

    monkeypatch.setattr(sqlite3, 'connect', observed_connect)
    monkeypatch.setattr(exchange, 'lp_order_books', observed_books)
    monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', observed_account)
    monkeypatch.setattr(exchange, 'lp_create_limit_order', observed_sign)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)

    def run():
        caller.append(get_ident())
        return scheduler.run_due()

    with ThreadPoolExecutor(2) as workers:
        pending = workers.submit(run)
        try:
            assert entered.wait(5), 'Original round reaches the real first approval write'
            before = engine.lp_auto_state()
            assert before['active_plan'] is None and before['intents'] == []
            assert not before['last_round'] or not before['last_round'].get('completed_at')
            assert signed == exchange.posts == exchange.cancels == []
            assert store.lp_sessions() == []
            with connect(store.path) as connection:
                document = json.loads(connection.execute(
                    'SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
                assert connection.execute('SELECT COUNT(*) FROM lp_actions').fetchone()[0] == 0
            approval = document['account_financial_facts']
            planning_at = datetime.fromisoformat(approval['checked_at'])
            assert approval['snapshot_id'] and approval['financial_status'] == 'known'
            assert before['funds']['as_of'] == approval['checked_at']
            assert account_reads[-1]['trade_generation'] == approval['trade_generation']
            assert planning_at <= pool.NOW
            if invalidation == 'generation':
                assert store.lp_advance_trade_generation(approval['trade_generation'])
                exchange.positions = [dict(token_id='H', condition_id='H',
                    size='20', average_price='.39')]
            elif invalidation == 'ttl':
                monkeypatch.setattr(pool, 'NOW', planning_at + timedelta(seconds=61))
            elif invalidation == 'incomplete_publication':
                exchange.positions = [dict(token_id='H', condition_id='H',
                    size='20', average_price='.39')]
                incomplete[0] = True
                published = engine.refresh_lp_dashboard_snapshot()
                assert published['reason'] == 'account_order_sync_unknown', published
                invalid = engine.lp_auto_state()
                assert 'account_order_sync_unknown' in invalid['admission_block_reasons']
                with connect(store.path) as connection:
                    current = json.loads(connection.execute(
                        'SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
                assert current['account_financial_facts'] == approval
                assert store.lp_trade_generation() == approval['trade_generation']
                assert invalid['funds']['as_of'] == approval['checked_at']
                assert invalid['active_plan'] is None and invalid['intents'] == []
                assert store.lp_sessions() == [] and signed == exchange.posts == exchange.cancels == []
                incomplete[0] = False
            elif invalidation == 'registration_inflight':
                exchange.positions = [dict(token_id='H', condition_id='H',
                    size='20', average_price='.39')]
                def publish():
                    registration_caller.append(get_ident())
                    registration_armed[0] = True
                    return engine.refresh_lp_dashboard_snapshot()
                publishing = workers.submit(publish)
                try:
                    assert registration_entered.wait(5), 'Real registration reaches its own writer in its apply lane'
                    assert registration_caller[0] != caller[0]
                    release.set()
                    assert pending.result(timeout=5), 'Original plan progresses without the background apply lane'
                    progressed = engine.lp_auto_state()
                    assert progressed['active_plan'] is None and progressed['last_round']['completed_at']
                    assert Counter(p['token_id'] for p in exchange.posts) == Counter({'B': 1, 'C': 1, 'E': 1, 'F': 1, 'G': 1})
                    assert exchange.cancels == []
                    assert len(store.lp_sessions()) == 5
                finally:
                    registration_release.set()
                published = publishing.result(timeout=5)
                assert registration_intercepted == [dict(thread=registration_caller[0], statement='BEGIN IMMEDIATE')]
                with connect(store.path) as connection:
                    current = json.loads(connection.execute(
                        'SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
                if published.get('state') == 'ready':
                    assert current['account_financial_facts']['financial_status'] == 'known'
                    assert Decimal(current['account_financial_facts']['inventory_cost_usd']) == Decimal('7.80')
                else:
                    assert published['reason'] == 'account_round_invalid', published
                    assert current['account_financial_facts'] == approval
                assert engine.lp_auto_state()['last_round']['round_id'] == progressed['last_round']['round_id']
            else:
                configured = engine.lp_auto_configure(dict(round_interval_seconds=90))
                assert configured['config_version'] == before['config_version'] + 1
                assert configured['trading_config_version'] == before['trading_config_version']
                assert configured['budget_usd'] == before['budget_usd'] == '39'
                assert configured['buy_price_level'] == before['buy_price_level'] == 2
                assert configured['target_buy_count'] == before['target_buy_count'] == 5
                assert configured['active_plan'] is None and configured['intents'] == []
                assert configured['funds']['as_of'] == approval['checked_at']
                with connect(store.path) as connection:
                    current = json.loads(connection.execute(
                        'SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
                assert current['account_financial_facts'] == approval
        finally:
            release.set()
        assert pending.result(timeout=5)
    assert intercepted == ['BEGIN IMMEDIATE'] and barrier_errors == []
    state = engine.lp_auto_state()
    assert Counter(p['token_id'] for p in exchange.posts) == Counter({'B': 1, 'C': 1, 'E': 1, 'F': 1, 'G': 1}), state['last_round']
    assert all(p['price'] == Decimal('.39') and p['quantity'] == 20 for p in exchange.posts)
    assert [r['token_id'] for r in state['last_round']['targets']] == ['B', 'C', 'E', 'F', 'G']
    assert all(a['state'] == 'success' for a in state['last_round']['actions'])
    assert state['last_round']['completed_at'] and state['active_plan'] is None
    assert exchange.cancels == []
    original_round = state['last_round']['round_id']
    original_orders = live_orders(exchange)
    assert signed_plans and all(plan['round_id'] == original_round for plan in signed_plans)
    assert all(plan['approval_account_facts'] == approval for plan in signed_plans)
    original_requests = [(session['session_id'], session['idempotency_key']) for session in store.lp_sessions()]
    completed = datetime.fromisoformat(state['last_round']['completed_at'])
    interval = 90 if invalidation == 'timing_only' else 30
    assert state['plan_wait']['kind'] == 'round'
    deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert deadline == completed + timedelta(seconds=interval)
    read_counts = (len(account_reads), len(market_reads))
    for elapsed in (1, interval - 1):
        monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=elapsed))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert (len(account_reads), len(market_reads)) == read_counts
        assert engine.lp_auto_state()['last_round']['round_id'] == original_round
        assert len(exchange.posts) == 5 and exchange.cancels == []
    assert not any(financial_reads)
    if invalidation in ('timing_only', 'ttl'):
        assert len(account_reads) == 1
        return
    monkeypatch.setattr(pool, 'NOW', deadline)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert [r['token_id'] for r in state['last_round']['targets']] == ['B', 'C', 'E', 'F'], state['last_round']
    assert state['last_round']['round_id'] != original_round
    next_round = state['last_round']['round_id']
    assert exchange.cancels == [original_orders['G']] and len(exchange.posts) == 5
    canceled_round = deepcopy(state['last_round'])
    canceled_audits = {session['session_id']: deepcopy(store.lp_actions(session['session_id']))
        for session in store.lp_sessions()}
    assert canceled_round['round_id'] == next_round and canceled_round['completed_at']
    assert state['active_plan'] is None and state['plan_wait']['kind'] == 'round'
    canceled_completed = datetime.fromisoformat(canceled_round['completed_at'])
    canceled_deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert canceled_deadline == canceled_completed + timedelta(seconds=30)
    before_current_reads = (len(account_reads), len(market_reads))
    before_current_as_of = state['funds']['as_of']
    for elapsed in (1, 29):
        monkeypatch.setattr(pool, 'NOW', canceled_completed + timedelta(seconds=elapsed))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert (len(account_reads), len(market_reads)) == before_current_reads
        assert engine.lp_auto_state()['last_round'] == canceled_round
        assert exchange.cancels == [original_orders['G']] and len(exchange.posts) == 5
        assert {sid: store.lp_actions(sid) for sid in canceled_audits} == canceled_audits
    monkeypatch.setattr(pool, 'NOW', canceled_deadline)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['last_round']['round_id'] != next_round
    assert state['last_round']['completed_at'] and state['active_plan'] is None
    assert state['funds']['as_of'] != before_current_as_of
    assert {sid: store.lp_actions(sid) for sid in canceled_audits} == canceled_audits
    assert live_orders(exchange) == {token: original_orders[token] for token in ('B', 'C', 'E', 'F')}
    assert exchange.cancels == [original_orders['G']] and len(exchange.posts) == 5
    assert [(session['session_id'], session['idempotency_key']) for session in store.lp_sessions()] == original_requests
    assert Decimal(state['funds']['total_usd']) == 39
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('7.80')
    assert state['slots']['occupied'] == 4
    assert len(account_reads) > read_counts[0] and not any(financial_reads)


@pytest.mark.parametrize('window', ['before_prepare', 'after_sign'])
def test_frozen_buy_keeps_current_stress_exit_gate(tmp_path, monkeypatch, window):
    from collections import Counter
    from pathlib import Path
    from threading import Lock, get_ident
    import json
    import sqlite3
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch, budget='39')
    direction, sign = exchange.direction, exchange.lp_create_limit_order
    changed, signatures, captured, financial_reads, market_reads, native_commits = [False], [], [], [], [], []
    fixture_lock = Lock()
    caller, armed = get_ident(), [window == 'before_prepare']
    connect = sqlite3.connect
    class ApprovalConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            result = super().execute(sql, parameters)
            if sql.strip().upper() == 'COMMIT' and armed[0] and get_ident() == caller:
                row = super().execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
                document = json.loads(row[0]) if row else {}
                plan = document.get('active_plan')
                if plan is not None:
                    armed[0] = False
                    assert not self.in_transaction
                    assert [t['token_id'] for t in plan['targets']] == ['B','C','E','F','G']
                    assert all(a['kind'] == 'buy' and a['state'] == 'pending' for a in plan['actions'])
                    assert super().execute('SELECT COUNT(*) FROM lp_actions').fetchone()[0] == 0
                    assert signatures == exchange.posts == exchange.cancels == []
                    state = deepcopy(engine.lp_auto_state())
                    assert state['active_plan']['round_id'] == plan['round_id'] == state['last_round']['round_id']
                    captured.append(state)
                    native_commits.append(dict(round_id=plan['round_id'],thread=caller,in_transaction=self.in_transaction))
                    changed[0] = True
            return result
    def observed_connect(database, *args, **kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs.setdefault('factory',ApprovalConnection)
        return connect(database,*args,**kwargs)
    if window == 'before_prepare':
        monkeypatch.setattr(sqlite3,'connect',observed_connect)
    def current_direction(token):
        result = direction(token)
        if token == 'B' and changed[0]:
            result['book']['bids'] = [dict(price='.40',size='1000'),dict(price='.39',size='1'),dict(price='.10',size='1000')]
        return result
    def signer(**kwargs):
        signed = sign(**kwargs)
        with fixture_lock:
            signatures.append(deepcopy(signed))
            if window == 'after_sign' and not captured:
                captured.append(deepcopy(engine.lp_auto_state()))
            if window == 'after_sign' and kwargs['token_id'] == 'B':
                changed[0] = True
        return signed
    monkeypatch.setattr(exchange,'direction',current_direction)
    monkeypatch.setattr(exchange,'lp_create_limit_order',signer)
    for name in ('lp_account_snapshot','lp_account_snapshot_shared','lp_market_metadata_fresh','lp_reward_catalog','lp_order_books'):
        read = getattr(exchange,name)
        def observed(*args,_read=read,_name=name,**kwargs):
            active = engine.lp_auto_state()['active_plan'] is not None
            if active:
                (financial_reads if _name in ('lp_account_snapshot','lp_account_snapshot_shared') else market_reads).append(_name)
            return _read(*args,**kwargs)
        monkeypatch.setattr(exchange,name,observed)
    scheduler = LPAutoScheduler(engine,clock=lambda:pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert changed[0] and len(captured) == 1 and captured[0]['active_plan']
    if window == 'before_prepare':
        assert len(native_commits) == 1 and native_commits[0]['in_transaction'] is False
    original = captured[0]
    assert Decimal('7.80')-Decimal('2.29') == Decimal('5.51')
    assert Decimal('5.51')/Decimal('7.80') > Decimal('.10')
    assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B','C','E','F','G'])
    assert Counter(p['token_id'] for p in signatures) == Counter(['B','C','E','F','G'])
    assert all(p['price'] == Decimal('.39') and p['quantity'] == 20 and p['post_only'] is True for p in exchange.posts)
    assert state['last_round']['round_id'] == original['active_plan']['round_id']
    assert state['last_round']['targets'] == original['last_round']['targets']
    assert [a['action_id'] for a in state['last_round']['actions']] == [a['action_id'] for a in original['active_plan']['actions']]
    assert [t['token_id'] for t in state['last_round']['targets']] == ['B','C','E','F','G']
    assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in state['last_round']['targets'])
    assert all(a['state'] == 'success' for a in state['last_round']['actions'])
    assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
    assert state['last_round']['completed_at'] and state['active_plan'] is None
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == original['funds']['as_of']
    assert original['funds']['as_of'] and financial_reads == market_reads == exchange.cancels == []
    assert state['plan_wait']['kind'] == 'round'
    completed = datetime.fromisoformat(state['last_round']['completed_at'])
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed+timedelta(seconds=30)
    audits = {a['session_id']:deepcopy(store.lp_actions(a['session_id'])) for a in state['last_round']['actions']}
    for action in state['last_round']['actions']:
        audit, = audits[action['session_id']]
        assert audit['state'] == 'accepted' and audit['post_started'] is True
        assert audit['token_id'] == action['token_id']
        assert Decimal(audit['price']) == Decimal('.39') and Decimal(audit['quantity']) == 20
    monkeypatch.setattr(pool,'NOW',completed+timedelta(seconds=29))
    scheduler.request_check()
    assert not scheduler.run_due()
    assert len(exchange.posts) == len(signatures) == 5 and financial_reads == market_reads == exchange.cancels == []
    assert {sid:store.lp_actions(sid) for sid in audits} == audits

@pytest.mark.parametrize('outcome', ['all_refused', 'mixed', 'refused_A_late_B',
    'late_A_refused_B', 'late_terminal_with_stop', 'late_terminal_with_protection'])
def test_rejected_cancel_batch_restores_real_protection_baseline(runtime, monkeypatch, outcome):
    from time import monotonic
    from polymarket.models.clob.cancel import CancelOrdersResponse
    from polymarket.models.clob.order_book import OrderBookLevel
    from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic
    from tests.test_lp_order_registration_contract import _open_order

    public = _CandidateSDKPublic(runtime.clock[0])
    book = public.get_order_book
    protective = [False]
    protective_book = Event()
    def current_book(*, token_id):
        value = book(token_id=token_id)
        result = value.model_copy(update={'timestamp': runtime.clock[0], 'bids': (
            OrderBookLevel(price=Decimal('.40'), size=Decimal('3000'
                if protective[0] and outcome != 'late_terminal_with_protection' else '1000')),
            OrderBookLevel(price=Decimal('.39'), size=Decimal('3000'
                if protective[0] and outcome == 'late_terminal_with_protection' else '1000')))})
        if protective[0]:
            protective_book.set()
        return result
    monkeypatch.setattr(public, 'get_order_book', current_book)
    originals = (_open_order('rank-a', 'BUY', price='.40', original='20'),
        _open_order('rank-b', 'BUY', price='.40', original='20'),
        _open_order('rank-c', 'BUY', price='.39', original='20'))
    store, adapter, account, lp, engine = runtime(orders=originals, public_client=public)
    engine.lp_auto_configure(dict(budget_usd='39', target_buy_count=1, buy_price_level=2))
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    session, = store.lp_active_sessions()
    baseline = deepcopy(session['queue_protection'])
    assert {Decimal(k) for k in baseline['levels']} == {Decimal('.40'), Decimal('.39')}
    high_key = next(k for k in baseline['levels'] if Decimal(k) == Decimal('.40'))
    low_key = next(k for k in baseline['levels'] if Decimal(k) == Decimal('.39'))
    assert baseline['levels'][high_key]['order_id'] == 'rank-a'
    assert baseline['levels'][low_key]['order_id'] == 'rank-c'
    assert lp._queue_protection_gate_open(session)
    terminal = {}
    original_ids = {o.id: o for o in originals}
    late = outcome not in ('all_refused', 'mixed')
    acknowledged_id = 'rank-a' if outcome == 'late_A_refused_B' else 'rank-b'
    refused_id = 'rank-b' if acknowledged_id == 'rank-a' else 'rank-a'
    financial_reads = []
    monitor_phase = [False]
    control_phase = ['current_planning']
    balance = account.get_balance_allowance
    def observed_balance(**kwargs):
        active = engine.lp_auto_state()['active_plan'] is not None
        if active or outcome in ('refused_A_late_B', 'late_A_refused_B',
                'late_terminal_with_stop', 'late_terminal_with_protection'):
            financial_reads.append(dict(phase='explicit_monitor' if monitor_phase[0] else
                'planned_auto' if active else control_phase[0],
                checked_at=runtime.clock[0], request=deepcopy(kwargs)))
        return balance(**kwargs)
    monkeypatch.setattr(account, 'get_balance_allowance', observed_balance)
    def venue(*, order_ids):
        account.cancels.append(order_ids)
        runtime.clock[0] += timedelta(seconds=1)
        if protective[0] or outcome == 'mixed' and order_ids == ('rank-a',):
            for oid in order_ids:
                terminal[oid] = original_ids[oid].model_copy(update={'status': 'CANCELED'})
            account.orders = tuple(o for o in account.orders if o.id not in order_ids)
            return CancelOrdersResponse(canceled=order_ids, not_canceled={})
        if late and order_ids == (acknowledged_id,):
            return CancelOrdersResponse(canceled=order_ids, not_canceled={})
        return CancelOrdersResponse(canceled=(), not_canceled={oid: 'venue_denied' for oid in order_ids})
    def exact_order(*, order_id):
        return terminal.get(order_id, original_ids[order_id])
    monkeypatch.setattr(account, 'cancel_orders', venue)
    monkeypatch.setattr(account, 'get_order', exact_order, raising=False)
    engine.lp_auto_set_desired_running(True)
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    assert scheduler.run_due()
    saved = engine.lp_auto_state()
    if outcome in ('all_refused', 'mixed'):
        original = deepcopy(saved['last_round'])
        assert saved['active_plan'] is None and original['completed_at']
        assert saved['plan_wait']['kind'] == 'round'
        assert datetime.fromisoformat(saved['plan_wait']['deadline']) == datetime.fromisoformat(
            original['completed_at']) + timedelta(seconds=30)
        assert [a['order_id'] for a in original['actions']] == ['rank-a', 'rank-b']
        assert [r['order_id'] for r in original['targets']] == ['rank-c']
        assert all(a['session_id'] == session['session_id'] for a in original['actions'])
        assert [a['state'] for a in original['actions']] == (
            ['rejected', 'rejected'] if outcome == 'all_refused' else ['canceling', 'rejected'])
        assert account.cancels == [('rank-a',), ('rank-b',)] and account.posts == []
        audits = deepcopy(store.lp_actions(session['session_id']))
        assert len(audits) == 2 and len({a['action_key'] for a in audits}) == 2
        for oid in ('rank-a', 'rank-b'):
            audit, = [a for a in audits if a.get('order_id') == oid]
            assert audit['state'] == ('accepted' if outcome == 'mixed' and oid == 'rank-a' else 'rejected')
            assert audit['rotation_round_id'] == original['round_id']
            if audit['state'] == 'rejected':
                assert audit['reason'] == 'venue_denied'
        assert saved['slots']['occupied'] == 3
        assert Decimal(saved['funds']['buy_reserved_usd']) == Decimal('23.80')
        assert financial_reads == []
        first_calls = list(account.cancels)
        engine.lp_auto_set_desired_running(False)
        deadline = datetime.fromisoformat(saved['plan_wait']['deadline'])
        runtime.clock[0] = deadline - timedelta(seconds=1)
        scheduler.request_check()
        assert not scheduler.run_due()
        engine.lp_auto_run_once()
        assert engine.lp_auto_state()['last_round'] == original
        assert account.cancels == first_calls and account.posts == []
        assert store.lp_actions(session['session_id']) == audits
        runtime.clock[0] = deadline
        state = engine.lp_auto_run_once()
        assert state['active_plan'] is None and state['desired_running'] is False
        assert state['last_round']['round_id'] != original['round_id']
        assert state['slots']['occupied'] == (3 if outcome == 'all_refused' else 2)
        assert Decimal(state['funds']['buy_reserved_usd']) == (
            Decimal('23.80') if outcome == 'all_refused' else Decimal('15.80'))
        restored = store.lp_session(session['session_id'])
        assert restored.get('yield_rotation_cancel_owners') == {}
        assert not restored.get('entry_cancel_requested')
        assert restored.get('augment_cancel_requested', []) == []
        if outcome == 'all_refused':
            assert restored['queue_protection'] == baseline
            assert {o.id for o in account.orders} == {'rank-a', 'rank-b', 'rank-c'}
        else:
            levels = restored['queue_protection']['levels']
            assert levels[low_key] == baseline['levels'][low_key]
            assert levels[high_key] == {**baseline['levels'][high_key], 'order_id': 'rank-b'}
            assert {k:v for k,v in restored['queue_protection'].items() if k != 'levels'} == {
                k:v for k,v in baseline.items() if k != 'levels'}
            assert {o.id for o in account.orders} == {'rank-b', 'rank-c'}
        assert lp._queue_protection_gate_open(restored)
        assert store.lp_actions(session['session_id']) == audits
        assert account.cancels == first_calls and account.posts == [] and financial_reads == []
        read_counts = dict(order_result=0, account=0)
        order_results = adapter.lp_order_result_snapshot
        account_snapshot = adapter.lp_account_snapshot_shared
        def observed_order_results(*args, **kwargs):
            read_counts['order_result'] += 1
            return order_results(*args, **kwargs)
        def observed_account_snapshot(*args, **kwargs):
            read_counts['account'] += 1
            return account_snapshot(*args, **kwargs)
        monkeypatch.setattr(adapter, 'lp_order_result_snapshot', observed_order_results)
        monkeypatch.setattr(adapter, 'lp_account_snapshot_shared', observed_account_snapshot)
        before_reads = dict(read_counts)
        current_round = deepcopy(state['last_round'])
        scheduler.request_check()
        assert scheduler.run_due()
        assert scheduler.snapshot()['next_check_at'] == state['plan_wait']['deadline']
        assert engine.lp_auto_state()['last_round'] == current_round
        assert read_counts == before_reads
        assert account.cancels == first_calls and account.posts == []
        assert store.lp_actions(session['session_id']) == audits
        engine.lp_auto_run_once()
        assert engine.lp_auto_state()['last_round'] == current_round
        assert read_counts == before_reads
        assert account.cancels == first_calls and account.posts == []
        assert store.lp_actions(session['session_id']) == audits
        if outcome == 'all_refused':
            ordinary_book = Event()
            normal_books = []
            current_sdk_book = public.get_order_book
            def observed_ordinary_book(*, token_id):
                model = current_sdk_book(token_id=token_id)
                if not protective[0]:
                    normal_books.append(model)
                    ordinary_book.set()
                return model
            monkeypatch.setattr(public, 'get_order_book', observed_ordinary_book)
        engine.lp_tick()
        assert account.cancels == first_calls and account.posts == []
        assert store.lp_actions(session['session_id']) == audits
        _, _, restarted_account, _, restarted = runtime(orders=account.orders, public_client=public)
        restarted.lp_auto_run_once()
        assert restarted_account.cancels == restarted_account.posts == []
        assert store.lp_actions(session['session_id']) == audits
        engine.lp_auto_set_desired_running(True)
        if outcome == 'all_refused':
            monitor_watchdog = monotonic() + 5
            monitor_pause = Event()
            while True:
                observed = engine.lp_tick()
                healthy = store.lp_session(session['session_id'])
                levels = healthy['queue_protection']['levels']
                if (ordinary_book.is_set() and not healthy.get('facts_error')
                        and not healthy.get('publication_pending') and lp._queue_protection_gate_open(healthy)
                        and set(levels) == set(baseline['levels'])
                        and all(bucket['state'] in ('registered', 'monitoring')
                            and 'book_unknown' not in bucket.get('reason_codes', ())
                            and bucket.get('data_time') and Decimal(bucket['level_total']) == 1000
                            for bucket in levels.values())):
                    break
                assert monotonic() < monitor_watchdog, (observed, healthy)
                monitor_pause.wait(min(.01, max(0, monitor_watchdog - monotonic())))
            assert normal_books and all({level.price: level.size for level in model.bids} == {
                Decimal('.40'): Decimal('1000'), Decimal('.39'): Decimal('1000')} for model in normal_books)
            assert healthy.get('yield_rotation_cancel_owners') == {}
            assert not healthy.get('entry_cancel_requested') and healthy.get('augment_cancel_requested', []) == []
            assert account.cancels == first_calls and account.posts == []
            assert store.lp_actions(session['session_id']) == audits
            retry_baseline = deepcopy(healthy['queue_protection'])
            assert {key:value for key,value in retry_baseline.items() if key != 'levels'} == {
                key:value for key,value in baseline.items() if key != 'levels'}
            for key, bucket in baseline['levels'].items():
                for field in ('baseline_front', 'baseline_price', 'baseline_source', 'baseline_source_timestamp',
                        'baseline_book_received_at', 'baseline_book_hash', 'baseline_version',
                        'threshold', 'cancel_scope', 'order_id', 'notification_sent'):
                    assert retry_baseline['levels'][key][field] == bucket[field], (key, field)
            assert retry_baseline['levels'][high_key]['order_id'] == 'rank-a'
            assert retry_baseline['levels'][low_key]['order_id'] == 'rank-c'
            runtime.clock[0] = datetime.fromisoformat(state['plan_wait']['deadline'])
            assert scheduler.run_due()
            waiting = engine.lp_auto_state()
            retried = deepcopy(waiting['last_round'])
            assert waiting['active_plan'] is None and retried['completed_at']
            assert retried['round_id'] != original['round_id']
            assert [a['state'] for a in retried['actions']] == ['rejected', 'rejected']
            assert account.cancels == first_calls + [('rank-a',), ('rank-b',)]
            retry_audits = deepcopy(store.lp_actions(session['session_id']))
            assert len(retry_audits) == 4 and len({a['action_key'] for a in retry_audits}) == 4
            for old in audits:
                assert next(a for a in retry_audits if a['action_key'] == old['action_key']) == old
            engine.lp_auto_set_desired_running(False)
            runtime.clock[0] = datetime.fromisoformat(waiting['plan_wait']['deadline'])
            state = engine.lp_auto_run_once()
            assert state['active_plan'] is None and state['desired_running'] is False
            restored = store.lp_session(session['session_id'])
            assert restored.get('yield_rotation_cancel_owners') == {}
            assert restored['queue_protection'] == retry_baseline and lp._queue_protection_gate_open(restored)
            assert store.lp_actions(session['session_id']) == retry_audits
            assert account.cancels == first_calls + [('rank-a',), ('rank-b',)] and account.posts == []
            audits = retry_audits
            engine.lp_auto_set_desired_running(True)
        calls = list(account.cancels)
        protective[0] = True
        runtime.clock[0] += timedelta(seconds=1)
        watchdog = monotonic() + 5
        expected = calls + ([('rank-a',), ('rank-b',)] if outcome == 'all_refused' else [('rank-b',)])
        pause = Event()
        while account.cancels != expected:
            assert monotonic() < watchdog, 'Public protection progression watchdog'
            engine.lp_tick()
            pause.wait(min(.01, max(0, watchdog - monotonic())))
        assert protective_book.is_set(), 'Original external SDK book was observed'
        assert account.cancels == expected and account.posts == []
        assert store.lp_session(session['session_id'])['queue_protection']['levels'][high_key]['cancel_reason'] == 'queue_ahead_ratio'
        for old in audits:
            assert next(a for a in store.lp_actions(session['session_id']) if a['action_key'] == old['action_key']) == old
        return
    if outcome in ('refused_A_late_B', 'late_A_refused_B',
            'late_terminal_with_stop', 'late_terminal_with_protection'):
        original = deepcopy(saved['last_round'])
        assert saved['active_plan'] is None and original['completed_at']
        assert [a['order_id'] for a in original['actions']] == ['rank-a', 'rank-b']
        assert all(a['session_id'] == session['session_id'] for a in original['actions'])
        assert [r['order_id'] for r in original['targets']] == ['rank-c']
        original_states = ['canceling', 'rejected'] if acknowledged_id == 'rank-a' else ['rejected', 'canceling']
        assert [a['state'] for a in original['actions']] == original_states
        assert saved['plan_wait']['kind'] == 'round'
        first_deadline = datetime.fromisoformat(saved['plan_wait']['deadline'])
        assert first_deadline == datetime.fromisoformat(original['completed_at']) + timedelta(seconds=30)
        assert account.cancels == [('rank-a',), ('rank-b',)] and account.posts == []
        assert saved['slots']['occupied'] == 3 and Decimal(saved['funds']['buy_reserved_usd']) == Decimal('23.80')
        original_audits = deepcopy(store.lp_actions(session['session_id']))
        assert len(original_audits) == 2 and len({a['action_key'] for a in original_audits}) == 2
        for oid in ('rank-a', 'rank-b'):
            audit, = [a for a in original_audits if a.get('order_id') == oid]
            assert audit['state'] == ('accepted' if oid == acknowledged_id else 'rejected')
            assert audit['rotation_round_id'] == original['round_id']
            if oid == refused_id:
                assert audit['reason'] == 'venue_denied'
        assert not any(row['phase'] == 'planned_auto' for row in financial_reads)
        result_calls = []
        read_results = adapter.lp_order_result_snapshot
        def observed_late_results(request):
            packet = read_results(request)
            result_calls.append(dict(request=deepcopy(request), packet=deepcopy(packet)))
            return packet
        monkeypatch.setattr(adapter, 'lp_order_result_snapshot', observed_late_results)
        owners_before = deepcopy(store.lp_session(session['session_id']).get('yield_rotation_cancel_owners'))
        before_read_counts = (len(financial_reads), len(result_calls))
        runtime.clock[0] = first_deadline - timedelta(seconds=1)
        scheduler.request_check()
        assert not scheduler.run_due()
        engine.lp_auto_run_once()
        assert (len(financial_reads), len(result_calls)) == before_read_counts
        assert store.lp_session(session['session_id']).get('yield_rotation_cancel_owners') == owners_before
        assert engine.lp_auto_state()['last_round'] == original
        assert account.cancels == [('rank-a',), ('rank-b',)] and account.posts == []
        assert store.lp_actions(session['session_id']) == original_audits
        runtime.clock[0] = first_deadline
        assert scheduler.run_due()
        second = engine.lp_auto_state()
        current_round = deepcopy(second['last_round'])
        assert second['active_plan'] is None and current_round['completed_at']
        assert current_round['round_id'] != original['round_id']
        assert [a['order_id'] for a in current_round['actions']] == ['rank-a', 'rank-b']
        assert [a['state'] for a in current_round['actions']] == original_states
        assert all(a['action_id'] != old['action_id'] for a, old in zip(current_round['actions'], original['actions']))
        assert [{k: r.get(k) for k in ('condition_id', 'token_id', 'order_id', 'session_id', 'price', 'quantity')}
            for r in current_round['targets']] == [{k: r.get(k) for k in
                ('condition_id', 'token_id', 'order_id', 'session_id', 'price', 'quantity')} for r in original['targets']]
        assert account.cancels == [('rank-a',), ('rank-b',), ('rank-a',), ('rank-b',)]
        assert account.posts == [] and {o.id for o in account.orders} == {'rank-a', 'rank-b', 'rank-c'}
        assert second['slots']['occupied'] == 3 and Decimal(second['funds']['buy_reserved_usd']) == Decimal('23.80')
        assert second['plan_wait']['kind'] == 'round'
        second_deadline = datetime.fromisoformat(second['plan_wait']['deadline'])
        assert second_deadline == datetime.fromisoformat(current_round['completed_at']) + timedelta(seconds=30)
        audits = deepcopy(store.lp_actions(session['session_id']))
        assert len(audits) == 4 and len({a['action_key'] for a in audits}) == len({a['action_id'] for a in audits}) == 4
        for old in original_audits:
            assert next(a for a in audits if a['action_key'] == old['action_key']) == old
        new_audits = [a for a in audits if a['rotation_round_id'] == current_round['round_id']]
        assert {a['order_id'] for a in new_audits} == {'rank-a', 'rank-b'}
        assert all(a['state'] == ('accepted' if a['order_id'] == acknowledged_id else 'rejected') for a in new_audits)
        assert all(a.get('reason') == 'venue_denied' for a in new_audits if a['state'] == 'rejected')
        assert any(row['phase'] == 'current_planning' and row['checked_at'] >= first_deadline for row in financial_reads)
        assert not any(row['phase'] == 'planned_auto' for row in financial_reads)
        engine.lp_auto_set_desired_running(False)
        control = None
        if outcome == 'late_terminal_with_stop':
            control_phase[0] = 'public_stop'
            try:
                stopped = lp.stop(session['session_id'])
            finally:
                control_phase[0] = 'current_planning'
            control = deepcopy(store.lp_session(session['session_id']))
            assert stopped['session_id'] == session['session_id'] and control['stop_requested'] is True
            assert control['state'] in ('review', 'needs_attention')
        elif outcome == 'late_terminal_with_protection':
            protective[0] = True
            runtime.clock[0] += timedelta(seconds=1)
            watchdog = monotonic() + 5
            pause = Event()
            yield_calls = list(account.cancels)
            monitor_phase[0] = True
            try:
                while True:
                    if account.cancels == yield_calls:
                        engine.lp_tick()
                    control = deepcopy(store.lp_session(session['session_id']))
                    bucket = control['queue_protection']['levels'][low_key]
                    protection_audits = [a for a in store.lp_actions(session['session_id'])
                        if a.get('role') == 'entry-protection-cancel' and a.get('targets') == ['rank-c']]
                    if (account.cancels == yield_calls + [('rank-c',)] and protective_book.is_set()
                            and bucket.get('cancel_reason') == 'queue_ahead_ratio'
                            and bucket.get('state') == 'canceling' and bucket.get('cancel_targets') == ['rank-c']
                            and protection_audits and all(a['state'] == 'accepted' for a in protection_audits)):
                        break
                    assert monotonic() < watchdog, (control, protection_audits, account.cancels)
                    pause.wait(min(.01, max(0, watchdog - monotonic())))
            finally:
                monitor_phase[0] = False
            assert 'rank-c' in control['augment_cancel_requested']
            assert any(row['phase'] == 'explicit_monitor' for row in financial_reads)
            protective[0] = False
        if control is not None:
            control_audits = deepcopy(store.lp_actions(session['session_id']))
            control_calls = list(account.cancels)
            assert control.get('yield_rotation_cancel_owners', {}).get(acknowledged_id) == current_round['round_id']
        terminal[acknowledged_id] = original_ids[acknowledged_id].model_copy(update={'status': 'CANCELED'})
        account.orders = tuple(o for o in account.orders if o.id != acknowledged_id)
        runtime.clock[0] = second_deadline
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        assert state['desired_running'] is False and state['active_plan'] is None
        assert state['last_round']['round_id'] not in (original['round_id'], current_round['round_id'])
        assert state['last_round']['actions'] == []
        if control is None:
            assert state['slots']['occupied'] == 2 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('15.80')
        restored = store.lp_session(session['session_id'])
        if control is not None:
            if outcome == 'late_terminal_with_protection':
                for key in ('state', 'stop_requested', 'stop_loss_latched', 'owned_cancel_requested'):
                    assert restored.get(key) == control.get(key), (key, control, restored)
                assert restored['queue_protection']['levels'][low_key] == control['queue_protection']['levels'][low_key]
                assert {key:value for key,value in restored['queue_protection'].items() if key != 'levels'} == {
                    key:value for key,value in control['queue_protection'].items() if key != 'levels'}
                assert restored['queue_protection']['levels'][high_key] == {
                    **baseline['levels'][high_key], 'order_id': refused_id}
                assert not restored.get('entry_cancel_requested')
                assert restored.get('augment_cancel_requested', []) == [
                    oid for oid in control.get('augment_cancel_requested', []) if oid not in (acknowledged_id, refused_id)]
                assert 'rank-c' in restored['augment_cancel_requested']
            else:
                for key in ('state', 'stop_requested', 'stop_loss_latched', 'entry_cancel_requested',
                        'augment_cancel_requested', 'owned_cancel_requested', 'queue_protection'):
                    assert restored.get(key) == control.get(key), (key, control, restored)
            assert restored.get('yield_rotation_cancel_owners') == {}
            assert restored['queue_protection'] != baseline
            assert store.lp_actions(session['session_id']) == control_audits
            assert account.cancels == control_calls and account.posts == []
            before_read_counts = (len(financial_reads), len(result_calls))
            scheduler.request_check()
            assert not scheduler.run_due()
            engine.lp_auto_run_once()
            assert (len(financial_reads), len(result_calls)) == before_read_counts
            _, _, restarted_account, _, restarted = runtime(orders=account.orders, public_client=public)
            restarted.lp_auto_run_once()
            assert restarted_account.cancels == restarted_account.posts == []
            assert account.cancels == control_calls and account.posts == []
            assert store.lp_actions(session['session_id']) == control_audits
            assert not any(row['phase'] == 'planned_auto' for row in financial_reads)
            return
        assert restored.get('yield_rotation_cancel_owners') == {}
        assert not restored.get('entry_cancel_requested') and restored.get('augment_cancel_requested', []) == []
        assert restored['queue_protection']['levels'][high_key] == {
            **baseline['levels'][high_key], 'order_id': refused_id}
        assert restored['queue_protection']['levels'][low_key] == baseline['levels'][low_key]
        assert lp._queue_protection_gate_open(restored)
        assert {o.id for o in account.orders} == {refused_id, 'rank-c'}
        assert store.lp_actions(session['session_id']) == audits
        calls = list(account.cancels)
        assert calls == [('rank-a',), ('rank-b',), ('rank-a',), ('rank-b',)] and account.posts == []
        before_read_counts = (len(financial_reads), len(result_calls))
        scheduler.request_check()
        assert not scheduler.run_due()
        engine.lp_auto_run_once()
        assert (len(financial_reads), len(result_calls)) == before_read_counts
        assert account.cancels == calls and account.posts == []
        assert store.lp_actions(session['session_id']) == audits
        monitor_phase[0] = True
        try:
            engine.lp_tick()
        finally:
            monitor_phase[0] = False
        assert account.cancels == calls and account.posts == []
        assert store.lp_actions(session['session_id']) == audits
        _, _, restarted_account, _, restarted = runtime(orders=account.orders, public_client=public)
        restarted.lp_auto_run_once()
        assert restarted_account.cancels == restarted_account.posts == []
        protective[0] = True
        runtime.clock[0] += timedelta(seconds=1)
        watchdog = monotonic() + 5
        expected = calls + [(refused_id,)]
        pause = Event()
        monitor_phase[0] = True
        try:
            while account.cancels != expected:
                assert monotonic() < watchdog, 'Public ordinary survivor protection watchdog'
                engine.lp_tick()
                pause.wait(min(.01, max(0, watchdog - monotonic())))
        finally:
            monitor_phase[0] = False
        assert protective_book.is_set() and account.cancels == expected and account.posts == []
        assert store.lp_session(session['session_id'])['queue_protection']['levels'][high_key]['cancel_reason'] == 'queue_ahead_ratio'
        for old in audits:
            assert next(a for a in store.lp_actions(session['session_id']) if a['action_key'] == old['action_key']) == old
        assert not any(row['phase'] == 'planned_auto' for row in financial_reads)
        return
    original = deepcopy(saved['active_plan'])
    assert [a['order_id'] for a in original['actions']] == ['rank-a', 'rank-b']
    assert [r['order_id'] for r in original['targets']] == ['rank-c']
    assert all(a['session_id'] == session['session_id'] for a in original['actions'])
    assert account.cancels == [('rank-a',), ('rank-b',)] and account.posts == []
    original_audits = deepcopy(store.lp_actions(session['session_id']))
    runtime.clock[0] = datetime.fromisoformat(saved['plan_wait']['deadline'])
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['last_round']['round_id'] == original['round_id']
    assert [a['action_id'] for a in state['last_round']['actions']] == [a['action_id'] for a in original['actions']]
    if late:
        assert [a['state'] for a in state['last_round']['actions']] == (
            ['canceling', 'rejected'] if acknowledged_id == 'rank-a' else ['rejected', 'canceling'])
        assert state['active_plan']['round_id'] == original['round_id']
        assert not state['last_round']['completed_at'] and state['plan_wait']['kind'] == 'order'
        assert state['slots']['occupied'] == 3 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('23.80')
        assert account.cancels == [('rank-a',), ('rank-b',), (acknowledged_id,)]
        assert account.posts == [] and financial_reads == []
        audits = deepcopy(store.lp_actions(session['session_id']))
        assert len(audits) == 3
        assert len({a['action_key'] for a in audits}) == 3
        assert [a['state'] for a in audits if a.get('order_id') == acknowledged_id] == ['accepted', 'accepted']
        refused_audit, = [a for a in audits if a.get('order_id') == refused_id]
        assert refused_audit['state'] == 'rejected' and refused_audit['reason'] == 'venue_denied'
        assert refused_audit['rotation_round_id'] == original['round_id']
        for audit in original_audits:
            assert next(a for a in audits if a['action_key'] == audit['action_key']) == audit
        control = None
        if outcome == 'late_terminal_with_stop':
            stopped = lp.stop(session['session_id'])
            control = store.lp_session(session['session_id'])
            assert stopped['session_id'] == session['session_id']
            assert control['stop_requested'] is True
            assert control['state'] in ('review', 'needs_attention')
        elif outcome == 'late_terminal_with_protection':
            protective[0] = True
            runtime.clock[0] += timedelta(seconds=1)
            deadline = monotonic() + 5
            pause = Event()
            monitor_phase[0] = True
            try:
                while account.cancels == [('rank-a',), ('rank-b',), (acknowledged_id,)]:
                    assert monotonic() < deadline, 'Public protection takeover watchdog'
                    engine.lp_tick()
                    pause.wait(min(.01, max(0, deadline - monotonic())))
            finally:
                monitor_phase[0] = False
            assert protective_book.is_set()
            assert account.cancels == [('rank-a',), ('rank-b',), (acknowledged_id,), ('rank-c',)]
            control = store.lp_session(session['session_id'])
            bucket = control['queue_protection']['levels'][low_key]
            assert bucket['cancel_reason'] == 'queue_ahead_ratio'
            assert bucket['state'] == 'canceling' and bucket['cancel_targets'] == ['rank-c']
            assert 'rank-c' in control['augment_cancel_requested']
            assert len(financial_reads) == 2, financial_reads
            assert all(row['phase'] == 'explicit_monitor' and row['request'] for row in financial_reads)
            monitor_reads = deepcopy(financial_reads)
            protective[0] = False
        if control is not None:
            control = deepcopy(control)
            control_audits = deepcopy(store.lp_actions(session['session_id']))
            control_calls = list(account.cancels)
            assert control.get('yield_rotation_cancel_owners') == {acknowledged_id: original['round_id']}
        terminal[acknowledged_id] = original_ids[acknowledged_id].model_copy(update={'status': 'CANCELED'})
        account.orders = tuple(o for o in account.orders if o.id != acknowledged_id)
        runtime.clock[0] = datetime.fromisoformat(state['plan_wait']['deadline'])
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        assert state['last_round']['round_id'] == original['round_id']
        assert [a['action_id'] for a in state['last_round']['actions']] == [a['action_id'] for a in original['actions']]
        assert state['last_round']['targets'] == saved['last_round']['targets']
        assert [a['state'] for a in state['last_round']['actions']] == (
            ['success', 'rejected'] if acknowledged_id == 'rank-a' else ['rejected', 'success'])
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        assert state['plan_wait']['kind'] == 'round'
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(
            state['last_round']['completed_at']) + timedelta(seconds=60)
        if control is None:
            assert state['slots']['occupied'] == 2 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('15.80')
        assert account.posts == []
        assert financial_reads == (monitor_reads if outcome == 'late_terminal_with_protection' else [])
        restored = store.lp_session(session['session_id'])
        if control is not None:
            if outcome == 'late_terminal_with_protection':
                for key in ('state', 'stop_requested', 'stop_loss_latched', 'owned_cancel_requested'):
                    assert restored.get(key) == control.get(key), (key, control, restored)
                assert restored['queue_protection']['levels'][low_key] == control['queue_protection']['levels'][low_key]
                assert {k:v for k,v in restored['queue_protection'].items() if k!='levels'} == {
                    k:v for k,v in control['queue_protection'].items() if k!='levels'}
                assert restored['queue_protection']['levels'][high_key] == {
                    **baseline['levels'][high_key], 'order_id': refused_id}
                assert not restored.get('entry_cancel_requested')
                assert restored.get('augment_cancel_requested', []) == [
                    oid for oid in control.get('augment_cancel_requested', [])
                    if oid not in (acknowledged_id, refused_id)]
                assert 'rank-c' in restored['augment_cancel_requested']
            else:
                for key in ('state', 'stop_requested', 'stop_loss_latched', 'entry_cancel_requested',
                        'augment_cancel_requested', 'owned_cancel_requested', 'queue_protection'):
                    assert restored.get(key) == control.get(key), (key, control, restored)
            assert restored.get('yield_rotation_cancel_owners') == {}
            assert restored['queue_protection'] != baseline
            assert store.lp_actions(session['session_id']) == control_audits
            assert account.cancels == control_calls and account.posts == []
            scheduler.request_check()
            assert not scheduler.run_due()
            engine.lp_auto_run_once()
            _, _, restarted_account, _, restarted = runtime(orders=account.orders, public_client=public)
            restarted.lp_auto_run_once()
            assert restarted_account.cancels == restarted_account.posts == []
            assert account.cancels == control_calls and account.posts == []
            assert store.lp_actions(session['session_id']) == control_audits
            assert financial_reads == (monitor_reads if outcome == 'late_terminal_with_protection' else [])
            return
        assert restored.get('yield_rotation_cancel_owners') == {}
        assert not restored.get('entry_cancel_requested')
        assert restored.get('augment_cancel_requested', []) == []
        assert restored['queue_protection']['levels'][high_key] == {
            **baseline['levels'][high_key], 'order_id': refused_id}
        assert restored['queue_protection']['levels'][low_key] == baseline['levels'][low_key]
        assert {o.id for o in account.orders} == {refused_id, 'rank-c'}
        assert store.lp_actions(session['session_id']) == audits
        calls = list(account.cancels)
        scheduler.request_check()
        assert not scheduler.run_due()
        engine.lp_auto_run_once()
        engine.lp_tick()
        assert account.cancels == calls and account.posts == []
        assert store.lp_actions(session['session_id']) == audits
        _, _, restarted_account, _, restarted = runtime(orders=account.orders, public_client=public)
        restarted.lp_auto_run_once()
        assert restarted_account.cancels == restarted_account.posts == []
        protective[0] = True
        runtime.clock[0] += timedelta(seconds=1)
        deadline = monotonic() + 5
        expected = calls + [(refused_id,)]
        pause = Event()
        while account.cancels != expected:
            assert monotonic() < deadline, 'Public protection progression watchdog'
            engine.lp_tick()
            pause.wait(min(.01, max(0, deadline - monotonic())))
        assert protective_book.is_set(), 'Original external SDK book was observed'
        assert account.cancels == expected
        assert store.lp_session(session['session_id'])['queue_protection']['levels'][high_key]['cancel_reason'] == 'queue_ahead_ratio'
        assert account.posts == []
        for audit in audits:
            assert next(a for a in store.lp_actions(session['session_id']) if a['action_key'] == audit['action_key']) == audit
        return
    assert [a['state'] for a in state['last_round']['actions']] == (['rejected', 'rejected']
        if outcome == 'all_refused' else ['success', 'rejected'])
    assert state['last_round']['completed_at'] and state['active_plan'] is None
    assert state['plan_wait']['kind'] == 'round'
    assert state['slots']['occupied'] == (3 if outcome == 'all_refused' else 2)
    assert Decimal(state['funds']['buy_reserved_usd']) == (Decimal('23.80')
        if outcome == 'all_refused' else Decimal('15.80'))
    assert account.posts == [] and financial_reads == []
    audits = deepcopy(store.lp_actions(session['session_id']))
    for oid in ('rank-a', 'rank-b'):
        audit, = [a for a in audits if a.get('order_id') == oid]
        assert audit['state'] == ('accepted' if outcome == 'mixed' and oid == 'rank-a' else 'rejected')
        if audit['state'] == 'rejected':
            assert audit['reason'] == 'venue_denied' and audit['rotation_round_id'] == original['round_id']
    restored = store.lp_session(session['session_id'])
    if outcome == 'all_refused':
        assert restored.get('yield_rotation_cancel_owners') == {}
        assert not restored.get('entry_cancel_requested')
        assert restored.get('augment_cancel_requested', []) == []
        assert restored['queue_protection'] == baseline
    else:
        levels = restored['queue_protection']['levels']
        assert levels[low_key] == baseline['levels'][low_key]
        assert levels[high_key] == {**baseline['levels'][high_key], 'order_id': 'rank-b'}
        assert 'rank-b' not in restored.get('augment_cancel_requested', [])
        assert {o.id for o in account.orders} == {'rank-b', 'rank-c'}
    assert lp._queue_protection_gate_open(restored)
    first_calls = list(account.cancels)
    scheduler.request_check()
    assert not scheduler.run_due()
    engine.lp_auto_run_once()
    engine.lp_tick()
    assert account.cancels == first_calls and account.posts == []
    assert store.lp_actions(session['session_id']) == audits
    _, _, restarted_account, _, restarted = runtime(orders=account.orders, public_client=public)
    restarted.lp_auto_run_once()
    assert restarted_account.cancels == restarted_account.posts == []
    if outcome == 'all_refused':
        runtime.clock[0] = datetime.fromisoformat(state['plan_wait']['deadline'])
        assert scheduler.run_due()
        waiting = engine.lp_auto_state()
        assert waiting['active_plan']['round_id'] != original['round_id']
        assert account.cancels == first_calls + [('rank-a',), ('rank-b',)]
        runtime.clock[0] = datetime.fromisoformat(waiting['plan_wait']['deadline'])
        assert scheduler.run_due()
        assert engine.lp_auto_state()['last_round']['completed_at']
        for old in audits:
            assert next(a for a in store.lp_actions(session['session_id'])
                if a['action_key'] == old['action_key']) == old
        protective[0] = True
        deadline = monotonic() + 5
        engine.lp_tick()
        assert protective_book.wait(max(0, deadline - monotonic())), 'External SDK book watchdog'
        expected = first_calls + [('rank-a',), ('rank-b',), ('rank-a',), ('rank-b',)]
        pause = Event()
        while account.cancels != expected:
            assert monotonic() < deadline, 'Public protection progression watchdog'
            pause.wait(min(.01, max(0, deadline - monotonic())))
            engine.lp_tick()
        assert account.cancels == expected
        bucket = store.lp_session(session['session_id'])['queue_protection']['levels'][high_key]
        assert bucket['cancel_reason'] == 'queue_ahead_ratio'
        assert account.posts == []


def test_entry_audit_registration_failure_prevents_post(tmp_path, monkeypatch):
    from collections import Counter
    import sqlite3
    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch)
    signed = []
    sign = exchange.lp_create_limit_order
    def signer(**kwargs):
        signed.append(kwargs)
        return sign(**kwargs)
    monkeypatch.setattr(exchange, 'lp_create_limit_order', signer)
    with sqlite3.connect(store.path) as connection:
        connection.execute('''CREATE TRIGGER fail_original_entry_audit BEFORE INSERT ON lp_actions
            WHEN json_extract(NEW.payload,'$.role')='entry'
            AND json_extract(NEW.payload,'$.side')='BUY'
            AND json_extract(NEW.payload,'$.token_id')='B'
            AND EXISTS (SELECT 1 FROM lp_sessions s JOIN lp_auto_pool p ON p.singleton=1
                WHERE s.session_id=NEW.session_id
                AND json_extract(s.payload,'$.token_id')='B'
                AND s.idempotency_key='lp-auto:' || json_extract(p.payload,'$.active_plan.actions[0].action_id'))
            BEGIN SELECT RAISE(ABORT, 'original entry audit registration failed'); END''')
    try:
        state = engine.lp_auto_run_once()
        plan = state['last_round']
        assert state['active_plan'] is None and plan['completed_at']
        assert [r['condition_id'] for r in plan['targets']] == ['B', 'C', 'E', 'F', 'G']
        assert all(Decimal(r['price']) == Decimal('.39') and Decimal(r['quantity']) == 20 for r in plan['targets'])
        first, = [a for a in plan['actions'] if a.get('token_id') == 'B']
        assert first['state'] == 'not_sent' and first['reason'] == 'entry_audit_registration_failed'
        assert first['error_type'] == 'IntegrityError' and first['error_code'] == 'SQLITE_CONSTRAINT_TRIGGER'
        session, = [row for row in store.lp_sessions() if row['token_id'] == 'B']
        assert first['session_id'] == session['session_id']
        assert session['token_id'] == session['condition_id'] == 'B'
        assert session['idempotency_key'] == 'lp-auto:' + first.get('request_id', first['action_id'])
        assert session['submit_stage'] == 'pre_send_rejected' and session['post_started'] is False
        assert session['state'] == 'entry_rejected' and not session.get('entry_order_id')
        assert not engine._lp.entry_send_inflight(session['session_id'])
        assert store.lp_actions(session['session_id']) == []
        assert Counter(row['token_id'] for row in signed) == Counter({'C': 1, 'E': 1, 'F': 1, 'G': 1})
        assert Counter(row['token_id'] for row in exchange.posts) == Counter({'C': 1, 'E': 1, 'F': 1, 'G': 1})
        assert all(p['price'] == Decimal('.39') and p['quantity'] == 20 and p['post_only'] is True for p in exchange.posts)
        assert exchange.cancels == [] and all(p['side'] == 'BUY' for p in exchange.posts)
        assert state['slots']['occupied'] == 4 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('31.20')
        siblings = [a for a in plan['actions'] if a.get('token_id') != 'B']
        assert len(siblings) == 4 and all(a['state'] == 'success' for a in siblings)
        assert len({a['action_id'] for a in siblings}) == len({a['order_id'] for a in siblings}) == 4
        original_audits = {a['session_id']: deepcopy(store.lp_actions(a['session_id'])) for a in siblings}
        assert all(len(rows) == 1 and rows[0]['state'] == 'accepted' and rows[0]['post_started'] is True for rows in original_audits.values())
        completed = datetime.fromisoformat(plan['completed_at'])
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=30)
        for elapsed in (1, 29):
            monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=elapsed))
            early = engine.lp_auto_run_once()
            assert early['last_round'] == plan and early['active_plan'] is None
            assert Counter(p['token_id'] for p in exchange.posts) == Counter({'C': 1, 'E': 1, 'F': 1, 'G': 1})
            assert {sid: store.lp_actions(sid) for sid in original_audits} == original_audits
            assert store.lp_actions(session['session_id']) == []
        monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=30))
        next_state = engine.lp_auto_run_once()
        fresh_b, = [a for a in next_state['last_round']['actions'] if a.get('token_id') == 'B']
        assert fresh_b['state'] == 'not_sent' and fresh_b['action_id'] != first['action_id']
        assert fresh_b['session_id'] != first['session_id']
        assert next_state['active_plan'] is None and next_state['slots']['occupied'] == 4
        assert Decimal(next_state['funds']['buy_reserved_usd']) == Decimal('31.20')
        assert Counter(p['token_id'] for p in exchange.posts) == Counter({'C': 1, 'E': 1, 'F': 1, 'G': 1})
        assert {sid: store.lp_actions(sid) for sid in original_audits} == original_audits
        assert store.lp_actions(session['session_id']) == []
    finally:
        with sqlite3.connect(store.path) as connection:
            connection.execute('DROP TRIGGER IF EXISTS fail_original_entry_audit')


@pytest.mark.parametrize('invalid', [
    'balance_transport', 'incomplete_account', 'wrong_identity',
    'generation_during_read', 'generation_during_planning',
])
def test_planning_requires_valid_account_facts(tmp_path, monkeypatch, invalid):
    from collections import Counter
    from dataclasses import asdict, is_dataclass
    from pathlib import Path
    import json
    _diagnostic_completed = False
    try:
        """Initial invalidity fails closed; later planning generation does not revoke the read."""
        engine, exchange, _, store = plan_setup(tmp_path, monkeypatch)
        read = exchange.lp_account_snapshot_shared
        reward = exchange.lp_reward_catalog
        armed = [True]
        reads, failed_at, observations = [], [], []

        def account(**kwargs):
            reads.append(pool.NOW)
            result = read(**kwargs)
            if armed[0] and invalid != 'generation_during_planning':
                # Failure ends after the read starts. The wait uses its end time.
                pool.NOW += timedelta(seconds=3)
                failed_at.append(pool.NOW)
                if invalid == 'balance_transport':
                    raise TimeoutError('simulated balance transport failure')
                if invalid == 'incomplete_account':
                    result['open_orders_complete'] = False
                elif invalid == 'wrong_identity':
                    result.update(account_id='another-wallet', wallet_address='another-wallet')
                else:
                    assert store.lp_advance_trade_generation(result['trade_generation'])
            return result

        def catalog(**kwargs):
            result = reward(**kwargs)
            if armed[0] and invalid == 'generation_during_planning' and not failed_at:
                import json, sqlite3
                with sqlite3.connect(store.path) as connection:
                    facts = json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])['account_financial_facts']
                assert facts['financial_status'] == 'known' and len(reads) == 1
                observations.append(facts)
                assert store.lp_advance_trade_generation(facts['trade_generation'])
                failed_at.append(pool.NOW)
            return result

        monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', account)
        monkeypatch.setattr(exchange, 'lp_reward_catalog', catalog)
        scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        assert len(failed_at) == 1
        if invalid == 'generation_during_planning':
            assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
            assert all(p['price'] == Decimal('.39') and p['quantity'] == 20 for p in exchange.posts)
            assert exchange.cancels == [] and len(reads) == 1
            assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
            assert state['active_plan'] is None and state['last_round']['completed_at']
            assert state['funds']['as_of'] == observations[0]['checked_at']
            assert state['funds']['status'] == 'unknown'
            assert 'account_financial_facts_changed' in state['admission_block_reasons']
            assert state['plan_wait']['kind'] == 'round'
            assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(
                state['last_round']['completed_at']) + timedelta(seconds=30)
            _diagnostic_completed = True
            return
        assert exchange.posts == exchange.cancels == []
        assert state['active_plan'] is None
        assert state['last_round']['completed_at']
        assert state['last_round']['actions'] == []
        assert state['plan_wait']['kind'] == 'round'
        started = datetime.fromisoformat(state['plan_wait']['started_at'])
        deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
        assert started == failed_at[0]
        assert deadline == started + timedelta(seconds=30)
        count = len(reads)
        for elapsed in (1, 29):
            monkeypatch.setattr(pool, 'NOW', started + timedelta(seconds=elapsed))
            scheduler.request_check()
            assert not scheduler.run_due()
            assert len(reads) == count
            assert exchange.posts == exchange.cancels == []
            assert engine.lp_auto_state()['active_plan'] is None
        armed[0] = False
        monkeypatch.setattr(pool, 'NOW', deadline)
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        assert len(reads) > count
        assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
        assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
        assert exchange.cancels == []
        assert state['last_round']['completed_at']
        assert state['plan_wait']['kind'] == 'round'
        _diagnostic_completed = True
    finally:
        _values = dict(locals())
        _store = _values.get('store')
        _service = _values.get('engine',_values.get('execution'))
        _external = _values.get('exchange',_values.get('account'))
        if _store is not None:
            _record = dict(completed=_diagnostic_completed,
                states={k:v for k,v in _values.items() if k in ('state','result','recovered','captured','publications','observation','plans','first','old_round','old_audits','reads','counts','failures','failed_at','observations','source','requests','original','audit','published','saved','signed','history_calls','baseline','actions','terms')},
                state=_service.lp_auto_state(),sessions=_store.lp_sessions(),
                audits={s['session_id']:_store.lp_actions(s['session_id']) for s in _store.lp_sessions()},
                posts=_external.posts,cancels=getattr(_external,'cancels',[]),orders=_external.orders,
                external_counts={k:getattr(_external,k,None) for k in ('balance_reads','order_reads','trade_reads','position_reads')})
            def _serialize(value):
                if is_dataclass(value):return asdict(value)
                if hasattr(value,'model_dump'):return value.model_dump(mode='json')
                return str(value)
            (Path(_store.path).parent/('final-direct25-'+'test_planning_requires_valid_account_facts'+'.json')).write_text(json.dumps(_record,indent=2,default=_serialize)+'\n')


@pytest.mark.parametrize('window', ['before_prepare', 'after_sign', 'long_lock', 'before_cancel'])
@pytest.mark.parametrize('financial_change', ['ttl', 'generation'])
def test_approved_plan_does_not_reapprove_account_finances(
    tmp_path, monkeypatch, window, financial_change,
):
    from collections import Counter
    from pathlib import Path
    from threading import Lock, get_ident
    from tests.test_lp_auto_target_convergence import publish_candidates, REWARDS
    import json
    import sqlite3
    initial = ('A','B','C','D','I') if window == 'before_cancel' else ()
    engine, exchange, lp, store = plan_setup(tmp_path,monkeypatch,initial)
    result_reader = install_order_result_reader(exchange,monkeypatch)
    scheduler = LPAutoScheduler(engine,clock=lambda:pool.NOW)
    saved, forbidden_reads, financial_reads, mutations, native_commits, packets, signatures = [], [], [], [], [], [], []
    native_generation = window == 'before_cancel' and financial_change == 'generation'
    unavailable_finance, held, armed = [False], [None], [window in ('before_prepare','long_lock') or native_generation]
    fixture_lock, caller = Lock(), get_ident()
    def invalidate():
        with fixture_lock:
            if saved:
                return
            snapshot = deepcopy(engine.lp_auto_state())
            assert snapshot['active_plan'] and snapshot['active_plan']['actions']
            saved.append(snapshot)
            before, generation = pool.NOW, store.lp_trade_generation()
            if financial_change == 'ttl':
                monkeypatch.setattr(pool,'NOW',pool.NOW+timedelta(seconds=61))
                won = None
            else:
                won = store.lp_advance_trade_generation(generation)
            mutations.append(dict(before=before,after=pool.NOW,generation=generation,
                resulting_generation=store.lp_trade_generation(),cas_won=won))
            if financial_change == 'generation':
                assert won, 'Chosen native generation CAS must succeed; report a sibling race rather than retry'
                assert mutations[-1]['resulting_generation'] > generation
            unavailable_finance[0] = True
    connect = sqlite3.connect
    class ApprovalConnection(sqlite3.Connection):
        def execute(self,sql,parameters=(),/):
            result = super().execute(sql,parameters)
            if sql.strip().upper() == 'COMMIT' and armed[0] and get_ident() == caller:
                row = super().execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
                document = json.loads(row[0]) if row else {}
                plan = document.get('active_plan')
                if plan is not None:
                    armed[0] = False
                    assert not self.in_transaction
                    assert [t['token_id'] for t in plan['targets']] == ['B','C','E','F','G']
                    if initial:
                        cancels = [a for a in plan['actions'] if a['kind'] == 'cancel']
                        dependent = [a for a in plan['actions'] if a['kind'] == 'buy']
                        assert {a['order_id'] for a in cancels} == {'original-A','original-D','original-I'}
                        assert all(a['state'] == 'pending' for a in cancels)
                        assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
                    else:
                        assert all(a['kind'] == 'buy' and a['state'] == 'pending' for a in plan['actions'])
                    assert super().execute('SELECT COUNT(*) FROM lp_actions').fetchone()[0] == 0
                    assert exchange.posts == exchange.cancels == signatures == []
                    native_commits.append(dict(round_id=plan['round_id'],thread=caller,in_transaction=False))
                    invalidate()
                    if window == 'long_lock':
                        held[0] = engine._lock_path.open('a+')
                        fcntl.flock(held[0].fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            return result
    def observed_connect(database,*args,**kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs.setdefault('factory',ApprovalConnection)
        return connect(database,*args,**kwargs)
    if window in ('before_prepare','long_lock') or native_generation:
        monkeypatch.setattr(sqlite3,'connect',observed_connect)
    for name in ('lp_account_snapshot','lp_account_snapshot_shared'):
        read = getattr(exchange,name)
        def observed(*args,_read=read,_name=name,**kwargs):
            active = engine.lp_auto_state()['active_plan'] is not None
            financial_reads.append(dict(method=_name,active=active,failed=unavailable_finance[0],at=pool.NOW))
            if active:
                forbidden_reads.append(_name)
                raise TimeoutError('approved execution must not reread financial transport')
            if unavailable_finance[0]:
                raise TimeoutError('simulated next planning financial transport unavailable')
            return _read(*args,**kwargs)
        monkeypatch.setattr(exchange,name,observed)
    sign = exchange.lp_create_limit_order
    def signer(**kwargs):
        signed = sign(**kwargs)
        signatures.append(deepcopy(signed))
        if window == 'after_sign' and kwargs['token_id'] == 'B':
            invalidate()
        return signed
    monkeypatch.setattr(exchange,'lp_create_limit_order',signer)
    def current_results(request):
        active = engine.lp_auto_state()['active_plan']
        if window == 'before_cancel' and active is not None and not saved:
            cancels = [a for a in active['actions'] if a['kind'] == 'cancel']
            dependent = [a for a in active['actions'] if a['kind'] == 'buy']
            assert {a['order_id'] for a in cancels} == {'original-A','original-D','original-I'}
            assert all(a['state'] == 'pending' for a in cancels)
            assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
            assert exchange.posts == exchange.cancels == []
            assert not any(a['role'] == 'reconciliation_cancel' for row in store.lp_sessions() for a in store.lp_actions(row['session_id']))
            invalidate()
        packet = result_reader(request)
        packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange,'lp_order_result_snapshot',current_results)
    try:
        assert scheduler.run_due()
        first = engine.lp_auto_state()
        assert len(saved) == len(mutations) == 1 and forbidden_reads == []
        if window in ('before_prepare','long_lock') or native_generation:
            assert len(native_commits) == 1 and native_commits[0]['in_transaction'] is False
        if financial_change == 'ttl':
            assert mutations[0]['after'] == mutations[0]['before']+timedelta(seconds=61)
        original = deepcopy(first['last_round'])
        approved = saved[0]
        assert original['round_id'] == approved['active_plan']['round_id']
        assert original['targets'] == approved['last_round']['targets']
        assert [a['action_id'] for a in original['actions']] == [a['action_id'] for a in approved['active_plan']['actions']]
        assert [t['token_id'] for t in original['targets']] == ['B','C','E','F','G']
        assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in original['targets'])
        assert original['completed_at'] and first['active_plan'] is None and first['plan_wait']['kind'] == 'round'
        completed = datetime.fromisoformat(original['completed_at'])
        deadline = datetime.fromisoformat(first['plan_wait']['deadline'])
        assert deadline == completed+timedelta(seconds=30)
        assert first['funds']['as_of'] == approved['funds']['as_of']
        old_audits = {row['session_id']:deepcopy(store.lp_actions(row['session_id'])) for row in store.lp_sessions()}
        if window == 'long_lock':
            assert exchange.posts == exchange.cancels == []
            assert all(a['state'] == 'not_sent' and a['reason'] == 'execution_lock' for a in original['actions'])
            assert all(a.get('post_started') is not True for audits in old_audits.values() for a in audits)
        elif initial:
            assert Counter(exchange.cancels) == Counter(['original-A','original-D','original-I']) and exchange.posts == []
            assert live_orders(exchange) == {'B':'original-B','C':'original-C'}
            assert sum(Decimal(o['price'])*(Decimal(o['original_size'])-Decimal(o['size_matched']))
                for o in exchange.orders if o['status'] == 'LIVE') == Decimal('15.60')
            dependent = [a for a in original['actions'] if a['kind'] == 'buy']
            assert {a['token_id'] for a in dependent} == {'E','F','G'}
            assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
            for action in original['actions']:
                if action['kind'] == 'cancel':
                    audit, = [a for a in old_audits[action['session_id']] if a['role'] == 'reconciliation_cancel']
                    assert audit['state'] == 'accepted' and audit['post_started'] is True and audit['order_id'] == action['order_id']
            assert packets and packets[0]['read_started_at'] >= mutations[0]['after']
            if native_generation:
                assert all(packets[0][k] is True for k in ('authenticated','open_orders_complete',
                    'positions_complete','trades_complete','pagination_complete'))
                assert packets[0]['wallet_address'] == 'test-wallet'
                assert {o['order_id'] for o in packets[0]['orders']} == {'original-A','original-B','original-C','original-D','original-I'}
                assert all(o['status'] == 'LIVE' and o['side'] == 'BUY' and Decimal(o['price']) == Decimal('.39')
                    and Decimal(o['original_size']) == 20 and Decimal(o['size_matched']) == 0 for o in packets[0]['orders'])
        else:
            assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B','C','E','F','G'])
            assert exchange.cancels == [] and all(a['state'] == 'success' for a in original['actions'])
            assert first['slots']['occupied'] == 5 and Decimal(first['funds']['buy_reserved_usd']) == 39
        before = (len(financial_reads),len(packets),len(signatures),len(exchange.posts),len(exchange.cancels))
        monkeypatch.setattr(pool,'NOW',deadline-timedelta(seconds=1))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert (len(financial_reads),len(packets),len(signatures),len(exchange.posts),len(exchange.cancels)) == before
        assert {sid:store.lp_actions(sid) for sid in old_audits} == old_audits
        state = first
        if window == 'long_lock':
            monkeypatch.setattr(pool,'NOW',deadline)
            assert scheduler.run_due()
            failed = engine.lp_auto_state()
            assert failed['last_round']['round_id'] != original['round_id']
            assert failed['last_round']['completed_at'] and failed['active_plan'] is None
            assert failed['last_round']['reason'] == 'account_order_sync_unknown'
            assert failed['last_round']['actions'] == [] and exchange.posts == exchange.cancels == []
            assert any(r['failed'] and not r['active'] for r in financial_reads)
            assert {sid:store.lp_actions(sid) for sid in old_audits} == old_audits
            fcntl.flock(held[0].fileno(),fcntl.LOCK_UN)
            held[0].close()
            held[0] = None
            unavailable_finance[0] = False
            monkeypatch.setattr(pool,'NOW',datetime.fromisoformat(failed['plan_wait']['deadline']))
            publish_candidates(exchange,lp,store,REWARDS)
            assert scheduler.run_due()
            state = engine.lp_auto_state()
        elif initial:
            unavailable_finance[0] = False
            monkeypatch.setattr(pool,'NOW',deadline)
            publish_candidates(exchange,lp,store,REWARDS)
            assert scheduler.run_due()
            state = engine.lp_auto_state()
        if window == 'long_lock' or initial:
            assert state['last_round']['round_id'] != original['round_id']
            new_buys = [a for a in state['last_round']['actions'] if a['kind'] == 'buy']
            assert {a['token_id'] for a in new_buys} == ({'E','F','G'} if initial else {'B','C','E','F','G'})
            assert {a['action_id'] for a in new_buys}.isdisjoint({a['action_id'] for a in original['actions']})
            assert all(a.get('session_id') and a['session_id'] not in old_audits and a['state'] == 'success' for a in new_buys)
            assert {sid:store.lp_actions(sid) for sid in old_audits} == old_audits
        assert Counter(p['token_id'] for p in exchange.posts) == Counter(['E','F','G'] if initial else ['B','C','E','F','G'])
        assert all(p['price'] == Decimal('.39') and p['quantity'] == 20 and p['post_only'] is True for p in exchange.posts)
        assert Counter(exchange.cancels) == Counter(['original-A','original-D','original-I'] if initial else [])
        assert set(live_orders(exchange)) == {'B','C','E','F','G'}
        if initial:
            assert live_orders(exchange)['B'] == 'original-B' and live_orders(exchange)['C'] == 'original-C'
        assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
        assert state['active_plan'] is None and state['last_round']['completed_at']
        assert forbidden_reads == [] and not any(r['active'] for r in financial_reads)
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(state['last_round']['completed_at'])+timedelta(seconds=30)
    finally:
        if held[0] is not None:
            fcntl.flock(held[0].fileno(),fcntl.LOCK_UN)
            held[0].close()

@pytest.mark.parametrize('rejection', [
    'balance_insufficient', 'allowance_insufficient', 'venue_denied', 'sdk_not_enough_balance',
])
def test_definite_buy_rejection_finishes_only_that_action(tmp_path, monkeypatch, rejection):
    from collections import Counter

    engine, exchange, _, store = plan_setup(tmp_path, monkeypatch)
    install_order_result_reader(exchange, monkeypatch)
    post = exchange.lp_post_order
    reject_once = [True]
    expected_reason = 'not_enough_balance' if rejection == 'sdk_not_enough_balance' else rejection

    def venue(signed):
        if signed['token_id'] == 'B' and reject_once[0]:
            reject_once[0] = False
            exchange.posts.append(signed)
            if rejection == 'sdk_not_enough_balance':
                from polymarket.models.clob import RejectedOrder
                return RejectedOrder(code='not_enough_balance', message='not enough balance / allowance')
            return dict(accepted=False, status='REJECTED', reason=rejection)
        return post(signed)

    monkeypatch.setattr(exchange, 'lp_post_order', venue)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert Counter(p['token_id'] for p in exchange.posts) == Counter({'B': 1, 'C': 1, 'E': 1, 'F': 1, 'G': 1})
    assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
    assert exchange.cancels == []
    first, *others = state['last_round']['actions']
    assert first['state'] == 'rejected' and first['reason'] == expected_reason
    assert all(a['state'] == 'success' for a in others)
    session = store.lp_session(first['session_id'])
    audit = store.lp_actions(first['session_id'])
    assert session['state'] == 'entry_rejected' and session['reason'] == expected_reason
    assert len(audit) == 1 and audit[0]['state'] == 'rejected'
    assert audit[0]['post_started'] is True and audit[0]['submit_stage'] == 'exchange_rejected'
    assert audit[0]['reason'] == expected_reason
    if rejection == 'sdk_not_enough_balance':
        assert session['exchange_rejection_message'] == 'not enough balance / allowance'
        assert audit[0]['exchange_rejection_message'] == 'not enough balance / allowance'
    assert state['last_round']['completed_at']
    assert state['active_plan'] is None and state['plan_wait']['kind'] == 'round'
    completed = datetime.fromisoformat(state['last_round']['completed_at'])
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=30)
    for elapsed in (0, 29):
        monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=elapsed))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert len(exchange.posts) == 5
        assert store.lp_actions(first['session_id']) == audit
    monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=30))
    assert scheduler.run_due()
    assert Counter(p['token_id'] for p in exchange.posts) == Counter({'B': 2, 'C': 1, 'E': 1, 'F': 1, 'G': 1})
    fresh_b, = [a for a in engine.lp_auto_state()['last_round']['actions'] if a.get('token_id') == 'B']
    assert fresh_b['action_id'] != first['action_id']
    assert fresh_b['session_id'] != first['session_id']
    assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
    assert store.lp_actions(first['session_id']) == audit


@pytest.mark.parametrize('unavailable', ['metadata', 'book'])
@pytest.mark.parametrize('planned_action', ['new_B', 'old_A'])
def test_unavailable_planned_market_keeps_existing_order_and_continues(
    tmp_path, monkeypatch, unavailable, planned_action,
):
    initial = ('A', 'B', 'C', 'D', 'I') if planned_action == 'old_A' else ()
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, initial)
    install_order_result_reader(exchange, monkeypatch)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    with engine._lock_path.open('a+') as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert scheduler.run_due()
            original = engine.lp_auto_state()['active_plan']
            assert original and exchange.posts == exchange.cancels == []
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    token = 'A' if initial else 'B'
    method = 'lp_market_metadata_fresh' if unavailable == 'metadata' else 'lp_order_books'
    read = getattr(exchange, method)
    failures = []

    def absent(ids, **kwargs):
        result = read(ids, **kwargs)
        if token in ids:
            failures.append(token)
            result.pop(token, None)
        return result

    monkeypatch.setattr(exchange, method, absent)
    old_session = next((r['session_id'] for r in original['victims'] if r['condition_id'] == token), None)
    protection = deepcopy(store.lp_session(old_session)['queue_protection']) if old_session else None
    monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']))
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    if initial:
        assert exchange.cancels == ['original-D', 'original-I']
        if any(a['state'] == 'canceling' for a in state['last_round']['actions']):
            assert state['plan_wait']['kind'] == 'order'
            monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(state['plan_wait']['deadline']))
            assert scheduler.run_due()
            state = engine.lp_auto_state()
        assert live_orders(exchange)['A'] == 'original-A'
        assert store.lp_session(old_session)['queue_protection'] == protection
        assert [p['token_id'] for p in exchange.posts] == ['E', 'F']
        assert set(live_orders(exchange)) == {'A', 'B', 'C', 'E', 'F'}
    else:
        assert [p['token_id'] for p in exchange.posts] == ['C', 'E', 'F', 'G']
        assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
        assert state['last_round']['completed_at']
    assert failures
    assert state['last_round']['round_id'] == original['round_id']
    action = next(a for a in state['last_round']['actions'] if a['condition_id'] == token)
    assert action['state'] == 'rejected'
    assert action['reason'] == ('candidate_market_unknown' if unavailable == 'metadata' else 'book_unknown')
    assert state['plan_wait']['kind'] != 'api'
    assert not any(p['token_id'] == 'H' for p in exchange.posts)


@pytest.mark.parametrize('first_buy_result', ['accepted', 'unknown'])
def test_retained_order_skips_only_the_buy_without_a_slot(tmp_path, monkeypatch, first_buy_result):
    import json
    import sqlite3
    from collections import Counter
    from tests.test_lp_auto_plan_scheduler import restart_engine
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    original_orders = deepcopy(exchange.orders)
    reader = install_order_result_reader(exchange, monkeypatch)
    phase, evidence = ['original_refusal'], dict(case=first_buy_result, states=[], full_reads=[], result_reads=[], preparations=[])
    def document():
        with sqlite3.connect(store.path) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state = deepcopy(engine.lp_auto_state())
        evidence['states'].append(dict(label=label,state=state,document=document(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=deepcopy(exchange.orders),posts=deepcopy(exchange.posts),cancels=list(exchange.cancels)))
        return state
    def result_packet(request):
        packet=reader(request)
        for field in ('orders','open_orders'):
            for row in packet[field]:
                if (row.get('side')=='BUY' and row.get('token_id')==row.get('condition_id')
                        and row.get('status') in ('LIVE','CANCELED') and row.get('fill_quantity_known') in (None,True)
                        and Decimal(str(row['price']))==Decimal('.39') and Decimal(str(row['original_size']))==20
                        and Decimal(str(row['size_matched']))==0):
                    row['fill_quantity_known']=True
        evidence['result_reads'].append(deepcopy(dict(phase=phase[0],request=request,packet=packet)))
        return packet
    monkeypatch.setattr(exchange,'lp_order_result_snapshot',result_packet)
    for name in ('lp_account_snapshot','lp_account_snapshot_shared'):
        read=getattr(exchange,name)
        def full(*args,_read=read,_name=name,**kwargs):
            packet=_read(*args,**kwargs)
            evidence['full_reads'].append(deepcopy(dict(phase=phase[0],method=_name,
                active_plan=bool(engine.lp_auto_state()['active_plan']),packet=packet)))
            return packet
        monkeypatch.setattr(exchange,name,full)
    post=exchange.lp_post_order
    if first_buy_result=='unknown':
        def venue(signed):
            if signed['token_id']=='E':
                exchange.posts.append(signed)
                raise TimeoutError('E original POST result unknown')
            return post(signed)
        monkeypatch.setattr(exchange,'lp_post_order',venue)
    metadata=exchange.lp_market_metadata_fresh
    unavailable=[False]
    def local_market(ids,**kwargs):
        result=metadata(ids,**kwargs)
        if unavailable[0]:
            result.pop('A',None)
        return result
    monkeypatch.setattr(exchange,'lp_market_metadata_fresh',local_market)
    catalog_read=exchange.lp_reward_catalog
    def cold_prepare(service):
        def catalog(*,condition_ids=None,**kwargs):
            result=catalog_read(condition_ids=tuple(exchange.rewards) if condition_ids is None else condition_ids,**kwargs)
            return {**result,'state':'known','complete':True}
        def history(token_ids,*,start_ts,end_ts,**kwargs):
            return dict(state='known',history={t:[dict(t=start_ts,p='.390'),dict(t=end_ts,p='.391')] for t in token_ids})
        def competition(**kwargs):
            return dict(state='known',complete=True,checked_at=pool.NOW,round_checked_at=pool.NOW,
                competitiveness={t:(Decimal('2.5'),pool.NOW) for t in exchange.rewards})
        monkeypatch.setattr(exchange,'lp_reward_catalog',catalog)
        monkeypatch.setattr(exchange,'lp_market_metadata',exchange.lp_market_metadata_fresh,raising=False)
        monkeypatch.setattr(exchange,'lp_price_history',history,raising=False)
        monkeypatch.setattr(exchange,'lp_market_competitiveness',competition,raising=False)
        competition_state=service.refresh_competition_cache()
        prepared=service.refresh_price_history()
        qualified=service.refresh_candidates(force=True)
        evidence['preparations'].append(deepcopy(dict(competition=competition_state,prepared=prepared,qualified=qualified)))
        assert competition_state['state']=='known' and prepared['preparation_outcome']=='success'
    scheduler=LPAutoScheduler(engine,clock=lambda:pool.NOW)
    try:
        with engine._lock_path.open('a+') as held:
            fcntl.flock(held.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:
                assert scheduler.run_due()
                refused=capture('native_original_refusal')
                original=deepcopy(refused['last_round'])
                assert refused['active_plan'] is None and original['completed_at']
                assert all(a['state']=='not_sent' for a in original['actions'])
                assert exchange.cancels==exchange.posts==[] and exchange.orders==original_orders
                assert refused['slots']['occupied']==5 and Decimal(refused['funds']['buy_reserved_usd'])==39
                assert datetime.fromisoformat(refused['plan_wait']['deadline'])==datetime.fromisoformat(original['completed_at'])+timedelta(seconds=30)
            finally:
                fcntl.flock(held.fileno(),fcntl.LOCK_UN)
        unavailable[0]=True
        phase[0]='current_retained_A_cancel_DI'
        monkeypatch.setattr(pool,'NOW',datetime.fromisoformat(refused['plan_wait']['deadline']))
        lp.refresh_candidate_recommendations()
        assert scheduler.run_due()
        canceling=capture('new_current_DI_only')
        second=deepcopy(canceling['last_round'])
        assert canceling['active_plan'] is None and second['completed_at'] and second['round_id']!=original['round_id']
        assert exchange.cancels==['original-D','original-I'] and exchange.posts==[]
        assert live_orders(exchange)['A']=='original-A'
        assert {a['order_id'] for a in second['actions'] if a['kind']=='cancel'}=={'original-D','original-I'}
        assert not [a for a in second['actions'] if a['kind']=='cancel' and a['order_id']=='original-A']
        original_keys={a['action_id'] for a in original['actions']}
        assert all(a['action_id'] not in original_keys for a in second['actions'])
        di_audits={a['session_id']:deepcopy(store.lp_actions(a['session_id'])) for a in second['actions'] if a['kind']=='cancel'}
        assert datetime.fromisoformat(canceling['plan_wait']['deadline'])==datetime.fromisoformat(second['completed_at'])+timedelta(seconds=30)
        phase[0]='following_current_two_free_BUYs'
        monkeypatch.setattr(pool,'NOW',datetime.fromisoformat(canceling['plan_wait']['deadline']))
        lp.refresh_candidate_recommendations()
        assert scheduler.run_due()
        state=capture('current_EF_only')
        third=deepcopy(state['last_round'])
        assert state['active_plan'] is None and third['completed_at'] and third['round_id'] not in {original['round_id'],second['round_id']}
        assert Counter(p['token_id'] for p in exchange.posts)==Counter(['E','F'])
        assert exchange.cancels==['original-D','original-I']
        physical=live_orders(exchange)
        assert {k:physical[k] for k in ('A','B','C')}=={k:'original-'+k for k in ('A','B','C')}
        assert set(physical)==({'A','B','C','E','F'} if first_buy_result=='accepted' else {'A','B','C','F'})
        assert all(Decimal(p['price'])==Decimal('.39') and Decimal(p['quantity'])==20 and p['post_only'] is True for p in exchange.posts)
        buys={a['token_id']:a for a in third['actions'] if a['kind']=='buy'}
        assert set(buys)=={'E','F'}
        assert buys['E']['state']==('success' if first_buy_result=='accepted' else 'unknown') and buys['F']['state']=='success'
        assert not any(s.get('token_id')=='G' for s in store.lp_sessions())
        assert state['slots']['occupied']==5 and Decimal(state['funds']['buy_reserved_usd'])==39
        for sid,audits in di_audits.items():assert store.lp_actions(sid)==audits
        assert {t['condition_id'] for t in third['targets']}=={'A','B','C','E','F'}
        assert all(Decimal(t['price'])==Decimal('.39') and Decimal(t['quantity'])==20 for t in third['targets'])
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(third['completed_at'])+timedelta(seconds=30)
        posts,cancels=deepcopy(exchange.posts),list(exchange.cancels)
        if first_buy_result=='unknown':
            original_e=deepcopy(buys['E']);sid=original_e['session_id'];request_id=original_e.get('request_id',original_e['action_id'])
            audit=deepcopy(store.lp_actions(sid));assert len(audit)==1 and audit[0]['state']=='unknown' and audit[0]['post_started'] is True
            assert not audit[0].get('order_id') and not store.lp_session(sid).get('entry_order_id')
            old_intent=deepcopy(document()['intents'][request_id]);assert old_intent['state']=='unknown' and not old_intent.get('order_id')
            assert old_intent['settled'] is False and Decimal(old_intent['reserved_usd'])==Decimal('7.80')
            engine=restart_engine(engine,exchange);scheduler=LPAutoScheduler(engine,clock=lambda:pool.NOW)
            for seconds in (1,29):
                monkeypatch.setattr(pool,'NOW',datetime.fromisoformat(third['completed_at'])+timedelta(seconds=seconds))
                scheduler.request_check();assert not scheduler.run_due();engine.lp_auto_run_once(round_id=original['round_id'])
                assert exchange.posts==posts and exchange.cancels==cancels and store.lp_actions(sid)==audit
            phase[0]='paused_current_full_coverage'
            engine.lp_auto_set_desired_running(False)
            monkeypatch.setattr(pool,'NOW',datetime.fromisoformat(state['plan_wait']['deadline']))
            assert scheduler.run_due()
            covered=capture('paused_authentic_E_coverage')
            assert covered['active_plan'] is None and covered['last_round']['actions']==[] and covered['last_round']['checked_at']
            assert covered['last_round']['round_id']!=third['round_id']
            assert datetime.fromisoformat(covered['plan_wait']['deadline'])==datetime.fromisoformat(covered['plan_wait']['started_at'])+timedelta(seconds=30)
            current=document();intent=current['intents'][request_id];financial=current['account_financial_facts'];marker=intent['reservation_coverage']
            packet,=[r['packet'] for r in evidence['full_reads'] if r['phase']==phase[0] and r['method']=='lp_account_snapshot_shared']
            assert all(packet[k] is True for k in ('authenticated','balance_complete','open_orders_complete','pagination_complete','positions_complete','trades_complete'))
            assert packet['wallet_address']==marker['account_id']==financial['account_id']=='test-wallet'
            assert marker['state']=='covered' and marker['intent_id']==request_id and marker['session_id']==sid
            assert marker['snapshot_id']==financial['snapshot_id'] and marker['pool_account_id']==current['account_id']
            assert datetime.fromisoformat(marker['read_started_at'])==datetime.fromisoformat(str(packet['read_started_at']))
            assert packet['trade_generation']==financial['trade_generation']
            assert {r['order_id'] for r in packet['open_orders']}==set(physical.values()) and packet['positions']==[] and packet['raw_trades']==[]
            assert intent['state']=='unknown' and intent['settled'] is False and not intent.get('order_id')
            assert intent['session_id']==sid and Decimal(intent['reserved_usd'])==Decimal('7.80')
            assert store.lp_actions(sid)==audit and not store.lp_session(sid).get('entry_order_id')
            assert covered['slots']['occupied']==4 and Decimal(covered['funds']['buy_reserved_usd'])==Decimal('31.20')
            assert exchange.posts==posts and exchange.cancels==cancels and sum(p['token_id']=='E' for p in exchange.posts)==1
        else:
            unavailable[0]=False
            monkeypatch.setattr(pool,'NOW',datetime.fromisoformat(state['plan_wait']['deadline'])-timedelta(seconds=1))
            scheduler.request_check();assert not scheduler.run_due();engine.lp_auto_run_once(round_id=original['round_id'])
            assert exchange.posts==posts and exchange.cancels==cancels
            phase[0]='next_current_A_new_cancel'
            monkeypatch.setattr(pool,'NOW',datetime.fromisoformat(state['plan_wait']['deadline']))
            cold_prepare(lp)
            assert scheduler.run_due()
            final=capture('distinct_current_A_cancel_no_G_prespend')
            assert final['active_plan'] is None and final['last_round']['completed_at']
            assert final['last_round']['round_id'] not in {original['round_id'],second['round_id'],third['round_id']}
            action,=[a for a in final['last_round']['actions'] if a['kind']=='cancel' and a['order_id']=='original-A']
            assert action['action_id'] not in original_keys and action['state']=='canceling'
            a_audit,=[a for a in store.lp_actions(action['session_id']) if a.get('order_id')=='original-A']
            assert a_audit['rotation_round_id']==final['last_round']['round_id'] and a_audit['state']=='accepted'
            assert exchange.cancels==['original-D','original-I','original-A'] and exchange.posts==posts
            assert not any(p['token_id']=='G' for p in exchange.posts) and not any(s.get('token_id')=='G' for s in store.lp_sessions())
        assert not any(r['active_plan'] for r in evidence['full_reads'])
        evidence['stage']='complete'
    finally:
        evidence.update(clock=pool.NOW,state=engine.lp_auto_state(),document=document(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=exchange.orders,posts=exchange.posts,cancels=exchange.cancels)
        (tmp_path/'RF-retained-diagnostics.json').write_text(json.dumps(evidence,default=str,indent=2)+'\n')


@pytest.mark.parametrize('carrier', ['mapping', 'sdk'])
def test_definite_cancel_rejection_is_not_retried_in_this_round(tmp_path, monkeypatch, request, carrier):
    import json
    from collections import Counter
    monkeypatch.setattr(pool,'NOW',pool.NOW)
    if carrier=='sdk':
        from polymarket.models.clob.cancel import CancelOrdersResponse
        from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic,NOW
        from tests.test_lp_order_registration_contract import _open_order
        sdk_runtime=request.getfixturevalue('runtime')
        orders=(_open_order('rank-a','BUY',price='.39',original='20'),_open_order('rank-b','BUY',price='.39',original='20'))
        store,adapter,account,lp,engine=sdk_runtime(orders=orders,public_client=_CandidateSDKPublic(NOW))
        engine.lp_auto_configure(dict(budget_usd='39',target_buy_count=1,buy_price_level=2))
        engine.lp_auto_set_desired_running(True)
        evidence=dict(carrier=carrier)
        def venue(*,order_ids):
            account.cancels.append(order_ids);assert order_ids==('rank-b',)
            return CancelOrdersResponse(canceled=(),not_canceled={'rank-b':'venue_denied'})
        monkeypatch.setattr(account,'cancel_orders',venue)
        scheduler=LPAutoScheduler(engine,clock=lambda:sdk_runtime.clock[0])
        try:
            assert scheduler.run_due()
            state=engine.lp_auto_state();original=deepcopy(state['last_round']);action,=original['actions']
            assert action['order_id']=='rank-b' and action['state']=='rejected' and action['reason']=='venue_denied'
            assert account.cancels==[('rank-b',)] and account.posts==[] and account.orders==orders
            assert state['active_plan'] is None and original['completed_at'] and state['plan_wait']['kind']=='round'
            assert state['slots']['occupied']==2 and Decimal(state['funds']['buy_reserved_usd'])==Decimal('15.60')
            assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(original['completed_at'])+timedelta(seconds=30)
            audit=[a for a in store.lp_actions(action['session_id']) if a.get('order_id')=='rank-b']
            assert len(audit)==1 and audit[0]['state']=='rejected' and audit[0]['reason']=='venue_denied'
            assert audit[0]['rotation_round_id']==original['round_id']
            scheduler.request_check();assert not scheduler.run_due();engine.lp_auto_run_once();engine.lp_tick()
            assert account.cancels==[('rank-b',)] and account.posts==[]
            assert [a for a in store.lp_actions(action['session_id']) if a.get('order_id')=='rank-b']==audit
            evidence['stage']='complete'
        finally:
            evidence.update(state=engine.lp_auto_state(),orders=account.orders,cancels=account.cancels,posts=account.posts,
                audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()})
            (tmp_path/'RF-refusal-sdk-diagnostics.json').write_text(json.dumps(evidence,default=str,indent=2)+'\n')
        return
    import sqlite3
    from tests.test_lp_auto_plan_scheduler import restart_engine
    engine,exchange,lp,store=plan_setup(tmp_path,monkeypatch,('A','B','C','D','I'))
    reader=install_order_result_reader(exchange,monkeypatch)
    phase,evidence=['original_denied_round'],dict(carrier=carrier,states=[],result_reads=[],full_reads=[],preparations=[])
    def document():
        with sqlite3.connect(store.path) as connection:return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state=deepcopy(engine.lp_auto_state());evidence['states'].append(dict(label=label,state=state,document=document(),
            sessions=store.lp_sessions(),audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=deepcopy(exchange.orders),cancels=list(exchange.cancels),posts=deepcopy(exchange.posts)));return state
    def results(request):
        packet=reader(request)
        for field in ('orders','open_orders'):
            for row in packet[field]:
                if (row.get('side')=='BUY' and row.get('token_id')==row.get('condition_id') and row.get('status') in ('LIVE','CANCELED')
                        and row.get('fill_quantity_known') in (None,True) and Decimal(str(row['price']))==Decimal('.39')
                        and Decimal(str(row['original_size']))==20 and Decimal(str(row['size_matched']))==0):row['fill_quantity_known']=True
        evidence['result_reads'].append(deepcopy(dict(phase=phase[0],request=request,packet=packet)));return packet
    monkeypatch.setattr(exchange,'lp_order_result_snapshot',results)
    for name in ('lp_account_snapshot','lp_account_snapshot_shared'):
        read=getattr(exchange,name)
        def full(*args,_read=read,_name=name,**kwargs):
            packet=_read(*args,**kwargs);evidence['full_reads'].append(deepcopy(dict(phase=phase[0],method=_name,
                active_plan=bool(engine.lp_auto_state()['active_plan']),packet=packet)));return packet
        monkeypatch.setattr(exchange,name,full)
    cancel=exchange.cancel_order;rejected=[True]
    def venue(order_id):
        if order_id=='original-A' and rejected[0]:
            exchange.cancels.append(order_id);return dict(canceled=[],not_canceled={order_id:'venue_denied'})
        return cancel(order_id)
    monkeypatch.setattr(exchange,'cancel_order',venue)
    catalog_read=exchange.lp_reward_catalog
    def cold_prepare(service):
        def catalog(*,condition_ids=None,**kwargs):
            result=catalog_read(condition_ids=tuple(exchange.rewards) if condition_ids is None else condition_ids,**kwargs)
            return {**result,'state':'known','complete':True}
        def history(token_ids,*,start_ts,end_ts,**kwargs):return dict(state='known',history={t:[dict(t=start_ts,p='.390'),dict(t=end_ts,p='.391')] for t in token_ids})
        def competition(**kwargs):return dict(state='known',complete=True,checked_at=pool.NOW,round_checked_at=pool.NOW,
            competitiveness={t:(Decimal('2.5'),pool.NOW) for t in exchange.rewards})
        monkeypatch.setattr(exchange,'lp_reward_catalog',catalog);monkeypatch.setattr(exchange,'lp_market_metadata',exchange.lp_market_metadata_fresh,raising=False)
        monkeypatch.setattr(exchange,'lp_price_history',history,raising=False);monkeypatch.setattr(exchange,'lp_market_competitiveness',competition,raising=False)
        competition_state=service.refresh_competition_cache();prepared=service.refresh_price_history();qualified=service.refresh_candidates(force=True)
        evidence['preparations'].append(deepcopy(dict(competition=competition_state,prepared=prepared,qualified=qualified)))
        assert competition_state['state']=='known' and prepared['preparation_outcome']=='success'
    scheduler=LPAutoScheduler(engine,clock=lambda:pool.NOW)
    try:
        assert scheduler.run_due()
        state=capture('original_denied_A_DI_ack_completed');original=deepcopy(state['last_round'])
        assert state['active_plan'] is None and original['completed_at']
        action,=[a for a in original['actions'] if a['kind']=='cancel' and a['condition_id']=='A']
        assert action['state']=='rejected' and action['reason']=='venue_denied'
        original_session=action['session_id'];audit=deepcopy([a for a in store.lp_actions(original_session) if a.get('order_id')=='original-A'])
        assert len(audit)==1 and audit[0]['state']=='rejected' and audit[0]['reason']=='venue_denied'
        assert audit[0]['rotation_round_id']==original['round_id'] and live_orders(exchange)['A']=='original-A'
        assert exchange.cancels==['original-A','original-D','original-I'] and exchange.posts==[]
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(original['completed_at'])+timedelta(seconds=30)
        scheduler.request_check();assert not scheduler.run_due();engine.lp_auto_run_once(round_id=original['round_id']);engine.lp_tick()
        assert exchange.cancels.count('original-A')==1 and exchange.posts==[]
        engine=restart_engine(engine,exchange);scheduler=LPAutoScheduler(engine,clock=lambda:pool.NOW)
        assert engine.lp_auto_state()['plan_wait']==state['plan_wait']
        scheduler.request_check();assert not scheduler.run_due();engine.lp_tick()
        assert exchange.cancels.count('original-A')==1 and exchange.posts==[]
        assert [a for a in store.lp_actions(original_session) if a.get('order_id')=='original-A']==audit
        engine.lp_auto_set_desired_running(False);phase[0]='paused_current_original_denial_cleanup'
        monkeypatch.setattr(pool,'NOW',datetime.fromisoformat(state['plan_wait']['deadline']))
        assert scheduler.run_due()
        cleaned=capture('paused_current_rejected_owner_cleanup')
        assert cleaned['active_plan'] is None and cleaned['last_round']['actions']==[] and cleaned['last_round']['checked_at']
        assert cleaned['last_round']['round_id']!=original['round_id']
        assert datetime.fromisoformat(cleaned['plan_wait']['deadline'])==datetime.fromisoformat(cleaned['plan_wait']['started_at'])+timedelta(seconds=30)
        assert cleaned['slots']['occupied']==3 and Decimal(cleaned['funds']['buy_reserved_usd'])==Decimal('23.40')
        assert live_orders(exchange)=={k:'original-'+k for k in ('A','B','C')} and exchange.posts==[]
        assert exchange.cancels==['original-A','original-D','original-I']
        session=store.lp_session(original_session)
        assert session['entry_cancel_requested'] is False
        assert not session.get('yield_rotation_cancel_owners')
        assert all(b['state'] in ('registered','monitoring') for b in engine._lp._queue_protection_levels(session).values())
        assert [a for a in store.lp_actions(original_session) if a.get('order_id')=='original-A']==audit
        paused_deadline=datetime.fromisoformat(cleaned['plan_wait']['deadline'])
        engine=restart_engine(engine,exchange);scheduler=LPAutoScheduler(engine,clock=lambda:pool.NOW)
        monkeypatch.setattr(pool,'NOW',paused_deadline-timedelta(seconds=1))
        scheduler.request_check();assert not scheduler.run_due();engine.lp_auto_run_once(round_id=original['round_id'])
        assert exchange.cancels.count('original-A')==1 and exchange.posts==[]
        rejected[0]=False;phase[0]='new_current_A_and_originally_free_EF'
        monkeypatch.setattr(pool,'NOW',paused_deadline)
        cold_prepare(engine._lp);engine.lp_auto_set_desired_running(True)
        assert scheduler.run_due()
        final=capture('new_current_distinct_A_only_cancel_and_two_free_BUYs')
        assert final['active_plan'] is None and final['last_round']['completed_at'] and final['last_round']['round_id']!=original['round_id']
        new_a,=[a for a in final['last_round']['actions'] if a['kind']=='cancel' and a['order_id']=='original-A']
        assert new_a['action_id']!=action['action_id'] and new_a['state']=='canceling'
        attempts=[a for a in store.lp_actions(original_session) if a.get('order_id')=='original-A']
        assert len(attempts)==2 and audit[0] in attempts
        newest,=[a for a in attempts if a['action_key']!=audit[0]['action_key']]
        assert newest['rotation_round_id']==final['last_round']['round_id'] and newest['action_id']!=audit[0]['action_id']
        assert exchange.cancels.count('original-A')==2 and Counter(exchange.cancels)==Counter(['original-A','original-A','original-D','original-I'])
        assert Counter(p['token_id'] for p in exchange.posts)==Counter(['E','F'])
        assert all(Decimal(p['price'])==Decimal('.39') and Decimal(p['quantity'])==20 and p['post_only'] is True for p in exchange.posts)
        assert not any(p['token_id']=='G' for p in exchange.posts) and not any(s.get('token_id')=='G' for s in store.lp_sessions())
        assert not any(r['active_plan'] for r in evidence['full_reads'])
        evidence['stage']='complete'
    finally:
        evidence.update(clock=pool.NOW,state=engine.lp_auto_state(),document=document(),sessions=store.lp_sessions(),
            audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            orders=exchange.orders,cancels=exchange.cancels,posts=exchange.posts)
        (tmp_path/'RF-refusal-mapping-diagnostics.json').write_text(json.dumps(evidence,default=str,indent=2)+'\n')


@pytest.mark.parametrize('result', [
    'canceled', 'LIVE', 'missing_order', 'incomplete_open_orders', 'uuid_owner_canceled', 'wrong_maker',
])
def test_order_confirmation_does_not_depend_on_balance_transport(runtime, monkeypatch, result):
    from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic, NOW
    from tests.test_lp_order_registration_contract import _open_order, WALLET
    first = _open_order('rank-a', 'BUY', price='.39', original='20')
    second = _open_order('rank-b', 'BUY', price='.39', original='20')
    store, adapter, account, _, engine = runtime(
        orders=(first, second), public_client=_CandidateSDKPublic(NOW))
    engine.lp_auto_configure(dict(budget_usd='39', target_buy_count=1, buy_price_level=2))
    engine.lp_auto_set_desired_running(True)
    finance_attempts = []
    balance = account.get_balance_allowance
    def healthy_balance(**kwargs):
        finance_attempts.append(dict(active=engine.lp_auto_state()['active_plan'] is not None, failed=False))
        return balance(**kwargs)
    monkeypatch.setattr(account, 'get_balance_allowance', healthy_balance)
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    with engine._lock_path.open('a+') as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert scheduler.run_due()
            state = engine.lp_auto_state()
            original = deepcopy(state['last_round'])
            assert state['active_plan'] is None and original['completed_at']
            assert [a['order_id'] for a in original['actions']] == ['rank-b']
            assert original['actions'][0]['state'] == 'not_sent'
            assert original['actions'][0]['reason'] == 'execution_lock'
            assert [r['order_id'] for r in original['targets']] == ['rank-a']
            assert all(Decimal(r['price']) == Decimal('.39') and Decimal(r['quantity']) == 20
                for r in original['targets'])
            assert state['plan_wait']['kind'] == 'round'
            assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(
                original['completed_at']) + timedelta(seconds=30)
            assert state['slots']['occupied'] == 2 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('15.60')
            assert account.posts == account.cancels == []
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    first_deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
    runtime.clock[0] = first_deadline - timedelta(seconds=1)
    before_reads = account.balance_reads
    assert not scheduler.run_due()
    assert account.balance_reads == before_reads and account.posts == account.cancels == []
    runtime.clock[0] = first_deadline
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    acknowledged = deepcopy(state['last_round'])
    assert state['active_plan'] is None and acknowledged['completed_at']
    assert acknowledged['round_id'] != original['round_id']
    action, = acknowledged['actions']
    assert action['kind'] == 'cancel' and action['order_id'] == 'rank-b' and action['state'] == 'canceling'
    assert action['action_id'] != original['actions'][0]['action_id']
    identity_fields = ('condition_id', 'token_id', 'order_id', 'session_id', 'retained_constraint')
    assert [{key: row.get(key) for key in identity_fields} for row in acknowledged['targets']] == [
        {key: row.get(key) for key in identity_fields} for row in original['targets']]
    assert [(Decimal(row['price']), Decimal(row['quantity'])) for row in acknowledged['targets']] == [
        (Decimal('.39'), Decimal('20'))]
    target, = acknowledged['targets']
    assert target['order_id'] == 'rank-a'
    estimate = target['minimum_order_estimate']
    assert estimate['state'] == 'known' and estimate['basis'] == 'resting_scoring_order'
    assert Decimal(estimate['price']) == Decimal('.39') and Decimal(estimate['quantity']) == 20
    assert Decimal(estimate['capital_usd']) == Decimal('7.80')
    observed_at = datetime.fromisoformat(estimate['checked_at'])
    assert first_deadline <= observed_at <= runtime.clock[0]
    assert observed_at > datetime.fromisoformat(original['targets'][0]['minimum_order_estimate']['checked_at'])
    assert account.cancels == [('rank-b',)] and account.posts == []
    assert state['slots']['occupied'] == 2 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('15.60')
    sid = action['session_id']
    audit, = store.lp_actions(sid)
    assert audit['state'] == 'accepted' and audit['role'] == 'reconciliation_cancel'
    assert audit['order_id'] == 'rank-b' and audit['rotation_round_id'] == acknowledged['round_id']
    assert audit['post_started'] is True
    original_audits = deepcopy(store.lp_actions(sid))
    assert not any(row['active'] for row in finance_attempts)
    financial_as_of = state['funds']['as_of']
    assert financial_as_of
    balance_reads = account.balance_reads
    def unavailable(**kwargs):
        account.balance_reads += 1
        finance_attempts.append(dict(active=engine.lp_auto_state()['active_plan'] is not None, failed=True))
        raise TimeoutError('SDK balance transport unavailable')
    monkeypatch.setattr(account, 'get_balance_allowance', unavailable)
    if result != 'LIVE':
        account.orders = (first,)
    reads = []
    def lookup(*, order_id):
        reads.append(order_id)
        assert order_id == 'rank-b'
        if result == 'missing_order':
            return None
        row = _open_order(order_id, 'BUY', price='.39', original='20', status='CANCELED')
        if result in ('uuid_owner_canceled', 'wrong_maker'):
            row = row.model_copy(update=dict(owner='550e8400-e29b-41d4-a716-446655440000',
                maker_address=WALLET if result == 'uuid_owner_canceled' else '0x' + 'b' * 40))
        return row
    monkeypatch.setattr(account, 'get_order', lookup, raising=False)
    if result == 'incomplete_open_orders':
        monkeypatch.setattr(account, 'list_open_orders', lambda **kwargs: None)
    result_observations = []
    result_reader = adapter.lp_order_result_snapshot
    def observed_result_reader(request):
        packet = result_reader(request)
        result_observations.append(dict(request=deepcopy(request), packet=deepcopy(packet)))
        return packet
    monkeypatch.setattr(adapter, 'lp_order_result_snapshot', observed_result_reader)
    next_deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert next_deadline == datetime.fromisoformat(acknowledged['completed_at']) + timedelta(seconds=30)
    runtime.clock[0] = next_deadline - timedelta(seconds=1)
    scheduler.request_check()
    assert not scheduler.run_due()
    assert account.balance_reads == balance_reads and reads == result_observations == []
    assert account.cancels == [('rank-b',)] and account.posts == []
    assert store.lp_actions(sid) == original_audits
    runtime.clock[0] = next_deadline
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert account.balance_reads > balance_reads and any(row['failed'] for row in finance_attempts)
    assert not any(row['active'] for row in finance_attempts)
    assert len(result_observations) == 1
    observation = result_observations[0]
    assert observation['request']['owned_order_ids'] == ['rank-b']
    packet = observation['packet']
    assert packet['wallet_address'] == WALLET
    if result == 'incomplete_open_orders':
        assert packet['authenticated'] is False
        assert packet['open_orders_complete'] is False and packet['pagination_complete'] is False
    else:
        assert packet['authenticated'] is True
    assert packet['read_started_at'] <= packet['read_ended_at']
    assert state['last_round']['round_id'] not in (original['round_id'], acknowledged['round_id'])
    assert state['last_round']['actions'] == [] and state['last_round']['targets'] == []
    assert state['last_round']['completed_at'] and state['active_plan'] is None
    assert state['last_round']['reason'] == 'account_order_sync_unknown'
    assert 'account_order_sync_unknown' in state['admission_block_reasons']
    assert state['plan_wait']['kind'] == 'round'
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(
        state['last_round']['completed_at']) + timedelta(seconds=30)
    assert state['funds']['as_of'] == financial_as_of
    assert account.posts == [] and account.cancels == [('rank-b',)]
    assert store.lp_actions(sid) == original_audits
    if result in ('canceled', 'uuid_owner_canceled'):
        assert reads == ['rank-b']
        assert all(packet[key] is True for key in ('open_orders_complete', 'pagination_complete',
            'trades_complete', 'positions_complete'))
        exact, = [row for row in packet['orders'] if row['order_id'] == 'rank-b']
        assert exact['status'] == 'CANCELED' and exact['side'] == 'BUY' and exact['token_id'] == second.token_id
        assert Decimal(exact['price']) == Decimal('.39') and Decimal(exact['original_size']) == 20
        assert exact['fill_quantity_known'] is True and Decimal(exact['size_matched']) == 0
        assert 'rank-b' not in packet['order_read_errors']
        assert state['slots']['occupied'] == 1
        assert {row['order_id'] for row in state['account_buys']} == {'rank-a'}
        assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('7.80')
        assert store.lp_session(sid).get('yield_rotation_cancel_owners') == {}
    elif result == 'LIVE':
        assert reads == []
        assert packet['open_orders_complete'] is True and packet['order_read_errors'] == {}
        assert state['slots']['occupied'] == 2
        assert {row['order_id'] for row in state['account_buys']} == {'rank-a', 'rank-b'}
        assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('15.60')
    else:
        assert reads == ['rank-b']
        if result == 'missing_order':
            assert packet['order_read_errors']['rank-b'] == 'order_lookup_unavailable'
        elif result == 'wrong_maker':
            assert packet['order_read_errors']['rank-b'] == 'order_response_invalid'
            assert packet['open_orders_complete'] is False and packet['pagination_complete'] is False
        else:
            assert packet['open_orders_complete'] is False and packet['pagination_complete'] is False
        assert state['slots']['occupied'] == 2
        assert {row['order_id'] for row in state['account_buys']} == {'rank-a', 'rank-b'}
        assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('15.60')
    assert store.lp_actions(sid) == original_audits
    assert account.posts == [] and account.cancels == [('rank-b',)]


@pytest.mark.parametrize('receipt', ['timeout', 'accepted_idless', 'sdk_opaque', 'ACK_LIVE', 'late_receipt'])
def test_unknown_submission_keeps_identity_until_result_recovery(tmp_path, monkeypatch, receipt):
    from collections import Counter
    from copy import deepcopy
    from threading import get_ident
    import json
    import uuid
    import sqlite3
    import hashlib
    from tests.test_lp_auto_plan_scheduler import restart_engine
    from polymarket.models.clob.order_response import (
        AcceptedOrder, RawOrderResponse, normalize_order_response,
    )
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    initial = ('A', 'B', 'C', 'D', 'I') if receipt == 'ACK_LIVE' else ()
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, initial)
    result_reader = install_order_result_reader(exchange, monkeypatch)
    post = exchange.lp_post_order
    entered, release = Event(), Event()
    query_entered, query_release = Event(), Event()
    account_entered, account_release = Event(), Event()
    states, full_reads, narrow_reads, refreshes, late_responses = [], [], [], [], []
    coverage_evidence = []
    original_owned = {o['order_id']: deepcopy(o) for o in exchange.orders}
    failed_at, narrow_failed_at = [], []
    fail_account, fail_narrow, block_account, block_narrow = [False], [False], [False], [False]
    account = exchange.lp_account_snapshot
    shared = exchange.lp_account_snapshot_shared
    def full_account(*args, **kwargs):
        observed = dict(route='account', started=pool.NOW, thread=get_ident(), failed=False)
        full_reads.append(observed)
        if fail_account[0] and receipt != 'ACK_LIVE':
            monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=3))
            failed_at.append(pool.NOW)
            observed.update(failed=True, ended=pool.NOW)
            raise TimeoutError('Required current full-account transport unavailable')
        if block_account[0]:
            account_entered.set()
            assert account_release.wait(5), 'Independent current-account SDK watchdog'
        try:
            packet = account(*args, **kwargs)
        except TimeoutError:
            observed.update(failed=True, ended=pool.NOW)
            raise
        observed.update(ended=pool.NOW, packet=deepcopy(packet))
        return packet
    def full_shared(*args, **kwargs):
        observed = dict(route='shared', started=pool.NOW, thread=get_ident(), failed=False)
        full_reads.append(observed)
        try:
            packet = shared(*args, **kwargs)
        except TimeoutError:
            observed.update(failed=True, ended=pool.NOW)
            raise
        observed.update(ended=pool.NOW, packet=deepcopy(packet))
        return packet
    def results(request):
        observed = dict(request=deepcopy(request), started=pool.NOW, thread=get_ident())
        narrow_reads.append(observed)
        if fail_narrow[0]:
            monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=3))
            narrow_failed_at.append(pool.NOW)
            observed.update(failed=True, ended=pool.NOW)
            raise TimeoutError('Necessary owned order-result read unavailable')
        if block_narrow[0]:
            query_entered.set()
            assert query_release.wait(5), 'Independent actual owned-result SDK watchdog'
        packet = result_reader(request)
        if receipt == 'ACK_LIVE':
            for field in ('orders', 'open_orders'):
                for row in packet[field]:
                    original_row = original_owned.get(row.get('order_id'))
                    if (original_row and row.get('status') in ('LIVE', 'CANCELED')
                            and row.get('fill_quantity_known') in (None, True)
                            and all(row.get(k) == original_row.get(k) for k in
                                ('condition_id', 'market_id', 'token_id', 'outcome', 'side'))
                            and row.get('side') == 'BUY'
                            and row.get('size_matched') is not None and Decimal(str(row['size_matched'])) == 0
                            and row.get('original_size') is not None and Decimal(str(row['original_size'])) == 20
                            and row.get('price') is not None and Decimal(str(row['price'])) == Decimal('.39')):
                        row['fill_quantity_known'] = True
        observed.update(failed=False, ended=pool.NOW, packet=deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_account_snapshot', full_account)
    monkeypatch.setattr(exchange, 'lp_account_snapshot_shared', full_shared)
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
    if receipt == 'ACK_LIVE':
        exchange.cancel_terminal = False
    else:
        def venue(signed):
            if signed['token_id'] != 'B':
                return post(signed)
            if receipt == 'late_receipt':
                entered.set()
                assert release.wait(5), 'Independent original POST watchdog'
                response = post(signed)
                late_responses.append(deepcopy(response))
                return AcceptedOrder(order_id=response['order_id'], status='live',
                    making_amount=Decimal('7.80'), taking_amount=Decimal('20'),
                    trade_ids=(), transactions_hashes=())
            exchange.posts.append(signed)
            if receipt == 'timeout':
                raise TimeoutError('Original POST result unknown')
            if receipt == 'accepted_idless':
                return dict(accepted=True, status='ACCEPTED')
            return normalize_order_response(RawOrderResponse(success=True, errorMsg='',
                orderID='', status='live', makingAmount='7.80', takingAmount='20',
                tradeIDs=(), transactionsHashes=()))
        monkeypatch.setattr(exchange, 'lp_post_order', venue)
    catalog_read = exchange.lp_reward_catalog
    def prepare_current_candidates(service):
        # Actual public preparation, from this function's external carriers.
        def catalog(*, condition_ids=None, **kwargs):
            result = catalog_read(condition_ids=tuple(exchange.rewards) if condition_ids is None else condition_ids, **kwargs)
            return {**result, 'state': 'known', 'complete': True}
        def history(token_ids, *, start_ts, end_ts, **kwargs):
            return dict(state='known', history={token: [dict(t=start_ts, p='.390'),
                dict(t=end_ts, p='.391')] for token in token_ids})
        def competition(**kwargs):
            return dict(state='known', complete=True, checked_at=pool.NOW,
                round_checked_at=pool.NOW, competitiveness={token: (Decimal('2.5'), pool.NOW)
                    for token in exchange.rewards})
        monkeypatch.setattr(exchange, 'lp_reward_catalog', catalog)
        monkeypatch.setattr(exchange, 'lp_market_metadata', exchange.lp_market_metadata_fresh, raising=False)
        monkeypatch.setattr(exchange, 'lp_price_history', history, raising=False)
        monkeypatch.setattr(exchange, 'lp_market_competitiveness', competition, raising=False)
        competition_state = service.refresh_competition_cache()
        assert competition_state['state'] == 'known' and competition_state['complete']
        prepared = service.refresh_price_history()
        assert prepared['preparation_outcome'] == 'success', prepared
        qualified = service.refresh_candidates(force=True)
        refreshes.append(deepcopy(dict(competition=competition_state, preparation=prepared, qualified=qualified)))
        for token in ('E', 'F', 'G'):
            row, = [row for row in qualified['candidates'] if row['condition_id'] == token]
            assert row['selected_direction']['token_id'] == token
            assert row['selected_direction']['eligible'] is True
    def native_document():
        with sqlite3.connect(store.path) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    def completed_state():
        state = engine.lp_auto_state()
        states.append(deepcopy(state))
        last = deepcopy(state['last_round'])
        assert last['completed_at'] and state['active_plan'] is None
        completed = datetime.fromisoformat(last['completed_at'])
        assert state['plan_wait']['kind'] == 'round'
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=30)
        return state, last, completed
    def audit_map():
        return {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    def silence(completed):
        counts = (len(exchange.posts), len(exchange.cancels), len(full_reads), len(narrow_reads))
        audits = audit_map()
        for elapsed in (1, 29):
            monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=elapsed))
            scheduler.request_check()
            assert not scheduler.run_due()
            engine.lp_auto_run_once()
            assert (len(exchange.posts), len(exchange.cancels), len(full_reads), len(narrow_reads)) == counts
            assert audit_map() == audits
    try:
        if receipt == 'late_receipt':
            with ThreadPoolExecutor(max_workers=1) as workers:
                running = workers.submit(scheduler.run_due)
                try:
                    assert entered.wait(5), 'Original B POST reaches the actual venue'
                    before = engine.lp_auto_state()
                    states.append(deepcopy(before))
                    original = deepcopy(before['active_plan'])
                    first, = [a for a in original['actions'] if a['token_id'] == 'B']
                    session, = [s for s in store.lp_sessions()
                        if s['idempotency_key'] == 'lp-auto:' + first.get('request_id', first['action_id'])]
                    audit, = [a for a in store.lp_actions(session['session_id']) if a['role'] == 'entry']
                    uuid.UUID(audit['action_id'])
                    assert session['post_started'] is True and audit['post_started'] is False
                    assert audit['state'] == 'pending' and not audit.get('order_id')
                    assert lp.entry_send_inflight(session['session_id'])
                    assert Counter(p['token_id'] for p in exchange.posts)['B'] == 0
                    assert all(n <= 1 for n in Counter(p['token_id'] for p in exchange.posts).values())
                    before_posts = deepcopy(exchange.posts)
                    scheduler.request_check()
                    assert not scheduler.run_due()
                    restored = restart_engine(engine, exchange)
                    assert restored.lp_auto_run_once()['round_reason'] == 'round_in_progress'
                    assert Counter(p['token_id'] for p in exchange.posts)['B'] == 0
                    assert all(n <= 1 for n in Counter(p['token_id'] for p in exchange.posts).values())
                    assert all(p in exchange.posts for p in before_posts)
                finally:
                    release.set()
                assert running.result(timeout=5)
            state, original_done, completed = completed_state()
            assert original_done['round_id'] == original['round_id']
            assert [a['action_id'] for a in original_done['actions']] == [a['action_id'] for a in original['actions']]
            assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
            assert len(late_responses) == 1
            finished, = [a for a in original_done['actions'] if a['token_id'] == 'B']
            assert finished['state'] == 'success' and finished['session_id'] == session['session_id']
            assert finished['order_id'] == late_responses[0]['order_id'] and finished['action_id'] == first['action_id']
            final_audit, = [a for a in store.lp_actions(session['session_id']) if a['role'] == 'entry']
            assert final_audit['state'] == 'accepted' and final_audit['order_id'] == late_responses[0]['order_id']
            assert final_audit['action_id'] == audit['action_id'] and final_audit['action_key'] == audit['action_key']
            assert final_audit['created_at'] == audit['created_at'] and final_audit['post_started'] is True
            assert not lp.entry_send_inflight(session['session_id'])
            assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
            assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20
                and p['post_only'] is True and p['side'] == 'BUY' for p in exchange.posts)
            silence(completed)
            return
        assert scheduler.run_due()
        state, original, completed = completed_state()
        assert [t['token_id'] for t in original['targets']] == ['B', 'C', 'E', 'F', 'G']
        assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in original['targets'])
        assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
        original_audits = audit_map()
        original_ids = [(a['action_id'], a.get('session_id'), a.get('order_id')) for a in original['actions']]
        if receipt == 'ACK_LIVE':
            assert exchange.posts == [] and Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'])
            assert set(live_orders(exchange)) == {'A', 'B', 'C', 'D', 'I'}
            dependent = [a for a in original['actions'] if a['kind'] == 'buy']
            assert {a['token_id'] for a in dependent} == {'E', 'F', 'G'}
            assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
            original_cancel_audits = [a for rows in original_audits.values() for a in rows if a['role'] == 'reconciliation_cancel']
            assert len(original_cancel_audits) == 3 and all(a['state'] == 'accepted' for a in original_cancel_audits)
        else:
            unknown, = [a for a in original['actions'] if a['token_id'] == 'B']
            assert unknown['state'] == 'unknown' and not unknown.get('order_id')
            assert all(a['state'] == 'success' for a in original['actions'] if a['token_id'] != 'B')
            assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
            assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
            assert sum(Decimal(str(o['price'])) * Decimal(str(o['original_size']))
                for o in exchange.orders if o['status'] == 'LIVE') == Decimal('31.20')
            unknown_intent, = [i for i in state['intents'] if i['token_id'] == 'B']
            assert unknown_intent['state'] == 'unknown' and Decimal(unknown_intent['reserved_usd']) == Decimal('7.80')
            b_audit, = [a for a in original_audits[unknown['session_id']] if a['role'] == 'entry']
            uuid.UUID(b_audit['action_id'])
            assert b_audit['state'] == ('accepted' if receipt == 'accepted_idless' else 'unknown')
            assert b_audit['submit_stage'] == ('receipt_received' if receipt == 'accepted_idless' else 'send_unknown')
            assert b_audit['post_started'] is True and not b_audit.get('order_id')
            assert not native_document()['intents'][unknown_intent['intent_id']].get('reservation_coverage')
            assert Decimal(b_audit['price']) == Decimal('.39') and Decimal(b_audit['quantity']) == 20
        silence(completed)
        exchange.account_failure = True
        fail_account[0] = True
        fail_narrow[0] = receipt == 'ACK_LIVE'
        monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=30))
        assert scheduler.run_due()
        state, failed_round, failed_completed = completed_state()
        assert failed_round['round_id'] != original['round_id']
        assert failed_round['reason'] == 'account_order_sync_unknown'
        failures = narrow_failed_at if receipt == 'ACK_LIVE' else failed_at
        assert len(failures) == 1 and failed_completed == failures[0]
        assert audit_map() == original_audits
        assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
        if receipt != 'ACK_LIVE':
            assert not native_document()['intents'][unknown_intent['intent_id']].get('reservation_coverage')
        assert len(exchange.posts) == (0 if receipt == 'ACK_LIVE' else 5)
        assert Counter(exchange.cancels) == (Counter(['original-A', 'original-D', 'original-I']) if receipt == 'ACK_LIVE' else Counter())
        engine = restart_engine(engine, exchange)
        lp = engine._lp
        scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
        cold = engine.lp_auto_state()
        states.append(deepcopy(cold))
        assert cold['slots']['occupied'] == 5 and Decimal(cold['funds']['buy_reserved_usd']) == 39
        if receipt != 'ACK_LIVE':
            assert not native_document()['intents'][unknown_intent['intent_id']].get('reservation_coverage')
        silence(failed_completed)
        fail_account[0] = fail_narrow[0] = False
        exchange.account_failure = False
        exchange.cancel_terminal = True
        monkeypatch.setattr(pool, 'NOW', failed_completed + timedelta(seconds=30))
        if receipt == 'ACK_LIVE':
            prepare_current_candidates(lp)
        else:
            refreshes.append(deepcopy(lp.refresh_candidates(force=True)))
        block_narrow[0] = receipt == 'ACK_LIVE'
        block_account[0] = receipt != 'ACK_LIVE'
        expected_event = query_entered if receipt == 'ACK_LIVE' else account_entered
        expected_release = query_release if receipt == 'ACK_LIVE' else account_release
        with ThreadPoolExecutor(max_workers=1) as workers:
            running = workers.submit(scheduler.run_due)
            try:
                assert expected_event.wait(5), 'Actual current recovery carrier reaches SDK callback'
                counts = len(exchange.posts), len(exchange.cancels)
                scheduler.request_check()
                assert not scheduler.run_due()
                assert (len(exchange.posts), len(exchange.cancels)) == counts
                assert audit_map() == original_audits
                held_state = engine.lp_auto_state()
                assert held_state['slots']['occupied'] == 5 and Decimal(held_state['funds']['buy_reserved_usd']) == 39
                if receipt != 'ACK_LIVE':
                    assert not native_document()['intents'][unknown_intent['intent_id']].get('reservation_coverage')
            finally:
                expected_release.set()
            assert running.result(timeout=5)
        block_account[0] = block_narrow[0] = False
        state, current, current_completed = completed_state()
        assert current['round_id'] not in (original['round_id'], failed_round['round_id'])
        assert {a['action_id'] for a in current['actions']}.isdisjoint(x[0] for x in original_ids)
        expected_slots, expected_reserved = (5, Decimal('39')) if receipt == 'ACK_LIVE' else (4, Decimal('31.20'))
        assert state['slots']['occupied'] == expected_slots and Decimal(state['funds']['buy_reserved_usd']) == expected_reserved
        for sid, rows in original_audits.items():
            assert all(a in store.lp_actions(sid) for a in rows)
        if receipt == 'ACK_LIVE':
            assert Counter(exchange.cancels) == Counter({'original-A': 2, 'original-D': 2, 'original-I': 2})
            assert exchange.posts == []
            new_audits = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])
                if a['role'] == 'reconciliation_cancel' and a not in original_audits.get(s['session_id'], ())]
            assert len(new_audits) == 3 and all(a['rotation_round_id'] == current['round_id'] for a in new_audits)
            assert {a['action_id'] for a in new_audits}.isdisjoint(a['action_id'] for a in original_cancel_audits)
            assert set(live_orders(exchange)) == {'B', 'C'}
            silence(current_completed)
            monkeypatch.setattr(pool, 'NOW', current_completed + timedelta(seconds=30))
            prepare_current_candidates(lp)
            assert scheduler.run_due()
            state, refill, _ = completed_state()
            assert refill['round_id'] != current['round_id']
            assert Counter(p['token_id'] for p in exchange.posts) == Counter(['E', 'F', 'G'])
            assert all(a['state'] == 'success' for a in refill['actions'])
            assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
            assert live_orders(exchange)['B'] == 'original-B' and live_orders(exchange)['C'] == 'original-C'
            assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
            for sid, rows in original_audits.items():
                assert all(a in store.lp_actions(sid) for a in rows)
        else:
            assert audit_map() == original_audits and len(exchange.posts) == 5 and exchange.cancels == []
            current_intent, = [i for i in state['intents'] if i['session_id'] == unknown['session_id']]
            assert current_intent['state'] == 'unknown' and not current_intent.get('order_id')
            assert Decimal(current_intent['reserved_usd']) == Decimal('7.80')
            assert current_intent.get('settled') is not True
            document = native_document()
            original_current = document['intents'][unknown_intent['intent_id']]
            marker = original_current['reservation_coverage']
            financial = document['account_financial_facts']
            assert marker['version'] == 1 and marker['state'] == 'covered' and marker['basis'] == 'ended_send'
            assert marker['intent_id'] == unknown_intent['intent_id'] and marker['session_id'] == unknown['session_id']
            assert marker['pool_account_id'] == document['account_id'] == financial['pool_account_id']
            assert marker['account_id'] == financial['account_id'] == 'test-wallet'
            assert hashlib.sha256(marker['account_id'].encode()).hexdigest() == marker['pool_account_id']
            assert marker['snapshot_id'] == financial['snapshot_id'] and marker['snapshot_id']
            assert Decimal(marker['original_reserved_usd']) == Decimal('7.80')
            assert financial['financial_status'] == 'known'
            request_finished = datetime.fromisoformat(b_audit['submit_finished_at'])
            assert request_finished == datetime.fromisoformat(marker['request_finished_at'])
            for key in ('read_started_at', 'read_ended_at', 'checked_at'):
                assert datetime.fromisoformat(marker[key]) == datetime.fromisoformat(financial[key])
            assert request_finished < datetime.fromisoformat(marker['read_started_at'])
            packets = [row['packet'] for row in full_reads if row['route'] == 'shared' and not row['failed']
                and datetime.fromisoformat(str(row['packet']['read_started_at'])) == datetime.fromisoformat(marker['read_started_at'])]
            assert len(packets) == 1
            packet = packets[0]
            assert packet['authenticated'] is True and packet['account_id'] == marker['account_id']
            assert packet['wallet_address'] == marker['account_id']
            assert all(packet[key] is True for key in ('balance_complete', 'open_orders_complete',
                'positions_complete', 'trades_complete', 'pagination_complete'))
            assert Decimal(str(packet['balance'])) == Decimal(financial['balance_usd'])
            assert Decimal(str(packet['allowance'])) == Decimal(financial['allowance_usd'])
            assert {o['order_id'] for o in packet['open_orders']} == set(live_orders(exchange).values())
            assert store.lp_actions(unknown['session_id']) == original_audits[unknown['session_id']]
            coverage_evidence.append(deepcopy(dict(marker=marker, financial=financial, packet=packet,
                original_intent=original_current, original_audit=b_audit)))
            assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
            assert all(set(row['request'].get('owned_order_ids', ())).issubset(
                {o['order_id'] for o in exchange.orders}) for row in narrow_reads)
            silence(current_completed)
        assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20
            and p['post_only'] is True and p['side'] == 'BUY' for p in exchange.posts)
    finally:
        release.set()
        query_release.set()
        account_release.set()
        (tmp_path / 'Q1-public-recovery-evidence.json').write_text(json.dumps(dict(receipt=receipt,
            states=states, full_reads=full_reads, narrow_reads=narrow_reads, refreshes=refreshes,
            late_responses=late_responses, failed_at=failed_at, narrow_failed_at=narrow_failed_at,
            coverage_evidence=coverage_evidence,
            late_entered=entered.is_set(), account_entered=account_entered.is_set(),
            narrow_entered=query_entered.is_set()), default=str, indent=2) + '\n')




@pytest.mark.parametrize('resource', [
    'execution_lock_before_cancel', 'execution_lock_after_sign', 'read_capacity', 'same_market',
])
def test_resource_wait_resumes_fixed_plan_without_balance_recheck(tmp_path, monkeypatch, resource):
    from collections import Counter
    from copy import deepcopy
    from pathlib import Path
    from threading import Barrier, Lock, current_thread, get_ident
    from time import monotonic
    import json
    import sqlite3
    from tests.test_lp_auto_plan_execution import install_order_result_reader
    from tests.test_lp_auto_target_convergence import publish_candidates, REWARDS

    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'))
    result_reader = install_order_result_reader(exchange, monkeypatch)
    result_packets, reads, signatures, approvals, preview_calls = [], [], [], [], []
    held, previews, preview_results = [], [], []
    network_jobs, network_results = {}, {}
    blockers = ('Y', 'Z') if resource == 'read_capacity' else ('E',) if resource == 'same_market' else ()
    entered = {token: Event() for token in blockers}
    completed = {token: Event() for token in blockers}
    release = Event()
    armed, approved_phase, preview_phase = [False], [False], [False]
    caller, deadline = get_ident(), [None]
    old_timeout = lp._market_read_timeout
    original_ids = live_orders(exchange)
    original_orders = deepcopy(exchange.orders)
    signature_lock = Lock()
    workers = ThreadPoolExecutor(max_workers=2)

    def remaining():
        return max(0, deadline[0] - monotonic())

    def hold():
        assert not held
        handle = engine._lock_path.open('a+')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        held.append(handle)

    def unlock():
        for handle in held:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        held.clear()

    def results(request):
        packet = result_reader(request)
        result_packets.append(deepcopy(packet))
        return packet

    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
    for name in ('lp_account_snapshot', 'lp_account_snapshot_shared', 'lp_reward_catalog', 'lp_order_books'):
        read = getattr(exchange, name)
        def observed(*args, _read=read, _name=name, **kwargs):
            preview = preview_phase[0] and current_thread().name == 'lp-market-read'
            reads.append(dict(method=_name, thread=get_ident(), thread_name=current_thread().name,
                phase='preview' if preview else 'automatic_approved' if approved_phase[0] else 'current_planning'))
            if approved_phase[0] and not preview and _name in ('lp_account_snapshot', 'lp_account_snapshot_shared'):
                raise TimeoutError('Financial transport unavailable for approved automatic dispatch')
            return _read(*args, **kwargs)
        monkeypatch.setattr(exchange, name, observed)
    metadata = exchange.lp_market_metadata_fresh
    def blocking_metadata(ids, **kwargs):
        preview = preview_phase[0] and current_thread().name == 'lp-market-read'
        reads.append(dict(method='lp_market_metadata_fresh', ids=list(ids), thread=get_ident(),
            thread_name=current_thread().name,
            phase='preview' if preview else 'automatic_approved' if approved_phase[0] else 'current_planning'))
        for token in blockers:
            if token in ids and preview:
                try:
                    entered[token].set()
                    assert release.wait(remaining()), 'Independent actual preview SDK watchdog'
                    return metadata(ids, **kwargs)
                finally:
                    completed[token].set()
        return metadata(ids, **kwargs)
    monkeypatch.setattr(exchange, 'lp_market_metadata_fresh', blocking_metadata)

    def preview(token):
        preview_calls.append(dict(token=token, caller=get_ident(), phase='public_preview_after_approval'))
        return engine.lp_candidate_preview(dict(market_id=token, condition_id=token, token_id=token, outcome='YES'))

    connect = sqlite3.connect
    class AuthorityConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            result = super().execute(sql, parameters)
            if sql.strip().upper() == 'COMMIT' and armed[0] and get_ident() == caller:
                row = super().execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
                document = json.loads(row[0]) if row else {}
                plan = document.get('active_plan')
                if plan:
                    armed[0] = False
                    assert not self.in_transaction
                    state = engine.lp_auto_state()
                    assert state['active_plan']['round_id'] == plan['round_id']
                    assert [t['token_id'] for t in plan['targets']] == ['B', 'C', 'E', 'F', 'G']
                    assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in plan['targets'])
                    assert {a['token_id'] for a in plan['actions'] if a['kind'] == 'buy'} == {'E', 'F', 'G'}
                    assert all(a['state'] == 'pending' for a in plan['actions'])
                    assert exchange.posts == []
                    approvals.append(deepcopy(plan))
                    approved_phase[0] = True
                    if blockers:
                        preview_phase[0] = True
                        if resource == 'read_capacity':
                            lp._market_read_timeout = .25
                        previews.extend(workers.submit(preview, token) for token in blockers)
                        for token in blockers:
                            assert entered[token].wait(remaining()), 'Actual public preview reaches SDK metadata'
                        if resource == 'read_capacity':
                            for pending in previews:
                                preview_results.append(pending.result(timeout=remaining()))
                            assert all(result['state'] == 'rejected' for result in preview_results)
                            assert not any(event.is_set() for event in completed.values())
                            lp._market_read_timeout = old_timeout
                        assert not any(event.is_set() for event in completed.values())
                        with lp._market_reads_lock:
                            network_jobs.update({token: lp._market_reads[token] for token in blockers})
                        assert all(not job.done() for job in network_jobs.values())
                    return result
            return result
    def observed_connect(database, *args, **kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs.setdefault('factory', AuthorityConnection)
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', observed_connect)

    sign = exchange.lp_create_limit_order
    barrier = Barrier(3, action=hold)
    def signer(**kwargs):
        signed = sign(**kwargs)
        with signature_lock:
            signatures.append(deepcopy(signed))
        if resource == 'execution_lock_after_sign' and approved_phase[0]:
            barrier.wait(timeout=remaining())
        return signed
    monkeypatch.setattr(exchange, 'lp_create_limit_order', signer)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)

    def closed():
        state = engine.lp_auto_state()
        last = deepcopy(state['last_round'])
        assert last['completed_at'] and state['active_plan'] is None
        completed_at = datetime.fromisoformat(last['completed_at'])
        assert state['plan_wait']['kind'] == 'round'
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed_at + timedelta(seconds=30)
        return state, last, completed_at

    def silent(completed_at):
        before = (len(reads), len(result_packets), deepcopy(exchange.posts), list(exchange.cancels))
        audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        for elapsed in (1, 29):
            monkeypatch.setattr(pool, 'NOW', completed_at + timedelta(seconds=elapsed))
            scheduler.request_check()
            assert not scheduler.run_due()
            engine.lp_auto_run_once()
            assert (len(reads), len(result_packets), exchange.posts, exchange.cancels) == before
            assert {sid: store.lp_actions(sid) for sid in audits} == audits

    def due(completed_at):
        approved_phase[0] = False
        monkeypatch.setattr(pool, 'NOW', completed_at + timedelta(seconds=30))
        publish_candidates(exchange, lp, store, REWARDS)
        return scheduler.run_due()

    try:
        if resource == 'execution_lock_before_cancel':
            hold()
        assert scheduler.run_due()
        state, original, completed_at = closed()
        assert [t['token_id'] for t in original['targets']] == ['B', 'C', 'E', 'F', 'G']
        assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in original['targets'])
        assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity'
            for a in original['actions'] if a['kind'] == 'buy')
        assert exchange.posts == []
        if resource == 'execution_lock_before_cancel':
            assert all(a['state'] == 'not_sent' and a['reason'] == 'execution_lock'
                for a in original['actions'] if a['kind'] == 'cancel')
            assert exchange.cancels == [] and exchange.orders == original_orders
            assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
            failed_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
            assert all(not a.get('rotation_round_id') for values in failed_audits.values() for a in values)
            silent(completed_at)
            unlock()
            assert due(completed_at)
            state, cancel_round, completed_at = closed()
            assert cancel_round['round_id'] != original['round_id']
            assert {a['action_id'] for a in cancel_round['actions']}.isdisjoint(a['action_id'] for a in original['actions'])
            assert {sid: store.lp_actions(sid)[:len(audits)] for sid, audits in failed_audits.items()} == failed_audits
        else:
            cancel_round = original
        assert Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'])
        assert live_orders(exchange) == {token: original_ids[token] for token in ('B', 'C')}
        assert sum(Decimal(str(o['price'])) * Decimal(str(o['original_size']))
            for o in exchange.orders if o['status'] == 'LIVE') == Decimal('15.60')
        old_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        cancel_audits = [a for values in old_audits.values() for a in values if a['role'] == 'reconciliation_cancel']
        assert len(cancel_audits) == 3 and all(a['state'] == 'accepted' for a in cancel_audits)
        assert {a['order_id'] for a in cancel_audits} == {'original-A', 'original-D', 'original-I'}
        silent(completed_at)
        deadline[0] = monotonic() + 5
        armed[0] = True
        assert due(completed_at)
        approved_phase[0] = False
        state, attempted, completed_at = closed()
        assert len(approvals) == 1 and attempted['round_id'] == approvals[0]['round_id']
        assert attempted['round_id'] != cancel_round['round_id']
        assert [a['action_id'] for a in attempted['actions']] == [a['action_id'] for a in approvals[0]['actions']]
        assert Counter(s['token_id'] for s in signatures) == Counter(['E', 'F', 'G'])
        assert all(Decimal(s['price']) == Decimal('.39') and Decimal(s['quantity']) == 20 and s['post_only'] is True for s in signatures)
        assert not any(r['phase'] == 'automatic_approved' for r in reads), reads
        assert {sid: store.lp_actions(sid) for sid in old_audits} == old_audits
        if resource == 'execution_lock_after_sign':
            assert held and exchange.posts == []
            assert all(a['state'] in ('rejected', 'not_sent') for a in attempted['actions'])
            entry_audits = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id']) if a['role'] == 'entry']
            assert len(entry_audits) == 3 and all(a['state'] == 'rejected' and a['post_started'] is False
                and not a.get('order_id') and Decimal(a['price']) == Decimal('.39')
                and Decimal(a['quantity']) == 20 for a in entry_audits)
            ended_audits = {a['session_id']: deepcopy(store.lp_actions(a['session_id'])) for a in entry_audits}
            silent(completed_at)
            unlock()
            assert due(completed_at)
            state, successful, completed_at = closed()
            assert successful['round_id'] != attempted['round_id']
            assert {a['action_id'] for a in successful['actions']}.isdisjoint(a['action_id'] for a in attempted['actions'])
            assert {sid: store.lp_actions(sid) for sid in ended_audits} == ended_audits
            assert Counter(s['token_id'] for s in signatures) == Counter(['E', 'E', 'F', 'F', 'G', 'G'])
        else:
            successful = attempted
            if blockers:
                assert len(preview_calls) == len(blockers)
                assert len([r for r in reads if r['phase'] == 'preview' and r['method'] == 'lp_account_snapshot']) == len(blockers)
                assert not any(event.is_set() for event in completed.values())
        assert all(a['state'] == 'success' for a in successful['actions'])
        assert Counter(p['token_id'] for p in exchange.posts) == Counter(['E', 'F', 'G'])
        assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20
            and p['post_only'] is True and p['side'] == 'BUY' for p in exchange.posts)
        assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
        assert all(live_orders(exchange)[token] == original_ids[token] for token in ('B', 'C'))
        assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
        assert Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'])
        silent(completed_at)
        release.set()
        for pending in previews:
            preview_results.append(pending.result(timeout=remaining()))
        for token in blockers:
            assert completed[token].wait(remaining())
        for token, job in network_jobs.items():
            try:
                network_results[token] = dict(state='completed', result=job.result(timeout=remaining()))
            except (ValueError, KeyError) as exc:
                network_results[token] = dict(state='completed_error', error=type(exc).__name__, reason=str(exc))
        preview_phase[0] = False
        if blockers:
            with lp._market_reads_lock:
                assert not any(not job.done() for job in lp._market_reads.values())
        assert due(completed_at)
        final_state, final_round, _ = closed()
        assert final_round['round_id'] != successful['round_id']
        assert Counter(p['token_id'] for p in exchange.posts) == Counter(['E', 'F', 'G'])
        assert Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'])
        assert final_state['slots']['occupied'] == 5 and Decimal(final_state['funds']['buy_reserved_usd']) == 39
        assert {sid: store.lp_actions(sid) for sid in old_audits} == old_audits
    finally:
        approved_phase[0] = False
        release.set()
        unlock()
        barrier.abort()
        for pending in previews:
            if not pending.done():
                pending.result(timeout=5)
        workers.shutdown(wait=True)
        lp._market_read_timeout = old_timeout
        (tmp_path / 'RS-public-resource-evidence.json').write_text(json.dumps(dict(resource=resource,
            approvals=approvals, reads=reads, signatures=signatures, original_orders=original_orders,
            result_packets=result_packets, preview_calls=preview_calls, preview_results=preview_results,
            network_results=network_results, network_done={k: v.done() for k, v in network_jobs.items()},
            preview_entered={k: v.is_set() for k, v in entered.items()},
            preview_completed={k: v.is_set() for k, v in completed.items()}), default=str, indent=2) + '\n')




@pytest.mark.parametrize('control,window', [
    (control, window) for control in ('stop', 'breaker', 'owner', 'account', 'config')
    for window in ('after_plan', 'after_sign')
] + [('order_identity', 'before_cancel'), ('protection', 'before_cancel'), ('stop', 'before_cancel')])
def test_execution_controls_still_fence_an_approved_plan(tmp_path, monkeypatch, control, window):
    from open_trader.prediction_runtime import _RuntimeOwnershipLock
    from tests.test_lp_auto_target_convergence import publish_candidates, REWARDS
    from threading import Barrier, Lock, Thread, get_ident
    from time import monotonic
    from pathlib import Path
    from collections import Counter
    import sqlite3
    import json
    import uuid
    initial = ('A', 'B', 'C', 'D', 'I') if window == 'before_cancel' else ()
    if control == 'protection':
        engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, tokens=initial)
        engine.lp_auto_run_once()
        original_a = live_orders(exchange)['A']
        initial_a = deepcopy(next(s for s in store.lp_sessions() if s['token_id'] == 'A'))
        exchange.posts.clear()
        monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']))
        publish_candidates(exchange, lp, store, REWARDS)
    else:
        engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, initial)
        original_a = 'original-A'
    original_orders = deepcopy(exchange.orders)
    initial_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    result_reader = install_order_result_reader(exchange, monkeypatch)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    owner = _RuntimeOwnershipLock(tmp_path / 'runtime.lock')
    owner.acquire()
    lp.owner_lock = owner
    captured, target_summaries, control_evidence, native_commits = [], [], [], []
    captured_stop_sessions, held_stop_boundaries = [], []
    captured_stop_audits = {}
    signatures, packets, financial_reads, monitor_packets = [], [], [], []
    control_started, signatures_ready, release, monitor_seen = Event(), Event(), Event(), Event()
    signature_lock = Lock()
    sign_barrier = Barrier(5, action=signatures_ready.set)
    controller_deadline = [None]
    armed, caller = [window != 'after_sign'], get_ident()
    def remaining():
        return max(0, controller_deadline[0] - monotonic())
    def capture_authority():
        snapshot = deepcopy(engine.lp_auto_state())
        plan = snapshot['active_plan']
        assert plan and plan['actions'] and not plan.get('completed_at')
        assert [t['token_id'] for t in plan['targets']] == ['B', 'C', 'E', 'F', 'G']
        assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in plan['targets'])
        if window == 'before_cancel':
            cancels = [a for a in plan['actions'] if a['kind'] == 'cancel']
            buys = [a for a in plan['actions'] if a['kind'] == 'buy']
            assert {a['order_id'] for a in cancels} == {live_orders(exchange)[k] for k in ('A', 'D', 'I')}
            assert all(a['state'] == 'pending' for a in cancels)
            assert {a['token_id'] for a in buys} == {'E', 'F', 'G'}
            assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in buys)
        else:
            assert len(plan['actions']) == 5 and all(a['kind'] == 'buy' and a['state'] == 'pending' for a in plan['actions'])
        assert exchange.posts == exchange.cancels == []
        audits = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])]
        if window == 'after_sign':
            entries = [a for a in audits if a['role'] == 'entry']
            assert len(entries) == 5 and all(a['state'] == 'pending' and a.get('post_started') is False for a in entries)
            assert {a['token_id'] for a in entries} == {'B', 'C', 'E', 'F', 'G'}
        else:
            assert {sid: store.lp_actions(sid) for sid in initial_audits} == initial_audits
            assert len(audits) == sum(len(a) for a in initial_audits.values())
        if control == 'stop' and window == 'before_cancel':
            captured_stop_sessions.extend(deepcopy(store.lp_sessions()))
            assert len(captured_stop_sessions) == 5
            assert {s['entry_order_id'] for s in captured_stop_sessions} == {o['order_id'] for o in original_orders}
            captured_stop_audits.update({s['session_id']: deepcopy(store.lp_actions(s['session_id']))
                for s in captured_stop_sessions})
            assert set(captured_stop_audits) == {s['session_id'] for s in captured_stop_sessions}
            assert all(a == [] for a in captured_stop_audits.values())
        captured.append(plan)
        target_summaries.append(snapshot['last_round']['targets'])
        return snapshot
    def change_control():
        assert not control_started.is_set()
        control_started.set()
        before = capture_authority()
        if control == 'stop':
            after = engine.lp_auto_set_desired_running(False)
            assert after['desired_running'] is False
        elif control == 'breaker':
            startup = engine.reconcile_startup()
            assert startup['state'] == 'locked' and startup['reason'] == 'account_unavailable'
            assert engine.lp_mutation_allowed() is False
            control_evidence.append(deepcopy(startup))
        elif control == 'owner':
            owner.release()
            assert owner.held is False
        elif control == 'account':
            exchange.config = SimpleNamespace(wallet_address='another-wallet')
        elif control == 'config':
            if window == 'after_sign':
                with pytest.raises(ValueError, match='^pause_and_finish_automatic_buys_before_configuring$'):
                    engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=5, buy_price_level=1))
                after = engine.lp_auto_state()
                for key in ('config_version', 'trading_config_version', 'buy_price_level', 'budget_usd'):
                    assert after[key] == before[key]
                assert after['active_plan']['targets'] == before['active_plan']['targets']
            else:
                engine.lp_auto_set_desired_running(False)
                engine.lp_auto_configure(dict(budget_usd='100', target_buy_count=5, buy_price_level=1))
                engine.lp_auto_set_desired_running(True)
        elif control == 'order_identity':
            next(o for o in exchange.orders if o['order_id'] == 'original-A')['token_id'] = 'unowned-token'
        else:
            direction = exchange.direction
            def depleted(token):
                result = direction(token)
                if token == 'A':
                    result['book']['bids'][1]['size'] = '3000'
                return result
            monkeypatch.setattr(exchange, 'direction', depleted)
            exchange.cancel_terminal = False
            snapshot = exchange.lp_snapshot
            def observed_monitor(request):
                packet = snapshot(request)
                if request['token_id'] == 'A':
                    monitor_packets.append(deepcopy(packet))
                    monitor_seen.set()
                return packet
            monkeypatch.setattr(exchange, 'lp_snapshot', observed_monitor)
            engine.lp_tick()
            assert monitor_seen.wait(remaining()), 'Actual external normal A book watchdog'
            while True:
                engine.lp_tick()
                session = next(s for s in store.lp_sessions() if s['token_id'] == 'A')
                protection = [a for a in store.lp_actions(session['session_id']) if a['role'] == 'entry-protection-cancel']
                if protection and any(b['state'] in ('triggered', 'canceling', 'cancel_unknown')
                        for b in session['queue_protection']['levels'].values()):
                    assert exchange.cancels.count(original_a) == 1
                    control_evidence.append(dict(session=deepcopy(session), audits=deepcopy(protection)))
                    break
                assert remaining() > 0, 'Public ordinary protective outcome watchdog'
                Event().wait(min(.01, remaining()))
        control_evidence.append(dict(control=control, window=window, at=pool.NOW,
            config=deepcopy(engine.lp_auto_state()['config_version'])))
    connect = sqlite3.connect
    class AuthorityConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            result = super().execute(sql, parameters)
            if sql.strip().upper() == 'COMMIT' and armed[0] and get_ident() == caller:
                row = super().execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
                document = json.loads(row[0]) if row else {}
                plan = document.get('active_plan')
                if plan:
                    armed[0] = False
                    assert not self.in_transaction
                    assert [t['token_id'] for t in plan['targets']] == ['B', 'C', 'E', 'F', 'G']
                    assert super().execute("SELECT COUNT(*) FROM lp_actions WHERE json_extract(payload,'$.role')='reconciliation_cancel'").fetchone()[0] == 0
                    assert exchange.posts == exchange.cancels == signatures == []
                    native_commits.append(dict(round_id=plan['round_id'], in_transaction=False, thread=caller))
                    change_control()
            return result
    def observed_connect(database, *args, **kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs.setdefault('factory', AuthorityConnection)
        return connect(database, *args, **kwargs)
    if window != 'after_sign':
        monkeypatch.setattr(sqlite3, 'connect', observed_connect)
    sign = exchange.lp_create_limit_order
    def signer(**kwargs):
        signed = sign(**kwargs)
        with signature_lock:
            signatures.append(deepcopy(signed))
        if window == 'after_sign':
            sign_barrier.wait(timeout=remaining())
            assert release.wait(remaining()), 'Original five real signatures control watchdog'
        return signed
    monkeypatch.setattr(exchange, 'lp_create_limit_order', signer)
    def results(request):
        packet = result_reader(request)
        packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
    for method in ('lp_account_snapshot', 'lp_account_snapshot_shared'):
        read = getattr(exchange, method)
        def observed(*args, _read=read, **kwargs):
            financial_reads.append(pool.NOW)
            return _read(*args, **kwargs)
        monkeypatch.setattr(exchange, method, observed)
    sender, results_returned, errors = None, [], []
    controller_deadline[0] = monotonic() + 5
    try:
        if window == 'after_sign':
            def run():
                try:
                    results_returned.append(scheduler.run_due())
                except BaseException as error:
                    errors.append(error)
            sender = Thread(target=run, name='C4-public-scheduler', daemon=True)
            sender.start()
            assert signatures_ready.wait(remaining()), 'All five real external signatures must arrive'
            assert Counter(s['token_id'] for s in signatures) == Counter(['B', 'C', 'E', 'F', 'G'])
            change_control()
            if control == 'stop':
                sender.join(remaining())
                assert not sender.is_alive() and not errors and results_returned == [True]
                stopped = deepcopy(engine.lp_auto_state())
                assert stopped['active_plan'] is None and stopped['last_round']['completed_at']
                assert stopped['last_round']['round_id'] == captured[0]['round_id'] and stopped['desired_running'] is False
                assert all(a['state'] == 'not_sent' and a['reason'] == 'cycle_ended' for a in stopped['last_round']['actions'])
                assert exchange.posts == exchange.cancels == []
                pending_receipts = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id']) if a['role'] == 'entry']
                assert len(pending_receipts) == 5 and all(a['state'] == 'pending' and a['post_started'] is False for a in pending_receipts)
                assert all(lp.entry_send_inflight(a['session_id']) for a in pending_receipts)
                held_stop_boundaries.append(dict(state=stopped, pending_receipts=deepcopy(pending_receipts), live_sender_count=5))
            release.set()
            sender.join(remaining())
            assert not sender.is_alive() and not errors and results_returned == [True]
            while True:
                audits = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id']) if a['role'] == 'entry']
                if len(audits) == 5 and all(a['state'] != 'pending' for a in audits):
                    break
                assert remaining() > 0, 'Actual original entry result completion watchdog'
                Event().wait(min(.01, remaining()))
        else:
            assert scheduler.run_due()
            assert len(native_commits) == 1 and native_commits[0]['round_id'] == captured[0]['round_id']
        assert len(captured) == 1
        state = engine.lp_auto_state()
        first = deepcopy(state['last_round'])
        assert first['round_id'] == captured[0]['round_id']
        assert [a['action_id'] for a in first['actions']] == [a['action_id'] for a in captured[0]['actions']]
        assert first['targets'] == target_summaries[0]
        assert first['completed_at'] and state['active_plan'] is None
        completed = datetime.fromisoformat(first['completed_at'])
        assert state['plan_wait']['kind'] == 'round' and datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=30)
        if control == 'config' and window == 'after_sign':
            assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
            assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20 and p['post_only'] is True and p['side'] == 'BUY' for p in exchange.posts)
            assert exchange.cancels == [] and state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
        elif window != 'before_cancel':
            assert exchange.posts == exchange.cancels == []
            if control == 'config':
                assert all(a['state'] == 'rejected' and a['reason'] == 'config_version_changed' for a in first['actions'])
                assert signatures == [] and store.lp_sessions() == []
            elif control == 'owner' or window == 'after_sign' and control in ('breaker', 'account'):
                assert all(a['state'] == 'rejected' and a['reason'] == 'prepare_failed' and a['request_state'] == 'entry_rejected'
                    for a in first['actions']), first
                expected_error = 'ValueError' if control == 'account' else '_MutationBlocked'
                normalized = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id']) if a['role'] == 'entry']
                assert len(normalized) == 5
                assert all(a['state'] == 'rejected' and a['reason'] == 'prepare_failed' and a['submit_stage'] == 'prepare_failed'
                    and a['post_started'] is False and not a.get('order_id') and a.get('submit_post_started_at') is None
                    and a['error'] == expected_error and a['submit_error_chain'] == [expected_error] for a in normalized)
                assert len(signatures) == (5 if window == 'after_sign' else 0)
                for audit in normalized:
                    uuid.UUID(audit['action_id'])
                    assert Decimal(audit['price']) == Decimal('.39') and Decimal(audit['quantity']) == 20
                    action, = [a for a in first['actions'] if a['token_id'] == audit['token_id']]
                    assert action['session_id'] == audit['session_id'] and not store.lp_session(audit['session_id']).get('entry_order_id')
            else:
                expected_reason = 'cycle_ended' if window == 'after_sign' else {
                    'stop': 'manually_paused', 'breaker': 'mutation_blocked', 'account': 'account_identity_mismatch'}[control]
                assert all(a['state'] == 'not_sent' and a['reason'] == expected_reason for a in first['actions']), first
                if window == 'after_plan':
                    assert signatures == [] and store.lp_sessions() == []
            if window == 'after_sign':
                audits = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id']) if a['role'] == 'entry']
                assert len(audits) == 5 and all(a['post_started'] is False and a['state'] == 'rejected' for a in audits)
                for audit in audits:
                    uuid.UUID(audit['action_id'])
                    assert Decimal(audit['price']) == Decimal('.39') and Decimal(audit['quantity']) == 20
                    action, = [a for a in first['actions'] if a['token_id'] == audit['token_id']]
                    assert action['session_id'] == audit['session_id']
        elif control == 'stop':
            assert state['desired_running'] is False and exchange.posts == exchange.cancels == []
            assert exchange.orders == original_orders
            assert live_orders(exchange) == {o['token_id']: o['order_id'] for o in original_orders}
            assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
            assert all(a['state'] == 'not_sent' and a['reason'] == 'manually_paused'
                for a in first['actions'] if a['kind'] == 'cancel')
            assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity'
                for a in first['actions'] if a['kind'] == 'buy')
            assert {s['session_id']: store.lp_actions(s['session_id']) for s in captured_stop_sessions} == captured_stop_audits
            assert {sid: store.lp_actions(sid) for sid in initial_audits} == initial_audits
            for original in captured_stop_sessions:
                current = store.lp_session(original['session_id'])
                for field in ('entry_order_id', 'order_history', 'queue_protection', 'yield_rotation_cancel_owners',
                        'yield_rotation_protection_before', 'entry_cancel_requested', 'augment_cancel_requested',
                        'stop_requested', 'stop_loss_latched'):
                    assert current.get(field) == original.get(field)
        elif control == 'order_identity':
            assert exchange.posts == [] and 'original-A' not in exchange.cancels
            assert next(o for o in exchange.orders if o['order_id'] == 'original-A')['status'] == 'LIVE'
            assert all(a['state'] == 'not_sent' and a['reason'] == 'owned_order_identity_mismatch'
                for a in first['actions'] if a['kind'] == 'cancel'), first
            assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
        else:
            assert monitor_seen.is_set() and monitor_packets
            assert exchange.cancels.count(original_a) == 1 and exchange.posts == []
            session = next(s for s in store.lp_sessions() if s['token_id'] == 'A')
            protected, = [e for e in control_evidence if 'session' in e]
            protected_session = protected['session']
            captured_bucket = protected_session['queue_protection']['levels']['0.39']
            assert captured_bucket['state'] == 'canceling' and captured_bucket['cancel_reason'] == 'queue_ahead_ratio'
            assert original_a in captured_bucket['cancel_targets']
            assert session['queue_protection']['levels']['0.39'] == captured_bucket
            for flag in ('entry_cancel_requested', 'augment_cancel_requested', 'stop_requested', 'stop_loss_latched'):
                assert session.get(flag) == protected_session.get(flag)
            for audit in protected['audits']:
                uuid.UUID(audit['action_id'])
                assert not audit.get('rotation_round_id')
                assert next(a for a in store.lp_actions(session['session_id']) if a['action_key'] == audit['action_key']) == audit
            assert not any(a.get('rotation_round_id') == first['round_id'] and a.get('order_id') == original_a
                for a in store.lp_actions(session['session_id']))
            assert session.get('stop_requested') == initial_a.get('stop_requested')
            assert next(o for o in exchange.orders if o['order_id'] == original_a)['status'] == 'LIVE'
        audits_before = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        before = (len(financial_reads), len(packets), deepcopy(exchange.posts), list(exchange.cancels))
        for elapsed in (1, 29):
            monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=elapsed))
            scheduler.request_check()
            assert not scheduler.run_due()
            engine.lp_auto_run_once()
            assert (len(financial_reads), len(packets), exchange.posts, exchange.cancels) == before
            assert {sid: store.lp_actions(sid) for sid in audits_before} == audits_before
        if control == 'config' and window == 'after_plan':
            monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=30))
            publish_candidates(exchange, lp, store, REWARDS)
            assert scheduler.run_due()
            current = engine.lp_auto_state()
            assert current['last_round']['round_id'] != first['round_id'] and current['last_round']['completed_at']
            assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
            assert all(Decimal(p['price']) == Decimal('.40') and Decimal(p['quantity']) == 20 and p['post_only'] is True and p['side'] == 'BUY' for p in exchange.posts)
            assert {a['action_id'] for a in current['last_round']['actions']}.isdisjoint({a['action_id'] for a in first['actions']})
            assert exchange.cancels == [] and current['slots']['occupied'] == 5 and Decimal(current['funds']['buy_reserved_usd']) == 40
    finally:
        (tmp_path / 'C4-public-control-evidence.json').write_text(json.dumps(dict(control=control, window=window,
            captured=captured, control_evidence=control_evidence, native_commits=native_commits,
            signatures=signatures, monitor_packets=monitor_packets, original_orders=original_orders,
            control_started=control_started.is_set(), signatures_ready=signatures_ready.is_set(),
            monitor_seen=monitor_seen.is_set(), captured_stop_sessions=captured_stop_sessions,
            held_stop_boundaries=held_stop_boundaries, captured_stop_audits=captured_stop_audits), default=str, indent=2) + '\n')
        release.set()
        sign_barrier.abort()
        if sender is not None:
            sender.join(timeout=5)
        owner.release()


@pytest.mark.parametrize('route', ['manual', 'augment', 'forged_manual', 'forged_augment', 'automatic_augment'])
@pytest.mark.parametrize('money', ['insufficient', 'unknown'])
def test_manual_and_augment_entries_keep_full_financial_validation(tmp_path, monkeypatch, route, money):
    from tests.test_polymarket_lp import _Exchange, _queue_book_snapshot, _request, _augment_preview_snapshot
    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    if route in ('augment', 'forged_augment'):
        store, exchange = pool.PredictionArbitrageStore(tmp_path / 'manual.sqlite'), _Exchange()
        exchange.snapshot_value = _queue_book_snapshot(pool.NOW, Decimal('120'))
        lp = pool.PolymarketLPService(store, exchange, clock=lambda: pool.NOW)
        entry = {**_request(pool.NOW), 'quantity': Decimal('120')}
        store.lp_save_price_history(entry['condition_id'], entry['token_id'], [], dict(state='known',
            amplitude=Decimal('.005'), checked_at=pool.NOW, valid_until=pool.NOW + timedelta(days=1)))
        preview = lp.preview(entry)
        started = lp.start(preview['preview_id'], 'manual-existing')
        assert started['state'] == 'entry_open'
        exchange.snapshot_value = _augment_preview_snapshot(pool.NOW, level=Decimal('380'), own_original=Decimal('120'))
        exchange.snapshot_value['account']['balance'] = '0' if money == 'insufficient' else None
        engine = PredictionExecutionService(store=store, monitor=SimpleNamespace(), trading=exchange,
            notifier=SimpleNamespace(), lock_path=tmp_path / 'execution.lock', lp=lp)
        engine._breaker_open = False  # Existing fixture's simulated clean startup.
        request = dict(session_id=started['session_id'], quantity='20', price='.29', idempotency_key='independent-augment')
        submit = engine.lp_submit_augment
    else:
        engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch)
        if route == 'automatic_augment':
            engine.lp_auto_run_once()
            started = next(s for s in store.lp_sessions() if s['token_id'] == 'B')
            request = dict(session_id=started['session_id'], quantity='20', price='.38', idempotency_key='automatic-augment')
            submit = engine.lp_submit_augment
        else:
            request = dict(market_id='X', condition_id='X', token_id='X', outcome='YES', price='.39', quantity='20',
                review_at=pool.NOW + timedelta(minutes=10), idempotency_key='independent-entry')
            submit = engine.lp_submit_entry
        account = exchange.lp_account_snapshot
        monkeypatch.setattr(exchange, 'lp_account_snapshot', lambda: {
            **account(), 'balance': '0' if money == 'insufficient' else None})
    if route.startswith('forged_'):
        # Client data cannot construct the server-owned authorization object.
        request.update(plan_authorization={'approved': True, 'round_id': 'forged', 'account_id': 'test-wallet',
            'price': '.39', 'quantity': '20'}, approved_plan=True, skip_account_validation=True,
            origin='automatic', active_plan={'version': 2, 'actions': [{'state': 'pending', 'kind': 'buy'}]})
    previous_posts = deepcopy(exchange.posts)
    result = submit(request)
    assert result['state'] == 'rejected'
    assert result['reason'] == ('automatic_session_augmentation_disabled' if route == 'automatic_augment'
        else 'balance_insufficient' if money == 'insufficient' else 'balance_unknown')
    assert exchange.posts == previous_posts
    assert not any(a.get('post_started') for s in store.lp_sessions()
        if s.get('idempotency_key') == request['idempotency_key'] for a in store.lp_actions(s['session_id']))


@pytest.mark.parametrize('interval', [30, 90], ids=['default30', 'custom90'])
def test_next_round_uses_new_facts_after_the_full_interval(tmp_path, monkeypatch, interval):
    from collections import Counter
    from tests.test_lp_auto_target_convergence import publish_candidates, REWARDS
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, budget='39')
    install_order_result_reader(exchange, monkeypatch)
    if interval != 30:
        engine.lp_auto_configure(dict(round_interval_seconds=interval))
    account, post = exchange.lp_account_snapshot, exchange.lp_post_order
    new_facts, captured = [False], []
    reads = []
    def financial():
        result = account()
        reads.append(dict(at=pool.NOW, changed=new_facts[0], active=engine.lp_auto_state()['active_plan'] is not None))
        if new_facts[0]:
            result.update(balance='60', allowance='60')
        return result
    def venue(signed):
        if signed['token_id'] == 'B' and not captured:
            captured.append(deepcopy(engine.lp_auto_state()['active_plan']))
            exchange.posts.append(signed)
            new_facts[0] = True
            exchange.rewards['H'] = Decimal('999')
            exchange.positions = [dict(token_id='A', condition_id='A', size='8', average_price='.39')]
            return dict(accepted=False, status='REJECTED', reason='venue_denied')
        return post(signed)
    monkeypatch.setattr(exchange, 'lp_account_snapshot', financial)
    monkeypatch.setattr(exchange, 'lp_post_order', venue)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    original = deepcopy(state['last_round'])
    assert original['completed_at'] and state['active_plan'] is None
    assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
    original_live = live_orders(exchange)
    assert set(original_live) == {'C', 'E', 'F', 'G'}
    assert [r['condition_id'] for r in original['targets']] == ['B', 'C', 'E', 'F', 'G']
    assert [a['action_id'] for a in original['actions']] == [a['action_id'] for a in captured[0]['actions']]
    original_b, = [a for a in original['actions'] if a.get('token_id') == 'B']
    assert original_b['state'] == 'rejected' and original_b['reason'] == 'venue_denied'
    assert all(a['state'] == 'success' for a in original['actions'] if a.get('token_id') != 'B')
    original_audits = {a['session_id']: deepcopy(store.lp_actions(a['session_id'])) for a in original['actions']}
    assert not any(row['changed'] or row['active'] for row in reads), 'Original fixed authorization must not reread changed finance'
    completed = datetime.fromisoformat(original['completed_at'])
    assert state['plan_wait']['kind'] == 'round'
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=interval)
    before = len(reads)
    for elapsed in (1, interval - 1):
        monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=elapsed))
        scheduler.request_check()
        assert not scheduler.run_due()
        assert len(reads) == before and len(exchange.posts) == 5
        assert engine.lp_auto_state()['last_round']['round_id'] == captured[0]['round_id']
    monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=interval))
    publish_candidates(exchange, lp, store, REWARDS)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    second = deepcopy(state['last_round'])
    assert len(reads) > before and any(row['changed'] for row in reads[before:])
    assert not any(row['active'] for row in reads)
    assert second['round_id'] != original['round_id'] and second['completed_at'] and state['active_plan'] is None
    assert [r['condition_id'] for r in second['targets']] == ['H', 'B', 'C', 'E']
    assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.12')
    # Four original accepted BUYs reserve31.20; inventory3.12 leaves4.68.
    # Neither H nor B may pre-spend F/G's original cancellation principal.
    assert Counter(exchange.cancels) == Counter([original_live['F'], original_live['G']])
    assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
    assert live_orders(exchange) == {key:original_live[key] for key in ('C', 'E')}
    dependent = [a for a in second['actions'] if a['kind'] == 'buy']
    assert {a['token_id'] for a in dependent} == {'H', 'B'}
    assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
    assert {a['action_id'] for a in second['actions']}.isdisjoint({a['action_id'] for a in original['actions']})
    for sid, audits in original_audits.items():
        current = store.lp_actions(sid)
        for audit in audits:
            assert next(a for a in current if a['action_key'] == audit['action_key']) == audit
    second_audits = {a['session_id']: deepcopy(store.lp_actions(a['session_id']))
        for a in second['actions'] if a['kind'] == 'cancel'}
    second_completed = datetime.fromisoformat(second['completed_at'])
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == second_completed + timedelta(seconds=interval)
    before = len(reads)
    monkeypatch.setattr(pool, 'NOW', second_completed + timedelta(seconds=interval - 1))
    assert not scheduler.run_due()
    assert len(reads) == before and len(exchange.posts) == 5
    monkeypatch.setattr(pool, 'NOW', second_completed + timedelta(seconds=interval))
    publish_candidates(exchange, lp, store, REWARDS)
    assert scheduler.run_due()
    final = engine.lp_auto_state()
    assert final['active_plan'] is None and final['last_round']['completed_at']
    assert final['last_round']['round_id'] not in (original['round_id'], second['round_id'])
    assert [r['condition_id'] for r in final['last_round']['targets']] == ['H', 'B', 'C', 'E']
    final_buys = [a for a in final['last_round']['actions'] if a['kind'] == 'buy']
    assert {a['token_id'] for a in final_buys} == {'H', 'B'} and all(a['state'] == 'success' for a in final_buys)
    assert {a['action_id'] for a in final_buys}.isdisjoint({a['action_id'] for a in dependent + original['actions']})
    new_b, = [a for a in final_buys if a['token_id'] == 'B']
    assert new_b['session_id'] != original_b['session_id']
    assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G', 'H', 'B'])
    final_live = live_orders(exchange)
    assert set(final_live) == {'H', 'B', 'C', 'E'}
    assert final_live['C'] == original_live['C'] and final_live['E'] == original_live['E']
    assert len(set(final_live.values())) == 4 and final['slots']['occupied'] == 4
    assert Decimal(final['funds']['buy_reserved_usd']) == Decimal('31.20')
    assert Decimal(final['funds']['inventory_cost_usd']) == Decimal('3.12')
    assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20 and p.get('side', 'BUY') == 'BUY'
        for p in exchange.posts)
    assert Counter(exchange.cancels) == Counter([original_live['F'], original_live['G']])
    for sid, audits in {**original_audits, **second_audits}.items():
        assert store.lp_actions(sid) == audits


@pytest.mark.parametrize('scenario', [
    'approved_unsent', 'cancel_pending', 'unknown_post', 'accepted_idless', 'completed_wait',
    'legacy_approved_unsent', 'legacy_cancel_pending', 'legacy_unknown_post',
    'legacy_accepted_idless', 'legacy_completed_wait', 'accepted_before_progress', 'receipt_await_apply', 'sign_interrupted',
    'rejected_before_progress', 'receipt_await_apply_terminal', 'receipt_await_apply_terminal_fill',
    'resource_rejected_before_progress', 'resource_rejected_clock_gap',
])
def test_restart_and_legacy_plan_upgrade_preserve_progress(tmp_path, monkeypatch, scenario):
    import base64
    import hashlib
    import json
    from pathlib import Path
    import sqlite3
    import zlib
    from tests.test_lp_auto_plan_scheduler import restart_engine
    from tests.test_lp_auto_target_convergence import PlanExchange
    legacy = scenario.startswith('legacy_')
    case = scenario.removeprefix('legacy_')
    rr1 = case in ('approved_unsent', 'cancel_pending', 'unknown_post', 'accepted_idless', 'completed_wait')
    if rr1:
        monkeypatch.setattr(pool, 'NOW', pool.NOW)  # Restore this shared clock before every local override.
    else:
        monkeypatch.setattr(pool, 'NOW', pool.NOW)  # Isolate RR2's deadline and native clock overrides.
    resource_rejection = case in ('resource_rejected_before_progress', 'resource_rejected_clock_gap')
    initial = ('A', 'B', 'C', 'D', 'I') if case == 'cancel_pending' else ()
    held = None
    trigger = False
    clock_gap, trace_errors, rejection_identity = [], [], {}
    if legacy:
        manifest = json.loads((Path(__file__).parent / 'fixtures/lp_auto_legacy_plans.json').read_text())
        sample = manifest['samples'][case]
        assert manifest['baseline_sha'] == sample['baseline_sha'] == 'ad59c636970acd81c06f76c5a6f1aefef7df770a'
        image = zlib.decompress(base64.b64decode(sample['database_zlib_base64']))
        assert hashlib.sha256(image).hexdigest() == sample['database_sha256']
        store = pool.PredictionArbitrageStore(tmp_path / 'state.sqlite')
        store.path.write_bytes(image)
        store = pool.PredictionArbitrageStore(tmp_path / 'state.sqlite')
        monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(sample['captured_at']))
        exchange = PlanExchange()
        for key, value in sample['exchange'].items():
            setattr(exchange, key, deepcopy(value))
        lp = pool.PolymarketLPService(store, exchange, clock=lambda: pool.NOW)
        exchange.lp = lp
        engine = pool.PredictionExecutionService(store=store, monitor=SimpleNamespace(), trading=exchange,
            notifier=SimpleNamespace(), lock_path=tmp_path / 'execution.lock', lp=lp)
        engine._breaker_open = False  # Isolated simulated startup, as existing fixture.
    else:
        engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, initial)
    install_order_result_reader(exchange, monkeypatch)
    if not rr1:
        from collections import Counter
        from threading import get_ident
        from time import monotonic
        import uuid
        entered, release = Event(), Event()
        evidence = dict(scenario=scenario, states=[], full_reads=[], narrow_reads=[],
            sibling_publications=[], signatures=[], responses=[], clock_commits=[], observer_errors=[], preparations=[])
        phase = ['original_execution']
        identity = {}
        original_sign, original_post = exchange.lp_create_limit_order, exchange.lp_post_order
        original_catalog = exchange.lp_reward_catalog
        result_reader = exchange.lp_order_result_snapshot
        holder = None
        trigger_live = False
        def document():
            with sqlite3.connect(store.path) as connection:
                return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
        def audits():
            return {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        def capture(label):
            state = deepcopy(engine.lp_auto_state())
            evidence['states'].append(dict(label=label, state=state, document=document(),
                sessions=deepcopy(store.lp_sessions()), audits=audits(), posts=deepcopy(exchange.posts),
                orders=deepcopy(exchange.orders), cancels=list(exchange.cancels)))
            return state
        for name in ('lp_account_snapshot', 'lp_account_snapshot_shared'):
            read = getattr(exchange, name)
            def account(*args, _read=read, _name=name, **kwargs):
                observed = dict(phase=phase[0], method=_name, attempted_at=pool.NOW)
                evidence['full_reads'].append(observed)
                try:
                    packet = _read(*args, **kwargs)
                except BaseException as exc:
                    observed.update(failed=True, error=type(exc).__name__)
                    raise
                observed.update(failed=False, packet=deepcopy(packet))
                return packet
            monkeypatch.setattr(exchange, name, account)
        def results(request):
            packet = result_reader(request)
            for field in ('orders', 'open_orders'):
                for row in packet[field]:
                    if (identity.get('order_id') and row.get('order_id') == identity['order_id']
                            and row.get('token_id') == row.get('condition_id') == 'B'
                            and row.get('side') == 'BUY' and row.get('status') in ('LIVE', 'CANCELED')
                            and Decimal(str(row.get('price'))) == Decimal('.39')
                            and Decimal(str(row.get('original_size'))) == 20
                            and Decimal(str(row.get('size_matched'))) == (8 if case == 'receipt_await_apply_terminal_fill' else 0)
                            and row.get('fill_quantity_known') in (None, True)):
                        row['fill_quantity_known'] = True
            evidence['narrow_reads'].append(dict(phase=phase[0], request=deepcopy(request), packet=deepcopy(packet)))
            return packet
        monkeypatch.setattr(exchange, 'lp_order_result_snapshot', results)
        def sign(**kwargs):
            signed = original_sign(**kwargs)
            evidence['signatures'].append(dict(signed=deepcopy(signed), phase=phase[0], thread=get_ident()))
            if kwargs['token_id'] == 'B':
                identity['thread'] = get_ident()
                entered.set()
                assert release.wait(5), 'Original B external signature watchdog'
                if case == 'sign_interrupted':
                    raise SystemExit('original sign invocation ended before POST')
            return signed
        def post(signed):
            if signed['token_id'] != 'B':
                return original_post(signed)
            identity['thread'] = get_ident()
            if case == 'rejected_before_progress':
                exchange.posts.append(signed)
                response = dict(accepted=False, status='REJECTED', reason='balance_insufficient')
            else:
                response = original_post(signed)
                identity['order_id'] = response['order_id']
            evidence['responses'].append(deepcopy(response))
            entered.set()
            assert release.wait(5), 'Original B external POST response watchdog'
            return response
        if case == 'sign_interrupted' or resource_rejection:
            monkeypatch.setattr(exchange, 'lp_create_limit_order', sign)
        else:
            monkeypatch.setattr(exchange, 'lp_post_order', post)
        if case == 'resource_rejected_clock_gap':
            native_connect = sqlite3.connect
            class RejectedCommitConnection(sqlite3.Connection):
                def execute(self, statement, parameters=(), /):
                    result = super().execute(statement, parameters)
                    try:
                        compact = statement.strip().upper()
                        if (compact.startswith('INSERT INTO LP_ACTIONS') and len(parameters) == 7
                                and identity.get('session_id') == parameters[1]
                                and identity.get('action_key') == parameters[2] and parameters[3] == 'rejected'
                                and identity.get('thread') == get_ident()):
                            payload = json.loads(parameters[4])
                            if (payload.get('role') == 'entry' and payload.get('side') == 'BUY'
                                    and payload.get('token_id') == 'B'
                                    and payload.get('submit_stage') == 'pre_send_rejected'
                                    and payload.get('reason') == 'execution_lock' and payload.get('post_started') is False):
                                self.reject_commit = dict(statement=statement, parameters=deepcopy(parameters),
                                    thread=get_ident(), session_id=parameters[1], action_key=parameters[2],
                                    database=str(store.path), observed_after_native_upsert=True,
                                    in_transaction_after_upsert=self.in_transaction)
                        elif compact == 'COMMIT' and getattr(self, 'reject_commit', None):
                            observed = self.reject_commit
                            self.reject_commit = None
                            before = pool.NOW
                            monkeypatch.setattr(pool, 'NOW', before + timedelta(microseconds=1))
                            evidence['clock_commits'].append(dict(**observed, commit=statement,
                                before=before, after=pool.NOW, native_commit_succeeded=True,
                                in_transaction_after_commit=self.in_transaction, returned_cursor_id=id(result)))
                        elif compact == 'ROLLBACK':
                            self.reject_commit = None
                    except BaseException as exc:
                        evidence['observer_errors'].append(dict(error=type(exc).__name__, message=str(exc)))
                    return result
            def observed_connect(database, *args, **kwargs):
                if Path(database).resolve() == store.path.resolve():
                    kwargs['factory'] = RejectedCommitConnection
                return native_connect(database, *args, **kwargs)
            monkeypatch.setattr(sqlite3, 'connect', observed_connect)
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        def install_fault():
            nonlocal trigger_live
            accepted = case == 'accepted_before_progress'
            state = 'accepted' if accepted else 'rejected'
            native_state = 'entry_open' if accepted else 'entry_rejected'
            outcome = ("AND json_extract(a.payload,'$.order_id')=" + quote(identity['order_id']) +
                " AND json_extract(s.payload,'$.entry_order_id')=" + quote(identity['order_id'])) if accepted else (
                "AND json_extract(a.payload,'$.reason')=" + quote('execution_lock' if resource_rejection else 'balance_insufficient') +
                " AND json_extract(s.payload,'$.reason')=" + quote('execution_lock' if resource_rejection else 'balance_insufficient'))
            stage = 'receipt_received' if accepted else ('pre_send_rejected' if resource_rejection else 'exchange_rejected')
            error = ('resource rejection persisted before plan progress' if resource_rejection else
                ('accepted receipt persisted before plan progress' if accepted else 'rejected receipt persisted before plan progress'))
            statement = f'''CREATE TRIGGER fail_plan_progress BEFORE UPDATE ON lp_auto_pool
                WHEN EXISTS (SELECT 1 FROM lp_actions a JOIN lp_sessions s USING(session_id)
                    JOIN json_each(OLD.payload,'$.intents') i
                    WHERE s.session_id={quote(identity['session_id'])}
                    AND s.idempotency_key={quote('lp-auto:' + identity['request_id'])}
                    AND a.action_key={quote(identity['action_key'])}
                    AND json_extract(i.value,'$.intent_id')={quote(identity['request_id'])}
                    AND json_extract(i.value,'$.session_id')=s.session_id
                    AND json_extract(i.value,'$.token_id')='B'
                    AND json_extract(s.payload,'$.account_id')='test-wallet'
                    AND json_extract(s.payload,'$.token_id')='B'
                    AND a.state={quote(state)} AND s.state={quote(native_state)}
                    AND json_extract(a.payload,'$.role')='entry' AND json_extract(a.payload,'$.side')='BUY'
                    AND json_extract(a.payload,'$.token_id')='B'
                    AND CAST(json_extract(a.payload,'$.price') AS REAL)=0.39
                    AND CAST(json_extract(a.payload,'$.quantity') AS REAL)=20
                    AND json_extract(a.payload,'$.submit_stage')={quote(stage)}
                    AND json_extract(s.payload,'$.submit_stage')={quote(stage)}
                    AND json_extract(a.payload,'$.post_started')={0 if resource_rejection else 1}
                    AND json_extract(s.payload,'$.post_started')={0 if resource_rejection else 1}
                    {outcome}) BEGIN SELECT RAISE(ABORT,{quote(error)}); END'''
            with sqlite3.connect(store.path) as connection:
                connection.execute(statement)
            trigger_live = True
            evidence['native_fault'] = dict(statement=statement, error=error, identity=deepcopy(identity))
            return error
        def release_holder():
            nonlocal holder
            if holder is not None:
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                holder.close()
                holder = None
        def prepare(service):
            phase[0] = 'explicit_cold_preparation'
            def catalog(*, condition_ids=None, **kwargs):
                result = original_catalog(condition_ids=tuple(exchange.rewards) if condition_ids is None else condition_ids, **kwargs)
                return {**result, 'state': 'known', 'complete': True}
            def history(token_ids, *, start_ts, end_ts, **kwargs):
                return dict(state='known', history={token: [dict(t=start_ts, p='.390'),
                    dict(t=end_ts, p='.391')] for token in token_ids})
            def competition(**kwargs):
                return dict(state='known', complete=True, checked_at=pool.NOW, round_checked_at=pool.NOW,
                    competitiveness={token: (Decimal('2.5'), pool.NOW) for token in exchange.rewards})
            monkeypatch.setattr(exchange, 'lp_reward_catalog', catalog)
            monkeypatch.setattr(exchange, 'lp_market_metadata', exchange.lp_market_metadata_fresh, raising=False)
            monkeypatch.setattr(exchange, 'lp_price_history', history, raising=False)
            monkeypatch.setattr(exchange, 'lp_market_competitiveness', competition, raising=False)
            competition_state = service.refresh_competition_cache()
            preparation = service.refresh_price_history()
            qualification = service.refresh_candidates(force=True)
            evidence['preparations'].append(deepcopy(dict(competition=competition_state,
                preparation=preparation, qualification=qualification)))
            assert competition_state['state'] == 'known' and competition_state['complete']
            assert preparation['preparation_outcome'] == 'success', preparation
            b, = [r for r in qualification['candidates'] if r['condition_id'] == 'B']
            assert b['selected_direction']['token_id'] == 'B' and b['selected_direction']['eligible'] is True
        def prove_retirement(current, ended_audit):
            observed = next(s for s in evidence['states'] if s['label'] == 'first_current_recovery')
            financial = observed['document']['account_financial_facts']
            marker = current['reservation_coverage']
            retired = current['account_coverage_retired']
            assert current['state'] == 'complete' and current['submit_status'] == 'rejected'
            assert current['post_started'] is False and not current.get('entry_order_id')
            assert marker['version'] == 1 and marker['state'] == 'covered' and marker['basis'] == 'ended_send'
            assert marker['intent_id'] == identity['request_id'] and marker['session_id'] == identity['session_id']
            assert marker['account_id'] == financial['account_id'] == 'test-wallet'
            assert marker['pool_account_id'] == observed['document']['account_id'] == financial['pool_account_id']
            assert marker['pool_account_id'] == hashlib.sha256(b'test-wallet').hexdigest()
            assert marker['snapshot_id'] == financial['snapshot_id'] == retired['snapshot_id'] and marker['snapshot_id']
            assert retired['reason'] == 'account_observation_no_exposure'
            assert datetime.fromisoformat(retired['checked_at']) == datetime.fromisoformat(financial['checked_at'])
            assert Decimal(marker['original_reserved_usd']) == 0
            assert datetime.fromisoformat(marker['request_finished_at']) == datetime.fromisoformat(ended_audit['submit_finished_at'])
            assert datetime.fromisoformat(marker['request_finished_at']) < datetime.fromisoformat(marker['read_started_at'])
            assert financial['financial_status'] == 'known' and financial['positions'] == []
            packets = [r['packet'] for r in evidence['full_reads'] if r['phase'] == 'current_recovery'
                and r['method'] == 'lp_account_snapshot_shared' and not r['failed']
                and datetime.fromisoformat(str(r['packet']['read_started_at'])) == datetime.fromisoformat(marker['read_started_at'])]
            packet, = packets
            assert packet['authenticated'] is True and packet['wallet_address'] == packet['account_id'] == 'test-wallet'
            assert all(packet[k] is True for k in ('balance_complete', 'open_orders_complete',
                'positions_complete', 'trades_complete', 'pagination_complete'))
            assert packet['positions'] == packet['raw_trades'] == []
            assert type(packet['trade_generation']) is int and packet['trade_generation'] == financial['trade_generation']
            for key in ('read_started_at', 'read_ended_at', 'checked_at'):
                assert datetime.fromisoformat(str(packet[key])) == datetime.fromisoformat(financial[key])
                assert datetime.fromisoformat(marker[key]) == datetime.fromisoformat(financial[key])
            assert Decimal(str(packet['balance'])) == Decimal(financial['balance_usd'])
            assert Decimal(str(packet['allowance'])) == Decimal(financial['allowance_usd'])
            original_ids = {a['order_id'] for a in evidence['sibling_publications'][0]['public']['last_round']['actions']
                if a['kind'] == 'buy' and a['token_id'] in {'C', 'E', 'F', 'G'}}
            assert {r['order_id'] for r in packet['open_orders']} == original_ids
            assert {r['token_id'] for r in packet['open_orders']} == {'C', 'E', 'F', 'G'}
            assert len(packet['open_orders']) == len(financial['buys']) == 4
            for row in packet['open_orders']:
                assert row['side'] == 'BUY' and row['status'] == 'LIVE' and row['token_id'] == row['condition_id']
                assert Decimal(str(row['price'])) == Decimal('.39')
                assert Decimal(str(row['original_size'])) == 20 and Decimal(str(row['size_matched'])) == 0
                buy, = [b for b in financial['buys'] if b['order_id'] == row['order_id']]
                assert buy['token_id'] == row['token_id'] and buy['side'] == 'BUY'
                assert Decimal(buy['reserved_usd']) == Decimal('7.80')
            assert sum(Decimal(buy['reserved_usd']) for buy in financial['buys']) == Decimal('31.20')
            evidence.setdefault('retirement_proofs', []).append(deepcopy(dict(session_id=current['session_id'],
                marker=marker, retired=retired, financial=financial, packet=packet)))
        def silence(label):
            nonlocal engine
            wait = engine.lp_auto_state().get('plan_wait')
            if not wait:
                return
            deadline = datetime.fromisoformat(wait['deadline'])
            assert datetime.fromisoformat(wait['started_at']) + timedelta(seconds=30) == deadline
            monkeypatch.setattr(pool, 'NOW', deadline - timedelta(seconds=1))
            counts = (len(evidence['full_reads']), len(evidence['narrow_reads']))
            prior_posts, prior_cancels, prior_audits = deepcopy(exchange.posts), list(exchange.cancels), audits()
            scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
            scheduler.request_check()
            assert not scheduler.run_due()
            with ThreadPoolExecutor(2) as workers:
                list(workers.map(lambda _: engine.lp_auto_run_once(), range(2)))
            engine = restart_engine(engine, exchange)
            engine.lp_auto_run_once()
            assert (len(evidence['full_reads']), len(evidence['narrow_reads'])) == counts
            assert exchange.posts == prior_posts and exchange.cancels == prior_cancels and audits() == prior_audits
            capture(label)
        try:
            with ThreadPoolExecutor(max_workers=1) as callers:
                running = callers.submit(engine.lp_auto_run_once)
                try:
                    assert entered.wait(5), 'Original B reaches genuine external boundary'
                    state = capture('original_B_external_boundary')
                    original = deepcopy(state['active_plan'])
                    assert {t['token_id'] for t in original['targets']} == {'B', 'C', 'E', 'F', 'G'}
                    assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in original['targets'])
                    b, = [a for a in original['actions'] if a['kind'] == 'buy' and a['token_id'] == 'B']
                    request_id = b.get('request_id', b['action_id'])
                    intent, = [i for i in state['intents'] if i['intent_id'] == request_id]
                    session = store.lp_session(intent['session_id'])
                    audit, = [a for a in store.lp_actions(session['session_id']) if a['role'] == 'entry']
                    identity.update(round_id=original['round_id'], action_id=b['action_id'], request_id=request_id,
                        session_id=session['session_id'], action_key=audit['action_key'])
                    assert session['idempotency_key'] == 'lp-auto:' + request_id
                    assert audit['action_key'] == session['session_id'] + ':entry-submit'
                    uuid.UUID(audit['action_id'])
                    assert audit['state'] == 'pending' and audit['post_started'] is False and not audit.get('order_id')
                    assert lp.entry_send_inflight(session['session_id'])
                    timeout = monotonic() + 5
                    while True:
                        public = engine.lp_auto_state()
                        siblings = [a for a in public['last_round']['actions'] if a['kind'] == 'buy' and a['token_id'] != 'B']
                        assert {a['token_id'] for a in siblings} == {'C', 'E', 'F', 'G'}
                        assert all(a['state'] in ('pending', 'success') for a in siblings), siblings
                        if all(a['state'] == 'success' for a in siblings):
                            for action in siblings:
                                sibling = store.lp_session(action['session_id'])
                                receipt, = [a for a in store.lp_actions(action['session_id']) if a['role'] == 'entry']
                                assert sibling['state'] == 'entry_open' and sibling['entry_order_id'] == action['order_id']
                                assert sibling['idempotency_key'] == 'lp-auto:' + action.get('request_id', action['action_id'])
                                assert receipt['state'] == 'accepted' and receipt['post_started'] is True
                                assert receipt['order_id'] == action['order_id'] == live_orders(exchange)[action['token_id']]
                                assert not lp.entry_send_inflight(action['session_id'])
                            evidence['sibling_publications'].append(deepcopy(dict(public=public, audits=audits())))
                            break
                        assert monotonic() < timeout, 'Public C/E/F/G completion watchdog'
                        Event().wait(.01)
                    if resource_rejection or case.startswith('receipt_await_apply'):
                        holder = engine._lock_path.open('a+')
                        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        evidence['holder'] = dict(acquired_after_public_siblings=True, path=str(engine._lock_path))
                    fault_error = install_fault() if resource_rejection or case in ('accepted_before_progress', 'rejected_before_progress') else None
                finally:
                    release.set()
                if fault_error:
                    with pytest.raises(sqlite3.IntegrityError, match=fault_error) as failed:
                        running.result(timeout=5)
                    assert failed.value.sqlite_errorname == 'SQLITE_CONSTRAINT_TRIGGER'
                    evidence['native_exception'] = dict(type=type(failed.value).__name__, message=str(failed.value),
                        sqlite_errorname=failed.value.sqlite_errorname, sqlite_errorcode=failed.value.sqlite_errorcode)
                elif case == 'sign_interrupted':
                    with pytest.raises(SystemExit, match='original sign invocation ended before POST'):
                        running.result(timeout=5)
                else:
                    running.result(timeout=5)
            saved = capture('original_completed_or_fault_checkpoint')
            original_audits = audits()
            original_session = store.lp_session(identity['session_id'])
            original_audit, = original_audits[identity['session_id']]
            original_action, = [a for a in saved['last_round']['actions'] if a['kind'] == 'buy' and a['token_id'] == 'B']
            assert not lp.entry_send_inflight(identity['session_id'])
            assert Counter(p['token_id'] for p in exchange.posts) == Counter(
                ['C', 'E', 'F', 'G'] if resource_rejection or case == 'sign_interrupted' else ['B', 'C', 'E', 'F', 'G'])
            assert all(p['side'] == 'BUY' and p['post_only'] is True and Decimal(p['price']) == Decimal('.39')
                and Decimal(p['quantity']) == 20 for p in exchange.posts)
            assert exchange.cancels == []
            if case == 'sign_interrupted':
                assert original_action['state'] == 'pending' and original_audit['state'] == 'pending'
                assert original_session['submit_stage'] == original_audit['submit_stage'] == 'preparing'
                assert original_session['post_started'] is original_audit['post_started'] is False
                assert not original_session.get('entry_order_id') and not original_audit.get('order_id')
            elif resource_rejection or case == 'rejected_before_progress':
                assert original_action['state'] == 'pending' and original_audit['state'] == 'rejected'
                assert original_session['state'] == 'entry_rejected'
                expected_reason = 'execution_lock' if resource_rejection else 'balance_insufficient'
                assert original_session['reason'] == original_audit['reason'] == expected_reason
                assert original_session['post_started'] is original_audit['post_started'] is (False if resource_rejection else True)
                assert original_session['submit_stage'] == original_audit['submit_stage'] == (
                    'pre_send_rejected' if resource_rejection else 'exchange_rejected')
                assert not original_session.get('entry_order_id') and not original_audit.get('order_id')
                assert Decimal(original_audit['price']) == Decimal('.39') and Decimal(original_audit['quantity']) == 20
            else:
                assert original_audit['state'] == 'accepted' and original_audit['post_started'] is True
                assert original_audit['order_id'] == identity['order_id'] == live_orders(exchange)['B']
                if case == 'accepted_before_progress':
                    assert original_action['state'] == 'pending'
                    assert original_session['entry_order_id'] == identity['order_id'] and original_session['state'] == 'entry_open'
                else:
                    assert original_action['state'] == 'unknown' and not original_session.get('entry_order_id')
                    assert saved['active_plan'] is None and saved['last_round']['completed_at']
                    assert saved['plan_wait']['kind'] == 'round'
                    assert datetime.fromisoformat(saved['plan_wait']['deadline']) == datetime.fromisoformat(
                        saved['last_round']['completed_at']) + timedelta(seconds=30)
            if case == 'resource_rejected_clock_gap':
                assert evidence['observer_errors'] == [] and len(evidence['clock_commits']) == 1
                gap = evidence['clock_commits'][0]
                assert gap['session_id'] == identity['session_id'] and gap['action_key'] == identity['action_key']
                assert gap['thread'] == identity['thread'] and gap['in_transaction_after_upsert'] is True
                assert gap['native_commit_succeeded'] is True and gap['in_transaction_after_commit'] is False
                assert gap['after'] == gap['before'] + timedelta(microseconds=1)
                requested, finished = (datetime.fromisoformat(original_audit[k]) for k in ('submit_requested_at', 'submit_finished_at'))
                assert requested <= finished < gap['after'] <= pool.NOW
                assert original_audit['submit_finished_at'] == original_session['submit_finished_at']
            if trigger_live:
                with sqlite3.connect(store.path) as connection:
                    connection.execute('DROP TRIGGER fail_plan_progress')
                trigger_live = False
            monkeypatch.setattr(exchange, 'lp_create_limit_order', original_sign)
            monkeypatch.setattr(exchange, 'lp_post_order', original_post)
            if case not in ('sign_interrupted',) and not resource_rejection:
                engine.lp_auto_set_desired_running(False)
            if case in ('receipt_await_apply_terminal', 'receipt_await_apply_terminal_fill'):
                row, = [o for o in exchange.orders if o['order_id'] == identity['order_id']]
                row.update(status='CANCELED', size_matched='8' if case.endswith('_fill') else '0')
                if case.endswith('_fill'):
                    exchange.positions = [dict(token_id='B', condition_id='B', size='8', average_price='.39')]
                    exchange.trades = [dict(trade_id='actual-B-fill', order_id=identity['order_id'], condition_id='B', token_id='B', side='BUY',
                        size='8', price='.39', fee='0', status='CONFIRMED', timestamp=pool.NOW, trader_side='MAKER',
                        taker_order_id='external-taker', maker_orders=[dict(order_id=identity['order_id'], token_id='B', side='BUY',
                            price='.39', matched_amount='8', fee='0', maker_address='test-wallet', owner='credential-owner-uuid')])]
            inherited = deepcopy(engine.lp_auto_state())
            engine = restart_engine(engine, exchange)
            cold = capture('cold_cached_startup')
            assert cold['active_plan'] == inherited['active_plan'] and cold['plan_wait'] == inherited['plan_wait']
            assert cold['last_round']['actions'] == inherited['last_round']['actions']
            assert cold['last_round']['targets'] == inherited['last_round']['targets']
            assert audits() == original_audits
            silence('inherited_deadline_silence')
            current_wait = engine.lp_auto_state().get('plan_wait')
            if current_wait:
                monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(current_wait['deadline']))
            if case == 'sign_interrupted':
                prepare(engine._lp)
            elif not resource_rejection:
                release_holder()
                exchange.account_failure = True
            phase[0] = 'current_recovery'
            old_financial = deepcopy(document().get('account_financial_facts'))
            engine.lp_auto_run_once()
            recovered = capture('first_current_recovery')
            assert recovered['active_plan'] is None and recovered['plan_wait']['kind'] == 'round'
            assert recovered['last_round']['round_id'] != original['round_id']
            if resource_rejection:
                assert Counter(p['token_id'] for p in exchange.posts) == Counter(['C', 'E', 'F', 'G'])
                assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'} and exchange.cancels == []
                assert store.lp_actions(identity['session_id']) == [original_audit]
                retired_session = store.lp_session(identity['session_id'])
                expected_session = {**original_session, 'state': 'complete',
                    'reservation_coverage': retired_session['reservation_coverage'],
                    'account_coverage_retired': retired_session['account_coverage_retired']}
                assert retired_session == expected_session
                prove_retirement(retired_session, original_audit)
                for action in recovered['last_round']['actions']:
                    if action['kind'] != 'buy':
                        continue
                    assert action.get('request_id', action['action_id']) != identity['request_id']
                    assert action['state'] == 'not_sent'
                    if action.get('session_id'):
                        held_session = store.lp_session(action['session_id'])
                        held_audit, = [a for a in store.lp_actions(action['session_id']) if a['role'] == 'entry']
                        assert held_session['post_started'] is held_audit['post_started'] is False
                        assert not held_session.get('entry_order_id') and not held_audit.get('order_id')
                capture('held_current_no_venue_send')
                release_holder()
                silence('held_current_interval_silence')
                monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']))
                prepare(engine._lp)
                phase[0] = 'new_current_planning'
                previously_registered = {s['session_id'] for s in store.lp_sessions()}
                engine.lp_auto_run_once()
                recovered = capture('healthy_new_B_after_resource_release')
                new_b, = [a for a in recovered['last_round']['actions'] if a['kind'] == 'buy' and a['token_id'] == 'B']
                assert new_b['state'] == 'success' and new_b['session_id'] not in previously_registered
                assert new_b.get('request_id', new_b['action_id']) != identity['request_id']
            elif case == 'sign_interrupted':
                previous, = store.lp_actions(identity['session_id'])
                assert previous['state'] == 'rejected' and previous['submit_stage'] == 'pre_send_rejected'
                assert previous['reason'] == 'preparation_interrupted' and previous['post_started'] is False
                assert previous['recovery_observed_at']
                for key in ('action_id', 'action_key', 'session_id', 'created_at', 'submit_requested_at', 'token_id', 'side'):
                    assert previous[key] == original_audit[key]
                original_audits[identity['session_id']] = [deepcopy(previous)]
                old_session = store.lp_session(identity['session_id'])
                assert old_session['state'] == 'complete' and old_session['idempotency_key'] == original_session['idempotency_key']
                assert old_session['submit_status'] == 'rejected' and old_session['reason'] == 'preparation_interrupted'
                assert old_session['submit_stage'] == 'pre_send_rejected' and old_session['post_started'] is False
                prove_retirement(old_session, previous)
                new_b, = [a for a in recovered['last_round']['actions'] if a['kind'] == 'buy' and a['token_id'] == 'B']
                assert new_b['state'] == 'success' and new_b['session_id'] != identity['session_id']
                assert new_b.get('request_id', new_b['action_id']) != identity['request_id']
            else:
                assert any(read['phase'] == 'current_recovery' and read['failed'] for read in evidence['full_reads'])
                assert document().get('account_financial_facts') == old_financial
                assert exchange.cancels == [] and len(exchange.posts) == 5
                actual = store.lp_session(identity['session_id'])
                if case == 'rejected_before_progress':
                    assert actual == original_session and actual['state'] == 'entry_rejected'
                    assert not actual.get('entry_order_id') and set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
                else:
                    assert actual['entry_order_id'] == identity['order_id'] and actual['submit_status'] == 'accepted'
                    if case.startswith('receipt_await_apply'):
                        packets = [read['packet'] for read in evidence['narrow_reads'] if read['phase'] == 'current_recovery']
                        assert packets
                        row, = [row for row in packets[-1]['orders'] if row['order_id'] == identity['order_id']]
                        assert row['fill_quantity_known'] is True and row['token_id'] == 'B' and row['side'] == 'BUY'
                        assert Decimal(str(row['price'])) == Decimal('.39') and Decimal(str(row['original_size'])) == 20
                    if case in ('receipt_await_apply_terminal', 'receipt_await_apply_terminal_fill'):
                        assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
                        assert actual['order_history'][identity['order_id']]['status'] == 'CANCELED'
                        assert recovered['slots']['occupied'] == 4
                        assert Decimal(recovered['funds']['buy_reserved_usd']) == Decimal('31.20')
                        if case.endswith('_fill'):
                            assert Decimal(actual['buy_filled_quantity']) == Decimal(actual['residual_quantity']) == 8
                            assert Decimal(actual['buy_cost']) == Decimal('3.12') and Decimal(actual['buy_fees']) == 0
                            assert exchange.positions == [dict(token_id='B', condition_id='B', size='8', average_price='.39')]
                            assert exchange.trades[0]['fee'] == '0'
                    else:
                        assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
                        assert recovered['slots']['occupied'] == 5 and Decimal(recovered['funds']['buy_reserved_usd']) == 39
                for key in ('stop_requested', 'entry_cancel_requested', 'stop_loss_latched', 'passive_cancel_requested', 'queue_protection_cancel_requested'):
                    assert actual.get(key) == original_session.get(key)
            if resource_rejection or case == 'sign_interrupted':
                assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
                assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
                assert recovered['slots']['occupied'] == 5 and Decimal(recovered['funds']['buy_reserved_usd']) == 39
            for sid, rows in original_audits.items():
                assert store.lp_actions(sid) == rows
            assert all(p['side'] == 'BUY' and p['post_only'] is True and Decimal(p['price']) == Decimal('.39')
                and Decimal(p['quantity']) == 20 for p in exchange.posts)
            silence('final_concurrent_and_cold_interval_silence')
            capture('completed')
        finally:
            release.set()
            release_holder()
            if trigger_live:
                with sqlite3.connect(store.path) as connection:
                    connection.execute('DROP TRIGGER IF EXISTS fail_plan_progress')
            evidence.update(identity=deepcopy(identity), final_state=deepcopy(engine.lp_auto_state()),
                final_audits=audits(), final_sessions=deepcopy(store.lp_sessions()),
                final_posts=deepcopy(exchange.posts), final_orders=deepcopy(exchange.orders), final_cancels=list(exchange.cancels))
            (tmp_path / 'RR2-public-restart-evidence.json').write_text(json.dumps(evidence, default=str, indent=2) + '\n')
        return
    if rr1:
        from collections import Counter
        import uuid
        rr1_evidence = dict(scenario=scenario, states=[], full_reads=[], narrow_reads=[],
            preparations=[], first_new_signs=[])
        rr1_phase = ['original_execution' if not legacy else 'legacy_cached_startup']
        rr1_original_orders = {o['order_id']: deepcopy(o) for o in exchange.orders}
        rr1_original_post = exchange.lp_post_order
        rr1_original_sign = exchange.lp_create_limit_order
        rr1_catalog = exchange.lp_reward_catalog
        def rr1_document():
            with sqlite3.connect(store.path) as connection:
                return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
        def rr1_audits():
            return {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        def rr1_capture(label):
            state = deepcopy(engine.lp_auto_state())
            rr1_evidence['states'].append(dict(label=label, state=state,
                document=rr1_document(), sessions=deepcopy(store.lp_sessions()), audits=rr1_audits()))
            return state
        for name in ('lp_account_snapshot', 'lp_account_snapshot_shared'):
            original_read = getattr(exchange, name)
            def rr1_account(*args, _read=original_read, _name=name, **kwargs):
                observation = dict(method=_name, phase=rr1_phase[0], called_at=pool.NOW)
                rr1_evidence['full_reads'].append(observation)
                try:
                    packet = _read(*args, **kwargs)
                except BaseException as exc:
                    observation.update(failed=True, error=type(exc).__name__)
                    raise
                observation.update(failed=False, packet=deepcopy(packet))
                return packet
            monkeypatch.setattr(exchange, name, rr1_account)
        rr1_result_reader = exchange.lp_order_result_snapshot
        def rr1_result(request):
            packet = rr1_result_reader(request)
            if case == 'cancel_pending':
                for field in ('orders', 'open_orders'):
                    for row in packet[field]:
                        original_row = rr1_original_orders.get(row.get('order_id'))
                        if (original_row and row.get('order_id') in exchange.cancels
                                and original_row['token_id'] in {'A', 'D', 'I'}
                                and row.get('status') == 'CANCELED'
                                and all(row.get(k) == original_row.get(k) for k in
                                    ('token_id', 'condition_id', 'market_id', 'outcome', 'side'))
                                and row['side'] == 'BUY' and Decimal(str(row['price'])) == Decimal('.39')
                                and Decimal(str(row['original_size'])) == 20
                                and Decimal(str(row['size_matched'])) == 0
                                and row.get('fill_quantity_known') in (None, True)):
                            row['fill_quantity_known'] = True
            rr1_evidence['narrow_reads'].append(dict(phase=rr1_phase[0], request=deepcopy(request), packet=deepcopy(packet)))
            return packet
        monkeypatch.setattr(exchange, 'lp_order_result_snapshot', rr1_result)
        def rr1_prepare(service):
            rr1_phase[0] = 'explicit_cold_preparation'
            def catalog(*, condition_ids=None, **kwargs):
                result = rr1_catalog(condition_ids=tuple(exchange.rewards) if condition_ids is None else condition_ids, **kwargs)
                return {**result, 'state': 'known', 'complete': True}
            def history(token_ids, *, start_ts, end_ts, **kwargs):
                return dict(state='known', history={token: [dict(t=start_ts, p='.390'),
                    dict(t=end_ts, p='.391')] for token in token_ids})
            def competition(**kwargs):
                return dict(state='known', complete=True, checked_at=pool.NOW,
                    round_checked_at=pool.NOW, competitiveness={token: (Decimal('2.5'), pool.NOW)
                        for token in exchange.rewards})
            monkeypatch.setattr(exchange, 'lp_reward_catalog', catalog)
            monkeypatch.setattr(exchange, 'lp_market_metadata', exchange.lp_market_metadata_fresh, raising=False)
            monkeypatch.setattr(exchange, 'lp_price_history', history, raising=False)
            monkeypatch.setattr(exchange, 'lp_market_competitiveness', competition, raising=False)
            competition_state = service.refresh_competition_cache()
            prepared = service.refresh_price_history()
            qualified = service.refresh_candidates(force=True)
            rr1_evidence['preparations'].append(deepcopy(dict(competition=competition_state,
                preparation=prepared, qualification=qualified)))
            assert competition_state['state'] == 'known' and competition_state['complete']
            assert prepared['preparation_outcome'] == 'success', prepared
            expected = ('E', 'F', 'G') if case == 'cancel_pending' else (
                ('B',) if case in ('unknown_post', 'accepted_idless') and not legacy else ('B', 'C', 'E', 'F', 'G'))
            for token in expected:
                row, = [row for row in qualified['candidates'] if row['condition_id'] == token]
                assert row['selected_direction']['token_id'] == token
                assert row['selected_direction']['eligible'] is True
        def rr1_new_sign(**kwargs):
            signed = rr1_original_sign(**kwargs)
            rr1_phase[0] = 'new_approved_execution'
            rr1_evidence['first_new_signs'].append(dict(signed=deepcopy(signed),
                state=deepcopy(engine.lp_auto_state()), document=rr1_document()))
            return signed
    try:
        if not legacy:
            if case == 'approved_unsent':
                held = engine._lock_path.open('a+')
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif case == 'cancel_pending':
                exchange.cancel_terminal = False
            elif case in ('unknown_post', 'accepted_idless'):
                post = exchange.lp_post_order
                def unknown(signed):
                    if signed['token_id'] == 'B':
                        exchange.posts.append(signed)
                        if case == 'unknown_post':
                            raise TimeoutError('actual POST outcome unknown')
                        return dict(accepted=True, status='ACCEPTED')
                    return post(signed)
                monkeypatch.setattr(exchange, 'lp_post_order', unknown)
            if case == 'sign_interrupted':
                with pytest.raises(SystemExit, match='original sign invocation ended before POST'):
                    engine.lp_auto_run_once()
                exchange.before_sign = None
            elif trigger:
                error = ('resource rejection persisted before plan progress' if resource_rejection
                    else f'{"rejected" if case == "rejected_before_progress" else "accepted"} receipt persisted before plan progress')
                with pytest.raises(sqlite3.IntegrityError, match=error):
                    engine.lp_auto_run_once()
            else:
                engine.lp_auto_run_once()
        saved = engine.lp_auto_state()
        assert saved['last_round']['round_id']
        previous_posts, previous_cancels = deepcopy(exchange.posts), list(exchange.cancels)
        audit = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        if rr1:
            original = deepcopy(saved['last_round'])
            rr1_capture('original_before_cold_rebuild')
            original_ids = {a['action_id'] for a in original['actions']}
            original_requests = {a.get('request_id', a['action_id']) for a in original['actions'] if a['kind'] == 'buy'}
            original_sessions = {s['session_id']: deepcopy(s) for s in store.lp_sessions()}
            original_controls = {sid: {key: deepcopy(session.get(key)) for key in
                ('stop_requested', 'entry_cancel_requested', 'stop_loss_latched', 'passive_cancel_requested',
                 'queue_protection_cancel_requested', 'queue_protection', 'queue_protection_levels')}
                for sid, session in original_sessions.items()}
            interval = saved['round_interval_seconds']
            assert interval == (60 if legacy else 30)
            assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20
                for t in original['targets'])
            assert saved['desired_running'] is True and saved['buy_price_level'] == 2
            if not legacy:
                assert saved['active_plan'] is None and original['completed_at']
                assert saved['plan_wait']['kind'] == 'round'
                assert datetime.fromisoformat(saved['plan_wait']['deadline']) == (
                    datetime.fromisoformat(original['completed_at']) + timedelta(seconds=interval))
            if case == 'approved_unsent':
                assert exchange.posts == exchange.cancels == [] and audit == {}
                if not legacy:
                    assert all(a['state'] == 'not_sent' for a in original['actions'])
                assert saved['slots']['occupied'] == 0 and Decimal(saved['funds']['buy_reserved_usd']) == 0
            elif case == 'cancel_pending':
                assert exchange.posts == []
                assert Counter(exchange.cancels) == Counter(['original-A', 'original-D', 'original-I'])
                assert set(live_orders(exchange)) == {'A', 'B', 'C', 'D', 'I'}
                assert saved['slots']['occupied'] == 5 and Decimal(saved['funds']['buy_reserved_usd']) == 39
                cancel_audits = [a for rows in audit.values() for a in rows if a['role'] == 'reconciliation_cancel']
                assert len(cancel_audits) == 3 and all(a['state'] == 'accepted' for a in cancel_audits)
                if not legacy:
                    dependent = [a for a in original['actions'] if a['kind'] == 'buy']
                    assert {a['token_id'] for a in dependent} == {'E', 'F', 'G'}
                    assert all(a['state'] == 'not_sent' and a['reason'] == 'next_round_capacity' for a in dependent)
            elif case in ('unknown_post', 'accepted_idless'):
                unknown_intent, = [i for i in saved['intents'] if i['token_id'] == 'B']
                unknown_session = original_sessions[unknown_intent['session_id']]
                b_audit, = [a for a in audit[unknown_session['session_id']] if a['role'] == 'entry']
                uuid.UUID(b_audit['action_id'])
                assert unknown_session['idempotency_key'] == 'lp-auto:' + unknown_intent['intent_id']
                assert b_audit['action_key'] == unknown_session['session_id'] + ':entry-submit'
                assert unknown_intent['state'] == 'unknown' and not unknown_intent.get('order_id')
                assert unknown_intent.get('settled') is not True
                assert Decimal(unknown_intent['reserved_usd']) == Decimal('7.80')
                assert not b_audit.get('order_id') and not unknown_session.get('entry_order_id')
                if not legacy:
                    assert b_audit['state'] == ('unknown' if case == 'unknown_post' else 'accepted')
                    assert b_audit['submit_stage'] == ('send_unknown' if case == 'unknown_post' else 'receipt_received')
                    assert b_audit['post_started'] is True
                    assert saved['slots']['occupied'] == 5 and Decimal(saved['funds']['buy_reserved_usd']) == 39
                    assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
                    assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
                else:
                    assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B'])
                    assert live_orders(exchange) == {}
                assert not rr1_document()['intents'][unknown_intent['intent_id']].get('reservation_coverage')
            else:
                assert case == 'completed_wait' and saved['active_plan'] is None
                assert saved['plan_wait']['kind'] == 'round'
                assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
                assert saved['slots']['occupied'] == 5 and Decimal(saved['funds']['buy_reserved_usd']) == 39
                assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
            deadline = datetime.fromisoformat(saved['plan_wait']['deadline'])
            engine = restart_engine(engine, exchange)
            assert engine._lp is not lp and engine._store is not store
            cold = rr1_capture('cold_cached_startup')
            assert cold['active_plan'] == saved['active_plan'] and cold['plan_wait'] == saved['plan_wait']
            assert cold['last_round']['actions'] == original['actions'] and cold['last_round']['targets'] == original['targets']
            assert cold['round_interval_seconds'] == interval
            assert rr1_audits() == audit and exchange.posts == previous_posts and exchange.cancels == previous_cancels
            scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
            if deadline > pool.NOW:
                monkeypatch.setattr(pool, 'NOW', deadline - timedelta(seconds=1))
                counts = len(rr1_evidence['full_reads']), len(rr1_evidence['narrow_reads'])
                scheduler.request_check()
                assert not scheduler.run_due()
                engine.lp_auto_run_once()
                assert (len(rr1_evidence['full_reads']), len(rr1_evidence['narrow_reads'])) == counts
                assert rr1_audits() == audit and exchange.posts == previous_posts and exchange.cancels == previous_cancels
                rr1_capture('inherited_deadline_silence')
            if case == 'completed_wait':
                assert deadline > pool.NOW
                return
            if held is not None:
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
                held.close()
                held = None
            # Only NEW requests use the restored normal external POST carrier.
            monkeypatch.setattr(exchange, 'lp_post_order', rr1_original_post)
            monkeypatch.setattr(pool, 'NOW', max(pool.NOW, deadline))
            if case == 'cancel_pending':
                for order in exchange.orders:
                    if order['order_id'] in previous_cancels:
                        order.update(status='CANCELED', size_matched='0')
                rr1_evidence['external_terminal_rows'] = deepcopy([row for row in exchange.orders
                    if row['order_id'] in previous_cancels])
            rr1_prepare(engine._lp)
            rr1_capture('after_public_cold_preparation')
            rr1_phase[0] = 'current_planning'
            monkeypatch.setattr(exchange, 'lp_create_limit_order', rr1_new_sign)
            assert scheduler.run_due()
            state = rr1_capture('new_current_completed')
            current = deepcopy(state['last_round'])
            assert current['round_id'] != original['round_id'] and current['completed_at']
            assert state['active_plan'] is None and state['plan_wait']['kind'] == 'round'
            completed = datetime.fromisoformat(current['completed_at'])
            assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=interval)
            assert {a['action_id'] for a in current['actions']}.isdisjoint(original_ids)
            new_buys = [a for a in current['actions'] if a['kind'] == 'buy']
            expected_new = {'E', 'F', 'G'} if case == 'cancel_pending' else (
                {'B'} if case in ('unknown_post', 'accepted_idless') and not legacy else {'B', 'C', 'E', 'F', 'G'})
            assert {a['token_id'] for a in new_buys} == expected_new
            assert all(a['state'] == 'success' for a in new_buys)
            assert {a.get('request_id', a['action_id']) for a in new_buys}.isdisjoint(original_requests)
            assert {a['session_id'] for a in new_buys}.isdisjoint(original_sessions)
            for action in new_buys:
                new_session = store.lp_session(action['session_id'])
                assert new_session['idempotency_key'] == 'lp-auto:' + action.get('request_id', action['action_id'])
                receipt, = [a for a in store.lp_actions(action['session_id']) if a['role'] == 'entry']
                assert receipt['state'] == 'accepted' and receipt['order_id'] == action['order_id']
                assert receipt['action_id'] not in {a['action_id'] for rows in audit.values() for a in rows}
            assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in current['targets'])
            assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20
                and p['side'] == 'BUY' and p['post_only'] is True for p in exchange.posts)
            assert set(live_orders(exchange)) == {'B', 'C', 'E', 'F', 'G'}
            assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 39
            assert exchange.cancels == previous_cancels
            for sid, rows in audit.items():
                assert store.lp_actions(sid) == rows
                expected_controls = deepcopy(original_controls[sid])
                if case == 'cancel_pending' and not legacy and original_sessions[sid]['token_id'] in {'A', 'D', 'I'}:
                    expected_controls['entry_cancel_requested'] = False
                    assert store.lp_session(sid).get('yield_rotation_cancel_owners') == {}
                assert {key: deepcopy(store.lp_session(sid).get(key)) for key in expected_controls} == expected_controls
            if case == 'cancel_pending':
                assert Counter(p['token_id'] for p in exchange.posts) == Counter(['E', 'F', 'G'])
                assert live_orders(exchange)['B'] == 'original-B' and live_orders(exchange)['C'] == 'original-C'
                if legacy:
                    # Legacy occupancy uses the acquired complete account packet;
                    # its old session history and cancellation flags remain untouched.
                    rows = rr1_evidence['external_terminal_rows']
                    assert {row['order_id'] for row in rows} == set(previous_cancels)
                    for row in rows:
                        expected = rr1_original_orders[row['order_id']]
                        assert all(row[k] == expected[k] for k in
                            ('order_id', 'condition_id', 'market_id', 'token_id', 'outcome', 'side'))
                        assert row['status'] == 'CANCELED' and row['side'] == 'BUY'
                        assert Decimal(str(row['price'])) == Decimal('.39')
                        assert Decimal(str(row['original_size'])) == 20 and Decimal(str(row['size_matched'])) == 0
                    observations = rr1_evidence['first_new_signs']
                    assert {o['signed']['token_id'] for o in observations} == {'E', 'F', 'G'}
                    document = observations[0]['document']
                    financial = document['account_financial_facts']
                    assert financial['financial_status'] == 'known' and financial['snapshot_id']
                    assert financial['account_id'] == 'test-wallet'
                    assert financial['pool_account_id'] == document['account_id'] == hashlib.sha256(b'test-wallet').hexdigest()
                    assert document['active_plan']['approval_account_facts'] == financial
                    packets = [read['packet'] for read in rr1_evidence['full_reads']
                        if read['method'] == 'lp_account_snapshot_shared' and read['phase'] == 'current_planning'
                        and not read['failed'] and datetime.fromisoformat(str(read['packet']['read_started_at'])) ==
                            datetime.fromisoformat(financial['read_started_at'])]
                    packet, = packets
                    assert packet['authenticated'] is True and packet['wallet_address'] == packet['account_id'] == 'test-wallet'
                    assert all(packet[key] is True for key in ('balance_complete', 'open_orders_complete',
                        'positions_complete', 'trades_complete', 'pagination_complete'))
                    assert packet['positions'] == packet['raw_trades'] == financial['positions'] == []
                    assert Decimal(financial['inventory_cost_usd']) == 0
                    assert type(packet['trade_generation']) is int
                    assert packet['trade_generation'] == financial['trade_generation']
                    for key in ('read_started_at', 'read_ended_at', 'checked_at'):
                        assert datetime.fromisoformat(str(packet[key])) == datetime.fromisoformat(financial[key])
                    assert datetime.fromisoformat(financial['read_started_at']) <= datetime.fromisoformat(
                        financial['checked_at']) <= datetime.fromisoformat(financial['read_ended_at'])
                    assert Decimal(str(packet['balance'])) == Decimal(financial['balance_usd'])
                    assert Decimal(str(packet['allowance'])) == Decimal(financial['allowance_usd'])
                    assert {row['order_id'] for row in packet['open_orders']} == {'original-B', 'original-C'}
                    assert len(packet['open_orders']) == len(financial['buys']) == 2
                    for row in packet['open_orders']:
                        expected = rr1_original_orders[row['order_id']]
                        assert all(row[key] == expected[key] for key in
                            ('order_id', 'condition_id', 'market_id', 'token_id', 'outcome', 'side', 'status'))
                        assert row['side'] == 'BUY' and row['status'] == 'LIVE'
                        assert Decimal(str(row['price'])) == Decimal('.39')
                        assert Decimal(str(row['original_size'])) == 20 and Decimal(str(row['size_matched'])) == 0
                        buy, = [buy for buy in financial['buys'] if buy['order_id'] == row['order_id']]
                        assert all(buy[key] == row[key] for key in
                            ('order_id', 'condition_id', 'market_id', 'token_id', 'outcome', 'side', 'status'))
                        assert buy['financial_status'] == 'known' and buy['state'] == 'active'
                        assert Decimal(buy['price']) == Decimal('.39')
                        assert Decimal(buy['original_quantity']) == Decimal(buy['quantity']) == 20
                        assert Decimal(buy['filled_quantity']) == 0 and Decimal(buy['reserved_usd']) == Decimal('7.80')
                        assert buy['session_id'] in original_sessions and original_sessions[buy['session_id']]['token_id'] == row['token_id']
                    assert sum(Decimal(str(row['price'])) * Decimal(str(row['original_size']))
                        for row in packet['open_orders']) == Decimal('15.60')
                    assert sum(Decimal(buy['reserved_usd']) for buy in financial['buys']) == Decimal('15.60')
                    assert rr1_evidence['narrow_reads'] == []
                    rr1_evidence['legacy_acquired_account'] = deepcopy(dict(packet=packet, financial=financial,
                        external_terminal_rows=rows))
                else:
                    assert any(all(row['fill_quantity_known'] is True for row in read['packet']['orders']
                        if row['order_id'] in previous_cancels) for read in rr1_evidence['narrow_reads']
                        if read['phase'] != 'original_execution')
            elif case in ('unknown_post', 'accepted_idless'):
                expected_posts = Counter(['B', 'B', 'C', 'E', 'F', 'G'])
                assert Counter(p['token_id'] for p in exchange.posts) == expected_posts
                old = rr1_document()['intents'][unknown_intent['intent_id']]
                assert old['state'] == 'unknown' and not old.get('order_id') and old.get('settled') is not True
                assert old['session_id'] == unknown_intent['session_id'] and Decimal(old['reserved_usd']) == Decimal('7.80')
                assert store.lp_actions(unknown_session['session_id']) == [b_audit]
                assert not store.lp_session(unknown_session['session_id']).get('entry_order_id')
                observations = [o for o in rr1_evidence['first_new_signs']
                    if o['signed']['token_id'] == 'B']
                assert len(observations) == 1
                document = observations[0]['document']
                marker = document['intents'][unknown_intent['intent_id']]['reservation_coverage']
                financial = document['account_financial_facts']
                assert marker['version'] == 1 and marker['state'] == 'covered'
                assert marker['intent_id'] == unknown_intent['intent_id'] and marker['session_id'] == unknown_session['session_id']
                assert marker['account_id'] == financial['account_id'] == 'test-wallet'
                assert marker['pool_account_id'] == document['account_id'] == financial['pool_account_id']
                assert hashlib.sha256(marker['account_id'].encode()).hexdigest() == marker['pool_account_id']
                assert marker['snapshot_id'] == financial['snapshot_id'] and marker['snapshot_id']
                assert Decimal(marker['original_reserved_usd']) == Decimal('7.80')
                assert financial['financial_status'] == 'known'
                finished = b_audit.get('submit_finished_at') or b_audit.get('submit_receipt_at') or b_audit['updated_at']
                assert datetime.fromisoformat(marker['request_finished_at']) == datetime.fromisoformat(finished)
                assert datetime.fromisoformat(marker['request_finished_at']) < datetime.fromisoformat(marker['read_started_at'])
                assert marker['basis'] == ('ended_send' if b_audit.get('submit_finished_at') or b_audit.get('submit_receipt_at') else 'legacy_finished_action')
                packets = [row['packet'] for row in rr1_evidence['full_reads']
                    if row['method'] == 'lp_account_snapshot_shared' and not row['failed']
                    and datetime.fromisoformat(str(row['packet']['read_started_at'])) == datetime.fromisoformat(marker['read_started_at'])]
                packet, = packets
                assert packet['authenticated'] is True and packet['wallet_address'] == marker['account_id']
                assert all(packet[key] is True for key in ('balance_complete', 'open_orders_complete',
                    'positions_complete', 'trades_complete', 'pagination_complete'))
                assert Decimal(str(packet['balance'])) == Decimal(financial['balance_usd'])
                assert Decimal(str(packet['allowance'])) == Decimal(financial['allowance_usd'])
                assert {o['order_id'] for o in packet['open_orders']} == (
                    set() if legacy else {o['order_id'] for o in exchange.orders if o['token_id'] in {'C', 'E', 'F', 'G'}})
                rr1_evidence['coverage'] = deepcopy(dict(marker=marker, financial=financial, packet=packet,
                    old_intent=old, old_audit=b_audit))
            else:
                assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
            assert not any(read['phase'] == 'new_approved_execution' for read in rr1_evidence['full_reads'])
            # Duplicate/current-cold wakes stay silent inside the actual new interval.
            monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=interval - 1))
            before_posts, before_cancels = deepcopy(exchange.posts), list(exchange.cancels)
            before_audits = rr1_audits()
            counts = len(rr1_evidence['full_reads']), len(rr1_evidence['narrow_reads'])
            scheduler.request_check()
            assert not scheduler.run_due()
            with ThreadPoolExecutor(2) as workers:
                list(workers.map(lambda _: engine.lp_auto_run_once(), range(2)))
            engine = restart_engine(engine, exchange)
            engine.lp_auto_run_once()
            assert exchange.posts == before_posts and exchange.cancels == before_cancels
            assert rr1_audits() == before_audits
            assert (len(rr1_evidence['full_reads']), len(rr1_evidence['narrow_reads'])) == counts
            rr1_capture('new_interval_concurrent_and_cold_silence')
            return
    finally:
        if rr1:
            rr1_evidence['final_state'] = deepcopy(engine.lp_auto_state())
            rr1_evidence['final_audits'] = rr1_audits()
            (tmp_path / 'RR1-public-restart-evidence.json').write_text(json.dumps(rr1_evidence, default=str, indent=2) + '\n')
        if trigger:
            with sqlite3.connect(store.path) as connection:
                connection.execute('DROP TRIGGER IF EXISTS fail_plan_progress')
        if held is not None:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()


@pytest.mark.parametrize('outcome', ['live_before_cancel', 'terminal_allowed', 'terminal_rejected'])
def test_partial_fill_preserves_inventory_without_replanning(tmp_path, monkeypatch, outcome):
    from collections import Counter
    from tests.test_lp_auto_target_convergence import publish_candidates, REWARDS
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'I'), budget='39')
    original_ids = live_orders(exchange)
    for order in exchange.orders:
        order['fill_quantity_known'] = True
    result_reader = install_order_result_reader(exchange, monkeypatch)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    cancel, post = exchange.cancel_order, exchange.lp_post_order
    captured, packets, financial_reads, protections = [], [], [], []
    def fill():
        next(o for o in exchange.orders if o['order_id'] == 'original-A')['size_matched'] = '8'
        exchange.positions = [dict(token_id='A', condition_id='A', size='8', average_price='.39')]
        exchange.trades = [dict(trade_id='actual-A-fill', order_id='original-A', condition_id='A', token_id='A', side='BUY',
            size='8', price='.39', fee='0', status='CONFIRMED', timestamp=pool.NOW, trader_side='MAKER',
            taker_order_id='external-taker', maker_orders=[dict(order_id='original-A', token_id='A', side='BUY',
                price='.39', matched_amount='8', fee='0', maker_address='test-wallet', owner='credential-owner-uuid')])]
    def current_results(request):
        if not captured:
            approved = engine.lp_auto_state()
            assert approved['active_plan'], 'Real public approval precedes actual external result read'
            captured.append(deepcopy(approved))
            original_a = next(s for s in store.lp_sessions() if s['entry_order_id']=='original-A')
            protections.append(deepcopy(original_a['queue_protection']))
            if outcome=='live_before_cancel':
                fill()
        packet = result_reader(request)
        packets.append(deepcopy(packet))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', current_results)
    def venue_cancel(oid):
        if oid == 'original-A' and outcome != 'live_before_cancel':
            fill()
        return cancel(oid)
    def venue_post(signed):
        if outcome == 'terminal_rejected' and signed['token_id'] == 'E':
            exchange.posts.append(signed)
            return dict(accepted=False, status='REJECTED', reason='balance_insufficient')
        return post(signed)
    monkeypatch.setattr(exchange, 'cancel_order', venue_cancel)
    monkeypatch.setattr(exchange, 'lp_post_order', venue_post)
    for name in ('lp_account_snapshot', 'lp_account_snapshot_shared'):
        external = getattr(exchange,name)
        def observed(*args,_read=external,_name=name,**kwargs):
            financial_reads.append(dict(method=_name,at=pool.NOW,active=engine.lp_auto_state()['active_plan'] is not None))
            return _read(*args,**kwargs)
        monkeypatch.setattr(exchange,name,observed)
    assert scheduler.run_due()
    first = engine.lp_auto_state()
    original = deepcopy(first['last_round'])
    approved = captured[0]
    assert [t['condition_id'] for t in approved['last_round']['targets']]==['B','C','E','F','G']
    assert original['round_id']==approved['last_round']['round_id']
    assert original['targets']==approved['last_round']['targets']
    assert [a['action_id'] for a in original['actions']]==[a['action_id'] for a in approved['active_plan']['actions']]
    assert original['completed_at'] and first['active_plan'] is None and first['plan_wait']['kind']=='round'
    assert exchange.posts==[], 'All five original slots/39 principal were occupied at approval'
    assert Counter(exchange.cancels)==Counter(['original-D','original-I'] if outcome=='live_before_cancel' else
        ['original-A','original-D','original-I'])
    assert not any(r['active'] for r in financial_reads), 'No new finance read during original approved execution'
    assert first['funds']['as_of']==approved['funds']['as_of']
    original_a = next(s for s in store.lp_sessions() if s['entry_order_id']=='original-A')
    sid = original_a['session_id']
    if outcome=='live_before_cancel':
        assert live_orders(exchange)['A']=='original-A'
        assert store.lp_session(sid)['queue_protection']==protections[0]
        session = engine.lp_status(sid)
        assert Decimal(session['buy_filled_quantity'])==Decimal(session['residual_quantity'])==8
        assert Decimal(session['buy_cost'])==Decimal('3.12') and Decimal(session['buy_fees'])==0
        exact, = [o for o in packets[0]['orders'] if o['order_id']=='original-A']
        assert exact['status']=='LIVE' and exact['fill_quantity_known'] is True and Decimal(exact['size_matched'])==8
    old_audits = {s['session_id']:deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    completed = datetime.fromisoformat(original['completed_at'])
    deadline = datetime.fromisoformat(first['plan_wait']['deadline'])
    assert deadline==completed+timedelta(seconds=30)
    before = (len(financial_reads),len(packets))
    for elapsed in (1,29):
        monkeypatch.setattr(pool,'NOW',completed+timedelta(seconds=elapsed))
        scheduler.request_check()
        assert not scheduler.run_due()
        engine.lp_auto_run_once()
        assert (len(financial_reads),len(packets))==before and exchange.posts==[]
        assert Counter(exchange.cancels)==Counter(['original-D','original-I'] if outcome=='live_before_cancel' else
            ['original-A','original-D','original-I'])
        assert {s:store.lp_actions(s) for s in old_audits}==old_audits
    monkeypatch.setattr(pool,'NOW',deadline)
    publish_candidates(exchange,lp,store,REWARDS)
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert len(financial_reads)>before[0] and not any(r['active'] for r in financial_reads)
    assert state['last_round']['round_id']!=original['round_id'] and state['last_round']['completed_at']
    assert state['active_plan'] is None
    session = engine.lp_status(sid)
    assert Decimal(session['buy_filled_quantity'])==Decimal(session['residual_quantity'])==8
    assert Decimal(session['buy_cost'])==Decimal('3.12') and Decimal(session['buy_fees'])==0
    assert exchange.positions==[dict(token_id='A',condition_id='A',size='8',average_price='.39')]
    assert Decimal(state['funds']['inventory_cost_usd'])==Decimal('3.12')
    assert Counter(p['token_id'] for p in exchange.posts)==Counter(['E','F'])
    new_buys = [a for a in state['last_round']['actions'] if a['kind']=='buy']
    assert {a['token_id'] for a in new_buys}=={'E','F'}
    assert {a['action_id'] for a in new_buys}.isdisjoint({a['action_id'] for a in original['actions']})
    assert all(a.get('session_id') and a['session_id'] not in old_audits for a in new_buys)
    current = live_orders(exchange)
    assert current['B']==original_ids['B'] and current['C']==original_ids['C']
    if outcome=='live_before_cancel':
        assert current=={**{k:original_ids[k] for k in ('A','B','C')},'E':current['E'],'F':current['F']}
        assert state['slots']['occupied']==5 and Decimal(state['funds']['buy_reserved_usd'])==Decimal('35.88')
        assert store.lp_session(sid)['queue_protection']==protections[0]
        assert all(a['state']=='success' for a in new_buys)
    else:
        assert [t['condition_id'] for t in state['last_round']['targets']]==['B','C','E','F']
        assert next(o for o in exchange.orders if o['order_id']=='original-A')['status']=='CANCELED'
        expected={'B','C','E','F'} if outcome=='terminal_allowed' else {'B','C','F'}
        assert set(current)==expected
        assert state['slots']['occupied']==(4 if outcome=='terminal_allowed' else 3)
        assert Decimal(state['funds']['buy_reserved_usd'])==(Decimal('31.20') if outcome=='terminal_allowed' else Decimal('23.40'))
        if outcome=='terminal_rejected':
            refused, = [a for a in new_buys if a['token_id']=='E']
            assert refused['state']=='rejected' and refused['reason']=='balance_insufficient'
        else:
            assert all(a['state']=='success' for a in new_buys)
    actual_buys = sum(Decimal(o['price'])*(Decimal(o['original_size'])-Decimal(o['size_matched']))
        for o in exchange.orders if o['status']=='LIVE')
    expected_total={'live_before_cancel':Decimal('39'),'terminal_allowed':Decimal('34.32'),'terminal_rejected':Decimal('26.52')}[outcome]
    assert actual_buys+Decimal(session['buy_cost'])==expected_total
    assert Decimal(state['funds']['buy_reserved_usd'])+Decimal(state['funds']['inventory_cost_usd'])==expected_total
    assert {s:store.lp_actions(s) for s in old_audits}==old_audits
    assert Counter(exchange.cancels)==Counter(['original-D','original-I'] if outcome=='live_before_cancel' else
        ['original-A','original-D','original-I'])
    assert all(p['side']=='BUY' and Decimal(p['price'])==Decimal('.39') and Decimal(p['quantity'])==20 for p in exchange.posts)
    assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(state['last_round']['completed_at'])+timedelta(seconds=30)


@pytest.mark.parametrize('financial_change', ['ttl', 'generation'])
def test_execution_diagnostics_separate_planning_and_result_reads(runtime, monkeypatch, caplog, financial_change):
    import logging
    from polymarket.models.clob import RejectedOrder
    from tests.test_lp_auto_refill_contract import prepare, RefillPublic
    from tests.test_lp_account_reservation_reconciliation import _refill_identity
    from tests.test_lp_causal_diagnostics import events
    from tests.test_lp_read_diagnostics import wait_read_logs
    wait_read_logs()
    caplog.set_level(logging.INFO)
    public = RefillPublic(runtime.clock)
    public.rates = {1: '120', 2: '96', 3: '72'}
    store, _, account, lp, engine, _ = prepare(runtime, count=3, target=3, public=public)
    engine.lp_auto_set_desired_running(False)
    engine.lp_auto_configure(dict(budget_usd='39', target_buy_count=3, buy_price_level=2))
    engine.lp_auto_set_desired_running(True)
    balance, sign, post = account.get_balance_allowance, account.create_limit_order, account.post_order
    all_balance_calls, during_plan, captured = [], [], []
    def observe_balance(**kwargs):
        active = engine.lp_auto_state()['active_plan']
        all_balance_calls.append((runtime.clock[0], bool(active)))
        if active:
            during_plan.append((runtime.clock[0], kwargs))
            raise TimeoutError('actual SDK balance/allowance unavailable while public active_plan exists')
        return balance(**kwargs)
    def observe_sign(**kwargs):
        if not captured:
            state = engine.lp_auto_state()
            captured.append(deepcopy(state))
            assert state['active_plan'], 'Actual first preparation occurs after durable plan'
            if financial_change == 'ttl':
                runtime.clock[0] += timedelta(seconds=61)
            else:
                assert store.lp_advance_trade_generation(store.lp_trade_generation())
        return sign(**kwargs)
    def venue(signed):
        if signed.token_id == _refill_identity(2)[2]:
            account.posts.append(signed)
            return RejectedOrder(code='not_enough_balance', message='venue balance/allowance denied')
        if signed.token_id == _refill_identity(3)[2]:
            account.posts.append(signed)
            raise TimeoutError('actual original POST receipt unknown')
        return post(signed)
    monkeypatch.setattr(account, 'get_balance_allowance', observe_balance)
    monkeypatch.setattr(account, 'create_limit_order', observe_sign)
    monkeypatch.setattr(account, 'post_order', venue)
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert len(captured) == 1 and during_plan == []
    assert all_balance_calls and all(not active for _, active in all_balance_calls)
    assert len(account.posts) == 3
    assert [p.token_id for p in account.posts] == [_refill_identity(i)[2] for i in (1, 2, 3)]
    assert all(Decimal(p.maker_amount) / p.taker_amount == Decimal('.39')
        and Decimal(p.taker_amount) / 1000000 == 20 for p in account.posts)
    actions = state['active_plan']['actions']
    assert [a['state'] for a in actions] == ['success', 'rejected', 'unknown']
    assert actions[1]['reason'] == 'not_enough_balance'
    audits = {a['session_id']: store.lp_actions(a['session_id']) for a in actions}
    assert [audits[a['session_id']][0]['state'] for a in actions] == ['accepted', 'rejected', 'unknown']
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['as_of'] == captured[0]['funds']['as_of']
    assert state['runtime_state'] == 'running'
    assert state['reason'] == state['last_round']['reason'] == 'submission_unknown'
    assert state['plan_wait']['kind'] == 'order' and not state['last_round']['completed_at']
    open_orders = account.list_open_orders
    failed_at = []
    def unavailable(**kwargs):
        runtime.clock[0] += timedelta(seconds=3)
        failed_at.append(runtime.clock[0])
        raise TimeoutError('necessary actual SDK order result read failed')
    monkeypatch.setattr(account, 'list_open_orders', unavailable)
    runtime.clock[0] = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert failed_at and state['plan_wait']['kind'] == 'api'
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == failed_at[-1] + timedelta(seconds=60)
    assert state['runtime_state'] == 'running'
    assert state['reason'] == state['last_round']['reason'] == 'order_result_incomplete'
    assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == captured[0]['funds']['as_of']
    assert during_plan == [] and len(account.posts) == 3
    failed_end = failed_at[-1]
    calls = len(all_balance_calls)
    for elapsed in (1, 59):
        runtime.clock[0] = failed_end + timedelta(seconds=elapsed)
        scheduler.request_check()
        assert not scheduler.run_due()
        assert len(failed_at) == 1 and len(all_balance_calls) == calls
        assert len(account.posts) == 3 and during_plan == []
    monkeypatch.setattr(account, 'list_open_orders', open_orders)
    runtime.clock[0] = datetime.fromisoformat(state['plan_wait']['deadline'])
    assert scheduler.run_due()
    state = engine.lp_auto_state()
    assert state['active_plan']['actions'][2]['state'] == 'unknown'
    assert state['runtime_state'] == 'running' and state['reason'] == 'submission_unknown'
    assert {sid: store.lp_actions(sid) for sid in audits} == audits
    assert during_plan == [] and len(account.posts) == 3
    wait_read_logs()
    assert not events(caplog, 'auto_presend_account_use') and not events(caplog, 'auto_send_account_use')
    planning = events(caplog, 'auto_account_use')
    assert len(planning) == 1
    assert planning[0]['checked_at'] == captured[0]['funds']['as_of']
    results = events(caplog, 'auto_order_result_use')
    assert len(results) == 1 and results[0]['read_started_at'] and results[0]['read_ended_at']
    assert datetime.fromisoformat(results[0]['read_ended_at']) > datetime.fromisoformat(planning[0]['read_ended_at'])
    assert state['funds']['as_of'] == captured[0]['funds']['as_of']


@pytest.mark.parametrize('mode', ['prospective', 'legacy_terminal', 'legacy_live',
    'legacy_incomplete', 'legacy_wrong_maker'])
def test_successful_terminal_owner_does_not_block_later_refused_round(runtime, monkeypatch, tmp_path, mode):
    import base64
    import hashlib
    import json
    from pathlib import Path
    import sqlite3
    import zlib
    from polymarket.models.clob.account import OpenOrder
    from time import monotonic
    from polymarket.models.clob.cancel import CancelOrdersResponse
    from polymarket.models.clob.order_book import OrderBookLevel
    from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic
    from tests.test_lp_order_registration_contract import _open_order

    public = _CandidateSDKPublic(runtime.clock[0])
    book = public.get_order_book
    protective, monitor_phase, pause = [False], [False], Event()
    protective_book, healthy_book = Event(), Event()
    stage = ['setup']
    evidence = dict(mode=mode, states=[], financial_reads=[], market_reads=[], result_packets=[],
        full_packets=[], exact_reads=[], list_reads=[], failed_ends=[], monitors=[])
    engine = None
    def current_book(*, token_id):
        value = book(token_id=token_id).model_copy(update={'timestamp': runtime.clock[0], 'bids': (
            OrderBookLevel(price=Decimal('.40'), size=Decimal('3000' if protective[0] else '1000')),
            OrderBookLevel(price=Decimal('.39'), size=Decimal('1000')))})
        evidence['market_reads'].append(dict(stage=stage[0], monitor=monitor_phase[0],
            active_plan=bool(engine and engine.lp_auto_state()['active_plan']), value=value.model_dump(mode='json')))
        (protective_book if protective[0] else healthy_book).set()
        return value
    monkeypatch.setattr(public, 'get_order_book', current_book)
    originals = tuple(_open_order('rank-'+letter, 'BUY', price='.39', original='20') for letter in 'abc')
    original_ids = {o.id:o for o in originals}
    terminal = {}
    result_fault = [False]
    legacy = mode != 'prospective'
    if legacy:
        fixture_path = Path(__file__).parent/'fixtures/lp_auto_terminal_cancel_owners_0906.json'
        fixture_bytes = fixture_path.read_bytes()
        assert hashlib.sha256(fixture_bytes).hexdigest() == '16b4b00c9833bc8129abef4d1a9a3567c4490acc70ba45439a7e6aec5db34260'
        fixture = json.loads(fixture_bytes)
        assert fixture['source_sha'] == '090648f807b20df34890375f66e0b29f587c8432'
        raw = zlib.decompress(base64.b64decode(fixture['database']))
        assert len(raw) == 528384
        assert hashlib.sha256(raw).hexdigest() == '279de489937108c95d41a4ddf4e75aa6428a4cdd89f50d1e9e1e41e5899b556b'
        database = tmp_path/'state.sqlite/prediction_arbitrage/prediction_arbitrage.sqlite3'
        database.parent.mkdir(parents=True)
        database.write_bytes(raw)
        runtime.clock[0] = datetime.fromisoformat(fixture['business_time'])
        originals = tuple(OpenOrder.model_validate(o) for o in fixture['external_orders'])
        original_ids.update({o.id:o for o in originals})
        terminal.update({o['id']:OpenOrder.model_validate(o) for o in fixture['exact_terminal_orders']})
        original_ids.update(terminal)
        evidence['fixture'] = dict(source_sha=fixture['source_sha'],encoded_sha=hashlib.sha256(fixture_bytes).hexdigest(),
            decoded_sha=hashlib.sha256(raw).hexdigest(),decoded_bytes=len(raw),original_audit=fixture['original_audit'])
    store, adapter, account, lp, engine = runtime(orders=originals, public_client=public)
    if not legacy:
        engine.lp_auto_configure(dict(budget_usd='39', target_buy_count=2, buy_price_level=2))
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    session, = store.lp_active_sessions()
    sid = session['session_id']
    before = engine.lp_auto_state()
    assert before['slots']['occupied'] == 3
    assert Decimal(before['funds']['buy_reserved_usd']) == Decimal('23.60' if legacy else '23.40')
    interval = 60 if legacy else 30
    def document():
        with sqlite3.connect(store.path) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state = deepcopy(engine.lp_auto_state())
        evidence['states'].append(dict(label=label,state=state,document=document(),session=store.lp_session(sid),
            audits=store.lp_actions(sid),orders=[o.model_dump(mode='json') for o in account.orders],
            cancels=list(account.cancels),posts=deepcopy(account.posts)))
        return state
    balance = account.get_balance_allowance
    def observed_balance(**kwargs):
        evidence['financial_reads'].append(dict(stage=stage[0],monitor=monitor_phase[0],
            active_plan=bool(engine.lp_auto_state()['active_plan']),at=runtime.clock[0]))
        return balance(**kwargs)
    monkeypatch.setattr(account, 'get_balance_allowance', observed_balance)
    def venue(*, order_ids):
        account.cancels.append(order_ids)
        runtime.clock[0] += timedelta(seconds=1)
        if order_ids == ('rank-c',) or protective[0]:
            for oid in order_ids:
                terminal[oid] = original_ids[oid].model_copy(update={'status':'CANCELED'})
            account.orders = tuple(o for o in account.orders if o.id not in order_ids)
            return CancelOrdersResponse(canceled=order_ids, not_canceled={})
        assert order_ids == ('rank-d',)
        return CancelOrdersResponse(canceled=(), not_canceled={'rank-d':'venue_denied'})
    def exact_order(*, order_id):
        value = terminal.get(order_id, original_ids[order_id])
        if order_id == 'rank-c' and result_fault[0] and mode == 'legacy_wrong_maker':
            value = terminal[order_id].model_copy(update={
                'owner':'d55eb563-8b8f-4a49-a0f5-343c99f274ba',
                'maker_address':'0x'+'b'*40})
        evidence['exact_reads'].append(dict(stage=stage[0],order_id=order_id,fault=result_fault[0],
            value=value.model_dump(mode='json')))
        return value
    open_orders = account.list_open_orders
    def current_open_orders(**kwargs):
        record = dict(stage=stage[0],at=runtime.clock[0],fault=result_fault[0])
        evidence['list_reads'].append(record)
        if result_fault[0] and mode == 'legacy_incomplete':
            evidence['failed_ends'].append(deepcopy(record))
            raise RuntimeError('actual SDK list-open-orders unavailable')
        value = open_orders(**kwargs)
        record['rows'] = [o.model_dump(mode='json') for o in value]
        return value
    monkeypatch.setattr(account, 'cancel_orders', venue)
    monkeypatch.setattr(account, 'get_order', exact_order, raising=False)
    monkeypatch.setattr(account, 'list_open_orders', current_open_orders)
    result_reader = adapter.lp_order_result_snapshot
    def observed_results(request):
        packet = result_reader(request)
        evidence['result_packets'].append(deepcopy(dict(stage=stage[0],request=request,packet=packet)))
        return packet
    monkeypatch.setattr(adapter,'lp_order_result_snapshot',observed_results)
    full_reader = adapter.lp_account_snapshot_shared
    def observed_full(**kwargs):
        packet = full_reader(**kwargs)
        evidence['full_packets'].append(deepcopy(dict(stage=stage[0],active_plan=bool(engine.lp_auto_state()['active_plan']),packet=packet)))
        return packet
    monkeypatch.setattr(adapter,'lp_account_snapshot_shared',observed_full)
    scheduler = LPAutoScheduler(engine, clock=lambda:runtime.clock[0])
    def wait_interval(state, dispatched=False):
        assert state['active_plan'] is None and state['plan_wait']['kind']=='round'
        start = state['last_round']['completed_at'] if dispatched else state['plan_wait']['started_at']
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == datetime.fromisoformat(start)+timedelta(seconds=interval)
    def paused_shape(state):
        assert state['desired_running'] is False and state['active_plan'] is None
        assert state['last_round']['actions']==[] and state['last_round']['checked_at']
        wait_interval(state)
    def no_replay(state,audits,calls,elapsed):
        deadline=datetime.fromisoformat(state['plan_wait']['deadline'])
        counts=tuple(len(evidence[k]) for k in ('financial_reads','market_reads','result_packets','full_packets','exact_reads','list_reads'))
        last=deepcopy(state['last_round'])
        runtime.clock[0]=datetime.fromisoformat(state['plan_wait']['started_at'])+timedelta(seconds=elapsed)
        assert runtime.clock[0]<deadline
        scheduler.request_check();scheduler.run_due()
        repeated=engine.lp_auto_run_once()
        assert repeated['plan_wait']==state['plan_wait'] and repeated['last_round']==last
        assert tuple(len(evidence[k]) for k in ('financial_reads','market_reads','result_packets','full_packets','exact_reads','list_reads'))==counts
        assert store.lp_actions(sid)==audits and account.cancels==calls and account.posts==[]
    def monitor_until(predicate,label):
        deadline=monotonic()+5
        healthy_book.clear();monitor_phase[0]=True
        try:
            while True:
                engine.lp_tick()
                current=store.lp_session(sid)
                evidence['monitors'].append(dict(stage=stage[0],session=deepcopy(current),at=runtime.clock[0]))
                if healthy_book.is_set() and predicate(current):return current
                assert monotonic()<deadline,label
                pause.wait(min(.01,max(0,deadline-monotonic())))
        finally:
            monitor_phase[0]=False
    def assert_c_proof(start):
        reads=evidence['result_packets'][start:]
        proof=[r for r in reads if 'rank-c' in r['request'].get('owned_order_ids',())]
        assert proof, 'Actual original C requested narrow result proof is required'
        assert any(r['packet']['authenticated'] is True and r['packet']['open_orders_complete'] is True
            and r['packet']['pagination_complete'] is True and any(o.get('order_id')=='rank-c'
                and o.get('status')=='CANCELED' and o.get('fill_quantity_known') is True
                and Decimal(o['size_matched'])==0 and Decimal(o['price'])==Decimal('.39')
                and Decimal(o['original_size'])==20 and o['token_id']==session['token_id']
                for o in r['packet']['orders']) for r in proof)
    try:
        if not legacy:
            stage[0]='prospective_C_local_round'
            engine.lp_auto_set_desired_running(True)
            assert scheduler.run_due()
            sent=capture('original_C_locally_completed')
            original_c=deepcopy(sent['last_round'])
            assert sent['active_plan'] is None and original_c['completed_at']
            assert [a['order_id'] for a in original_c['actions']]==['rank-c']
            assert [r['order_id'] for r in original_c['targets']]==['rank-a','rank-b']
            assert original_c['actions'][0]['session_id']==sid and original_c['actions'][0]['state']=='canceling'
            audit_c,=deepcopy(store.lp_actions(sid))
            assert audit_c['state']=='accepted' and audit_c['order_id']=='rank-c'
            assert audit_c['rotation_round_id']==original_c['round_id']
            wait_interval(sent,dispatched=True)
            engine.lp_auto_set_desired_running(False)
            runtime.clock[0]=datetime.fromisoformat(sent['plan_wait']['deadline'])
            stage[0]='prospective_paused_C_terminal_cleanup';result_start=len(evidence['result_packets'])
            assert scheduler.run_due()
            settled=capture('paused_original_C_terminal_retired')
            paused_shape(settled);assert settled['last_round']['round_id']!=original_c['round_id']
            assert_c_proof(result_start)
            assert settled['slots']['occupied']==2 and Decimal(settled['funds']['buy_reserved_usd'])==Decimal('15.60')
            assert store.lp_actions(sid)==[audit_c] and account.cancels==[('rank-c',)] and account.posts==[]
            retired=store.lp_session(sid)
            assert retired.get('yield_rotation_cancel_owners')=={}, 'Exact successful terminal owner must retire without a rejected sibling'
            assert 'rank-c' not in retired.get('yield_rotation_protection_before',{})
            assert 'rank-c' not in retired.get('augment_cancel_requested',[])
            stage[0]='public_C_terminal_history_monitor'
            current=monitor_until(lambda s:s['order_history']['rank-c']['status']=='CANCELED'
                and s['queue_protection']['levels']['0.39']['state']=='monitoring','Public terminal-history/monitor watchdog')
            low=deepcopy(current['queue_protection']['levels']['0.39'])
            runtime.clock[0]+=timedelta(seconds=1)
            original_ids['rank-d']=_open_order('rank-d','BUY',price='.40',original='20')
            account.orders=(*account.orders,original_ids['rank-d'])
            reads=account.order_reads
            adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
            assert account.order_reads>reads
            assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
            assert {'rank-a','rank-b','rank-c','rank-d'}<=store.lp_session(sid)['order_history'].keys()
            for key in ('baseline_front','baseline_level_total','baseline_price','baseline_book_time','order_id'):
                assert store.lp_session(sid)['queue_protection']['levels']['0.39'].get(key)==low.get(key)
            stage[0]='public_D_healthy_monitor'
            monitor_until(lambda s:s['queue_protection']['levels']['0.40']['state']=='monitoring','New public price-level monitoring watchdog')
        else:
            assert store.path == database
            assert sid == fixture['original_session_id']
            assert before['active_plan'] is None
            original_c = deepcopy(before['last_round'])
            audit_c, = deepcopy(store.lp_actions(sid))
            assert audit_c == fixture['original_audit']
            assert audit_c['state'] == 'accepted' and audit_c['order_id'] == 'rank-c'
            assert audit_c['rotation_round_id'] == fixture['original_round_id'] == original_c['round_id']
            assert original_c['actions'][0]['action_id'] == fixture['original_action_id']
            assert original_c['actions'][0]['state'] == 'success' and original_c['completed_at']
            assert session['yield_rotation_cancel_owners'] == {'rank-c':original_c['round_id']}
            assert session['order_history']['rank-c']['status'] == 'CANCELED'
            assert Decimal(session['order_history']['rank-c']['size_matched']) == 0
            assert {'rank-a','rank-b','rank-c','rank-d'} <= session['order_history'].keys()
            assert session['queue_protection']['levels']['0.40']['state'] == 'monitoring'
            assert account.cancels == account.posts == []
            capture('authentic_legacy_before_C_recovery')
            original_owner=deepcopy(store.lp_session(sid))
            engine.lp_auto_set_desired_running(False)
            result_fault[0]=mode in ('legacy_incomplete','legacy_wrong_maker')
            stage[0]='first_paused_legacy_C_bad_proof' if result_fault[0] else 'first_paused_legacy_C_terminal_proof'
            runtime.clock[0]=datetime.fromisoformat(before['plan_wait']['deadline'])
            result_start=len(evidence['result_packets']);exact_start=len(evidence['exact_reads'])
            assert scheduler.run_due()
            first=capture('first_paused_legacy_C_check')
            assert first['active_plan'] is None and first['last_round']['actions']==[]
            wait_interval(first)
            assert first['slots']['occupied']==3 and Decimal(first['funds']['buy_reserved_usd'])==Decimal('23.60')
            assert account.cancels==account.posts==[] and store.lp_actions(sid)==[audit_c]
            if result_fault[0]:
                held=store.lp_session(sid)
                for key in ('yield_rotation_cancel_owners','yield_rotation_protection_before','entry_cancel_requested','augment_cancel_requested'):
                    assert held.get(key)==original_owner.get(key), key
                bad=[r for r in evidence['result_packets'][result_start:] if 'rank-c' in r['request'].get('owned_order_ids',())]
                assert bad, 'Bad original C proof must genuinely be requested before any valid retirement'
                if mode=='legacy_incomplete':
                    assert any(r['stage']==stage[0] for r in evidence['failed_ends'])
                    assert any(r['packet']['open_orders_complete'] is False and r['packet']['pagination_complete'] is False for r in bad)
                else:
                    actual=[r for r in evidence['exact_reads'][exact_start:] if r['order_id']=='rank-c' and r['fault']]
                    assert actual and actual[0]['value']['maker_address']=='0x'+'b'*40
                    assert actual[0]['value']['owner']=='d55eb563-8b8f-4a49-a0f5-343c99f274ba'
                    assert any(r['packet'].get('order_read_errors',{}).get('rank-c')=='order_response_invalid'
                        and r['packet']['open_orders_complete'] is False for r in bad)
                for elapsed in (1,59):no_replay(first,[audit_c],[],elapsed)
                result_fault[0]=False;stage[0]='next_paused_legacy_C_valid_proof'
                runtime.clock[0]=datetime.fromisoformat(first['plan_wait']['deadline'])
                result_start=len(evidence['result_packets'])
                assert scheduler.run_due();first=capture('valid_paused_legacy_C_retired_after_fault')
            paused_shape(first);assert_c_proof(result_start)
            retired=store.lp_session(sid)
            assert not retired.get('yield_rotation_cancel_owners') and not retired.get('yield_rotation_protection_before')
            assert not retired.get('entry_cancel_requested') and retired.get('augment_cancel_requested',[])==[]
            assert store.lp_actions(sid)==[audit_c] and account.cancels==account.posts==[]
        baseline=deepcopy(store.lp_session(sid)['queue_protection'])
        assert Decimal(baseline['levels']['0.40']['baseline_front'])==Decimal('980')
        ready=engine.lp_auto_state()
        assert ready['slots']['occupied']==3 and Decimal(ready['funds']['buy_reserved_usd'])==Decimal('23.60')
        stage[0]='new_current_D_refused_round'
        engine.lp_auto_set_desired_running(True)
        runtime.clock[0]=datetime.fromisoformat(ready['plan_wait']['deadline'])
        assert scheduler.run_due()
        waiting=capture('new_D_locally_refused')
        original_d=deepcopy(waiting['last_round'])
        assert waiting['active_plan'] is None and original_d['completed_at']
        assert original_d['round_id']!=original_c['round_id']
        assert [a['order_id'] for a in original_d['actions']]==['rank-d']
        assert [r['order_id'] for r in original_d['targets']]==['rank-a','rank-b']
        assert original_d['actions'][0]['state']=='rejected' and original_d['actions'][0]['reason']=='venue_denied'
        audit_d,=[deepcopy(a) for a in store.lp_actions(sid) if a.get('order_id')=='rank-d']
        assert audit_d['state']=='rejected' and audit_d['reason']=='venue_denied'
        assert audit_d['rotation_round_id']==original_d['round_id']
        assert audit_d['action_id']!=audit_c['action_id'] and audit_d['action_key']!=audit_c['action_key']
        assert next(a for a in store.lp_actions(sid) if a['action_key']==audit_c['action_key'])==audit_c
        wait_interval(waiting,dispatched=True)
        assert waiting['slots']['occupied']==3 and Decimal(waiting['funds']['buy_reserved_usd'])==Decimal('23.60')
        first_calls=list(account.cancels)
        assert first_calls==([('rank-d',)] if legacy else [('rank-c',),('rank-d',)]) and account.posts==[]
        evidence['pre_D_baseline']=baseline
        engine.lp_auto_set_desired_running(False)
        low_control=deepcopy(store.lp_session(sid))
        if mode=='legacy_live':
            stage[0]='public_current_C_LIVE_precedence'
            account.orders=(*account.orders,original_ids['rank-c'].model_copy(update={'status':'LIVE'}))
            adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
            assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
            low_control=deepcopy(store.lp_session(sid))
            assert {o.id for o in account.orders}=={'rank-a','rank-b','rank-c','rank-d'}
            current=capture('actual_current_C_LIVE_control')
            assert current['slots']['occupied']==4 and Decimal(current['funds']['buy_reserved_usd'])==Decimal('31.40')
        stage[0]='paused_current_D_denied_cleanup'
        runtime.clock[0]=datetime.fromisoformat(waiting['plan_wait']['deadline'])
        result_start=len(evidence['result_packets'])
        assert scheduler.run_due()
        state=capture('paused_D_owner_baseline_restored')
        paused_shape(state);assert state['last_round']['round_id'] not in (original_c['round_id'],original_d['round_id'])
        assert state['slots']['occupied']==(4 if mode=='legacy_live' else 3)
        assert Decimal(state['funds']['buy_reserved_usd'])==Decimal('31.40' if mode=='legacy_live' else '23.60')
        d_packets=[r for r in evidence['result_packets'][result_start:] if 'rank-d' in r['request'].get('owned_order_ids',())]
        assert d_packets and any(r['packet']['authenticated'] is True and r['packet']['open_orders_complete'] is True
            and r['packet']['pagination_complete'] is True and any(o.get('order_id')=='rank-d' and o['status']=='LIVE'
                and o['fill_quantity_known'] is True and Decimal(o['size_matched'])==0 and Decimal(o['price'])==Decimal('.40')
                and Decimal(o['original_size'])==20 for o in r['packet']['orders']) for r in d_packets)
        restored=store.lp_session(sid)
        assert restored.get('yield_rotation_cancel_owners')=={} and restored.get('yield_rotation_protection_before')=={}
        assert not restored.get('entry_cancel_requested') and restored.get('augment_cancel_requested',[])==[]
        assert restored['queue_protection']['levels']['0.40']==baseline['levels']['0.40']
        assert restored['queue_protection']['levels']['0.39']==low_control['queue_protection']['levels']['0.39']
        assert {k:v for k,v in restored['queue_protection'].items() if k!='levels'}=={
            k:v for k,v in low_control['queue_protection'].items() if k!='levels'}
        for flag in ('stop_requested','stop_loss_latched','owned_cancel_requested'):
            assert restored.get(flag)==low_control.get(flag), flag
        assert store.lp_actions(sid)==[audit_c,audit_d] and account.cancels==first_calls and account.posts==[]
        if mode=='legacy_live':
            assert any(o['order_id']=='rank-c' and o['status']=='LIVE' and Decimal(o['price'])==Decimal('.39')
                and Decimal(o['original_size'])==20 and Decimal(o['size_matched'])==0
                for r in evidence['full_packets'] if r['stage']=='public_current_C_LIVE_precedence' for o in r['packet']['open_orders'])
            assert restored['order_history']['rank-c']['order_id']=='rank-c'
        no_replay(state,[audit_c,audit_d],first_calls,interval-1)
        stage[0]='full_cold_before_current_deadline'
        _,restarted_adapter,restarted_account,_,restarted=runtime(orders=account.orders,public_client=public)
        cold_result,cold_full=[],[]
        read=restarted_adapter.lp_order_result_snapshot
        def observed_cold(request):
            value=read(request);cold_result.append(deepcopy(value));return value
        monkeypatch.setattr(restarted_adapter,'lp_order_result_snapshot',observed_cold)
        read_full=restarted_adapter.lp_account_snapshot_shared
        def observed_cold_full(**kwargs):
            value=read_full(**kwargs);cold_full.append(deepcopy(value));return value
        monkeypatch.setattr(restarted_adapter,'lp_account_snapshot_shared',observed_cold_full)
        restarted.lp_auto_run_once()
        evidence['cold']=dict(state=restarted.lp_auto_state(),result=cold_result,full=cold_full,
            cancels=list(restarted_account.cancels),posts=deepcopy(restarted_account.posts))
        assert cold_result==cold_full==[] and restarted_account.cancels==restarted_account.posts==[]
        assert store.lp_actions(sid)==[audit_c,audit_d]
        assert not any(r['active_plan'] and not r['monitor'] for r in evidence['financial_reads'])
        assert not any(r['active_plan'] and not r['monitor'] for r in evidence['market_reads'])
        if mode=='legacy_live':
            evidence['stage']='complete';return
        assert restored['queue_protection']==baseline
        stage[0]='ordinary_public_D_pressure_3000'
        protective[0]=True;deadline=monotonic()+5;monitor_phase[0]=True
        try:
            engine.lp_tick()
            assert protective_book.wait(max(0,deadline-monotonic())), 'Actual SDK protective book watchdog'
            while account.cancels==first_calls:
                assert monotonic()<deadline,'Public ordinary protection watchdog'
                pause.wait(min(.01,max(0,deadline-monotonic())));engine.lp_tick()
        finally:
            monitor_phase[0]=False
        assert account.cancels==first_calls+[('rank-d',)] and account.posts==[]
        bucket=store.lp_session(sid)['queue_protection']['levels']['0.40']
        assert bucket['cancel_reason']=='queue_ahead_ratio' and bucket['cancel_targets']==['rank-d']
        for original in (audit_c,audit_d):
            assert next(a for a in store.lp_actions(sid) if a['action_key']==original['action_key'])==original
        assert not any(r['active_plan'] and not r['monitor'] for r in evidence['financial_reads'])
        evidence['stage']='complete'
    finally:
        evidence.update(reached_stage=stage[0],clock=runtime.clock[0],state=engine.lp_auto_state(),document=document(),
            session=store.lp_session(sid),audits=store.lp_actions(sid),orders=[o.model_dump(mode='json') for o in account.orders],
            cancels=list(account.cancels),posts=deepcopy(account.posts))
        (tmp_path/'O3-diagnostics.json').write_text(json.dumps(evidence,default=str,indent=2)+'\n')


@pytest.mark.parametrize('case', ['current_protection', 'current_stop', 'healthy'])
def test_cancel_restoration_preserves_concurrent_public_control(runtime, monkeypatch, tmp_path, case):
    import json
    import hashlib
    from pathlib import Path
    import sqlite3
    from threading import Thread, get_ident
    from time import monotonic
    from polymarket.models.clob.cancel import CancelOrdersResponse
    from polymarket.models.clob.order_book import OrderBookLevel
    from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic
    from tests.test_lp_order_registration_contract import _open_order

    public = _CandidateSDKPublic(runtime.clock[0])
    book = public.get_order_book
    protective, normal_book, normal_seen = [False], [False], Event()
    protective_seen, result_complete, observed, control_done = Event(), Event(), Event(), Event()
    scheduler_threads, control_threads, monitor_threads = [], [], set()
    explicit_control, explicit_monitor = [False], [False]
    rows_seen, events, errors, control_errors, control_capture = [], [], [], [], {}
    result_packets, current_states = [], []
    current_phase = ['planned_auto']
    deadline = [None]
    def current_book(*, token_id):
        value = book(token_id=token_id).model_copy(update={'timestamp':runtime.clock[0], 'bids':(
            OrderBookLevel(price=Decimal('.40'), size=Decimal('1000')),
            OrderBookLevel(price=Decimal('.39'), size=Decimal('3000' if protective[0] else '1000')))})
        if protective[0]:
            protective_seen.set()
        if normal_book[0]:
            normal_seen.set()
        return value
    monkeypatch.setattr(public, 'get_order_book', current_book)
    originals = (_open_order('rank-a', 'BUY', price='.40', original='20'),
        _open_order('rank-b', 'BUY', price='.40', original='20'),
        _open_order('rank-c', 'BUY', price='.39', original='20'))
    store, adapter, account, lp, engine = runtime(orders=originals, public_client=public)
    engine.lp_auto_configure(dict(budget_usd='39', target_buy_count=1, buy_price_level=2))
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    session, = store.lp_active_sessions()
    sid = session['session_id']
    baseline = deepcopy(session['queue_protection'])
    assert baseline['levels']['0.40']['order_id'] == 'rank-a'
    assert baseline['levels']['0.39']['order_id'] == 'rank-c'
    assert Decimal(baseline['levels']['0.39']['baseline_front']) == 980
    finances = []
    balance = account.get_balance_allowance
    def observed_balance(**kwargs):
        active = bool(engine.lp_auto_state()['active_plan'])
        phase = 'explicit_control' if explicit_control[0] else (
            'explicit_monitor' if explicit_monitor[0] else current_phase[0])
        finances.append(dict(active_plan=active, phase=phase, as_of=runtime.clock[0].isoformat(), request=deepcopy(kwargs)))
        return balance(**kwargs)
    monkeypatch.setattr(account, 'get_balance_allowance', observed_balance)
    def venue(*, order_ids):
        account.cancels.append(order_ids)
        runtime.clock[0] += timedelta(seconds=1)
        if explicit_control[0]:
            events.append(dict(event='real_control_ack', ids=order_ids, at=monotonic()))
            return CancelOrdersResponse(canceled=order_ids, not_canceled={})
        assert order_ids in (('rank-a',), ('rank-b',))
        return CancelOrdersResponse(canceled=(), not_canceled={oid:'venue_denied' for oid in order_ids})
    monkeypatch.setattr(account, 'cancel_orders', venue)
    monkeypatch.setattr(account, 'get_order', lambda *,order_id:next(o for o in originals if o.id==order_id), raising=False)
    engine.lp_auto_set_desired_running(True)
    scheduler = LPAutoScheduler(engine, clock=lambda:runtime.clock[0])
    assert scheduler.run_due()
    sent = engine.lp_auto_state()
    plan = deepcopy(sent['last_round'])
    assert sent['active_plan'] is None and plan['completed_at']
    assert [(a['state'], a['reason']) for a in plan['actions']] == [('rejected', 'venue_denied')] * 2
    assert sent['slots']['occupied'] == 3 and Decimal(sent['funds']['buy_reserved_usd']) == Decimal('23.80')
    assert datetime.fromisoformat(sent['plan_wait']['deadline']) == datetime.fromisoformat(plan['completed_at']) + timedelta(seconds=30)
    assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in plan['targets'])
    with sqlite3.connect(store.path) as connection:
        original_document = json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    original_rotations = {oid: deepcopy(original_document['account_rotations'][oid]) for oid in ('rank-a', 'rank-b')}
    assert all(rotation['session_id'] == sid and rotation['rotation_round_id'] == plan['round_id']
        and Decimal(rotation['price']) == Decimal('.40') and Decimal(rotation['original_quantity']) == 20
        for rotation in original_rotations.values())
    assert [a['order_id'] for a in plan['actions']] == ['rank-a','rank-b']
    assert [r['order_id'] for r in plan['targets']] == ['rank-c']
    assert all(a['session_id']==sid for a in plan['actions'])
    original_audits = deepcopy(store.lp_actions(sid))
    assert len(original_audits)==2
    assert all(a['state']=='rejected' and a['reason']=='venue_denied'
        and a['rotation_round_id']==plan['round_id'] for a in original_audits)
    assert account.cancels==[('rank-a',),('rank-b',)] and account.posts==[]
    assert not any(r['active_plan'] and r['phase']=='planned_auto' for r in finances)
    owned = store.lp_session(sid)
    assert owned['yield_rotation_cancel_owners'] == {'rank-a': plan['round_id'], 'rank-b': plan['round_id']}
    assert set(owned['yield_rotation_protection_before']) == {'rank-a', 'rank-b'}
    assert all(owned['yield_rotation_protection_before'][oid] == baseline for oid in ('rank-a', 'rank-b'))
    engine.lp_auto_set_desired_running(False)
    assert engine.lp_auto_state()['desired_running'] is False
    current_phase[0] = 'current_cleanup'
    runtime.clock[0] = datetime.fromisoformat(sent['plan_wait']['deadline'])
    result_reader = adapter.lp_order_result_snapshot
    def observed_result(request):
        packet = result_reader(request)
        result_packets.append(deepcopy(dict(request=request, packet=packet, thread=get_ident(), phase=current_phase[0])))
        if scheduler_threads and request.get('owned_order_ids') == ['rank-a', 'rank-b']:
            assert packet['authenticated'] is True
            assert hashlib.sha256(str(packet['wallet_address']).strip().casefold().encode()).hexdigest() == original_document['account_id']
            assert all(packet[key] is True for key in ('open_orders_complete', 'positions_complete',
                'trades_complete', 'pagination_complete'))
            for oid in ('rank-a', 'rank-b'):
                row, = [row for row in packet['orders'] if row['order_id'] == oid]
                assert row['token_id'] == owned['token_id'] and row['side'] == 'BUY' and row['status'] == 'LIVE'
                assert Decimal(row['price']) == Decimal('.40') and Decimal(row['original_size']) == 20
                assert row['fill_quantity_known'] is True and Decimal(row['size_matched']) == 0
            events.append(dict(event='original_sdk_result_complete', thread=get_ident(), at=monotonic()))
            result_complete.set()
        return packet
    monkeypatch.setattr(adapter, 'lp_order_result_snapshot', observed_result)

    def control():
        control_threads.append(get_ident())
        explicit_control[0] = True
        events.append(dict(event='control_started', at=monotonic()))
        try:
            runtime.clock[0] += timedelta(seconds=1)
            if case.endswith('_stop'):
                control_capture['result'] = lp.stop(sid)
            else:
                protective[0] = True
                pause = Event()
                while True:
                    assert monotonic()<deadline[0], 'Real public control watchdog'
                    engine.lp_tick()
                    current = store.lp_session(sid)
                    bucket = current['queue_protection']['levels']['0.39']
                    actual_protection = [a for a in store.lp_actions(sid) if a.get('role') == 'entry-protection-cancel'
                        and a.get('targets') == ['rank-c'] and a['state'] == 'accepted']
                    if (bucket.get('state') == 'canceling' and bucket.get('cancel_reason') == 'queue_ahead_ratio'
                            and bucket.get('cancel_targets') == ['rank-c'] and 'rank-c' in current.get('augment_cancel_requested', [])
                            and actual_protection):
                        break
                    pause.wait(min(.01,max(0,deadline[0]-monotonic())))
                assert protective_seen.is_set()
            control_capture['session'] = deepcopy(store.lp_session(sid))
            control_capture['audits'] = deepcopy(store.lp_actions(sid))
            control_capture['funds'] = deepcopy(engine.lp_auto_state()['funds'])
            control_capture['calls'] = list(account.cancels)
            events.append(dict(event='control_committed', at=monotonic()))
        except BaseException as exc:
            control_errors.append(repr(exc))
        finally:
            explicit_control[0] = False
            control_done.set()
    control_jobs = []
    connect = sqlite3.connect
    class ObservedCursor(sqlite3.Cursor):
        def execute(self, sql, parameters=(), /):
            self.observed_sql, self.observed_parameters = sql, parameters
            result = super().execute(sql, parameters)
            compact = sql.strip().upper()
            if compact in ('BEGIN', 'BEGIN IMMEDIATE'):
                self.connection.observed_transaction = compact
                self.connection.observed_dml = False
                events.append(dict(event='native_begin_succeeded', transaction=compact,
                    before_dml=not getattr(self.connection, 'observed_dml', False), in_transaction=self.connection.in_transaction,
                thread=get_ident(), connection=id(self.connection), at=monotonic()))
            elif compact.startswith(('UPDATE ', 'INSERT ', 'DELETE ', 'REPLACE ')):
                self.connection.observed_dml = True
            elif compact in ('COMMIT','ROLLBACK'):
                events.append(dict(event='native_transaction_finished', outcome=compact,
                    thread=get_ident(), connection=id(self.connection), at=monotonic()))
                self.connection.observed_transaction = None
            return result
        def fetchone(self):
            row = super().fetchone()
            if (row is None or not isinstance(row,sqlite3.Row) or observed.is_set()
                    or not scheduler_threads or get_ident()!=scheduler_threads[0] or not result_complete.is_set()
                    or not {'session_id','payload','idempotency_key','state'} <= set(row.keys())
                    or str(row['session_id'])!=sid):
                return row
            payload = json.loads(row['payload'])
            owners = payload.get('yield_rotation_cancel_owners') or {}
            saved = payload.get('yield_rotation_protection_before') or {}
            transaction = getattr(self.connection,'observed_transaction',None)
            writer = self.connection.in_transaction and transaction=='BEGIN IMMEDIATE'
            record = dict(sql=self.observed_sql, parameters=self.observed_parameters,
                row=dict(row), row_identity=id(row), writer=writer, transaction=transaction,
                before_dml=not getattr(self.connection, 'observed_dml', False),
                in_transaction=self.connection.in_transaction,
                thread=get_ident(), connection=id(self.connection), at=monotonic())
            rows_seen.append(record)
            events.append(dict(event='actual_row_fetched', writer=writer, at=record['at']))
            observed.set()
            # The FIRST exact original Row is the obligation, even if its
            # writer or baseline premise is wrong. Never skip to a good image.
            assert writer and transaction == 'BEGIN IMMEDIATE' and record['before_dml'], record
            assert owners == {'rank-a': plan['round_id'], 'rank-b': plan['round_id']}
            assert set(saved) == {'rank-a', 'rank-b'} and all(saved[oid] == baseline for oid in saved)
            assert not payload.get('stop_requested') and 'rank-c' not in payload.get('augment_cancel_requested', [])
            if case!='healthy':
                job = Thread(target=control)
                control_jobs.append(job)
                events.append(dict(event='control_launched', at=monotonic()))
                job.start()
            record['returned_row_identity'] = id(row)
            record['delivered_at'] = monotonic()
            events.append(dict(event='actual_row_delivered', writer=writer, at=record['delivered_at']))
            return row
    class ObservedConnection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            return self.cursor(factory=ObservedCursor).execute(*args, **kwargs)
    def observed_connect(database, *args, **kwargs):
        if Path(database).resolve()==store.path.resolve():
            kwargs['factory'] = ObservedConnection
        return connect(database,*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',observed_connect)
    outcomes = []
    def run_result():
        scheduler_threads.append(get_ident())
        try:
            outcomes.append(scheduler.run_due())
        except BaseException as exc:
            errors.append(repr(exc))
    deadline[0] = monotonic()+5
    sender = Thread(target=run_result)
    sender.start()
    try:
        assert observed.wait(max(0,deadline[0]-monotonic())), 'Semantic real SQLite row watchdog'
        sender.join(max(0,deadline[0]-monotonic()))
        assert not sender.is_alive(), 'Original scheduler publication watchdog'
        for job in control_jobs:
            job.join(max(0,deadline[0]-monotonic()))
            assert not job.is_alive(), 'Public control completion watchdog'
        assert not errors and not control_errors, (errors,control_errors,rows_seen)
        assert outcomes==[True] and len(rows_seen)==1
        row = rows_seen[0]
        assert row['row_identity']==row['returned_row_identity']
        assert row['thread']==scheduler_threads[0]
        assert row['writer'] and row['in_transaction'] and row['before_dml']
        assert row['transaction'] == 'BEGIN IMMEDIATE'
        assert any(e['event'] == 'native_begin_succeeded' and e['transaction'] == 'BEGIN IMMEDIATE'
            and e['connection'] == row['connection'] and e['at'] < row['at'] for e in events)
        commit = next(e for e in events if e['event'] == 'native_transaction_finished'
            and e['outcome'] == 'COMMIT' and e['connection'] == row['connection'] and e['at'] > row['at'])
        assert row['delivered_at'] < commit['at']
        if case != 'healthy':
            launched = next(e for e in events if e['event'] == 'control_launched')
            assert row['at'] <= launched['at'] < row['delivered_at']
            assert commit['at'] < next(e['at'] for e in events if e['event'] == 'control_committed')
        state = engine.lp_auto_state()
        current_states.append(deepcopy(state))
        assert state['active_plan'] is None and state['desired_running'] is False
        assert state['plan_wait']['kind'] == 'round'
        check_started = datetime.fromisoformat(state['plan_wait']['started_at'])
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == check_started + timedelta(seconds=30)
        with connect(store.path) as connection:
            current_document = json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
        assert current_document['rounds'][plan['round_id']] == original_document['rounds'][plan['round_id']]
        for oid, original_rotation in original_rotations.items():
            rotation = current_document['account_rotations'][oid]
            assert rotation['state'] == 'active' and rotation['rotation_settled_at']
            assert all(rotation[k] == original_rotation[k] for k in ('rotation_round_id', 'rotation_requested_at',
                'session_id', 'order_id', 'token_id', 'condition_id', 'price', 'quantity', 'original_quantity'))
        assert state['slots']['occupied']==3 and Decimal(state['funds']['buy_reserved_usd'])==Decimal('23.80')
        assert {o.id for o in account.orders}=={'rank-a','rank-b','rank-c'} and account.posts==[]
        final = store.lp_session(sid)
        assert not final.get('yield_rotation_cancel_owners') and not final.get('yield_rotation_protection_before')
        for old in original_audits:
            assert next(a for a in store.lp_actions(sid) if a['action_key']==old['action_key'])==old
        if case.endswith('_protection'):
            assert account.cancels==[('rank-a',),('rank-b',),('rank-c',)]
            protected_audit, = [a for a in control_capture['audits'] if a.get('role')=='entry-protection-cancel']
            assert protected_audit['state']=='accepted' and protected_audit['targets']==['rank-c']
            bucket = final['queue_protection']['levels']['0.39']
            assert bucket['state']=='canceling' and bucket['cancel_reason']=='queue_ahead_ratio', (bucket,rows_seen,control_capture)
            assert bucket['cancel_targets']==['rank-c']
            assert 'rank-c' in final.get('augment_cancel_requested',[]), (final,rows_seen)
            assert final['queue_protection'] == control_capture['session']['queue_protection']
            for flag in ('entry_cancel_requested', 'augment_cancel_requested', 'stop_requested', 'stop_loss_latched'):
                assert final.get(flag) == control_capture['session'].get(flag)
            assert next(a for a in store.lp_actions(sid) if a['action_key']==protected_audit['action_key'])==protected_audit
            assert any(r['phase']=='explicit_control' and r['request'] for r in finances)
        elif case.endswith('_stop'):
            assert final['stop_requested'] is True
            assert final['state']==control_capture['session']['state']
            assert final['queue_protection'] == control_capture['session']['queue_protection']
            for flag in ('entry_cancel_requested', 'augment_cancel_requested', 'stop_requested', 'stop_loss_latched'):
                assert final.get(flag) == control_capture['session'].get(flag)
            stop_audits = [a for a in control_capture['audits'] if a.get('source')=='owned_cancel_sweep']
            assert stop_audits and any(a['order_id']=='rank-c' for a in stop_audits)
            for audit in stop_audits:
                assert audit['state']=='accepted'
                assert next(a for a in store.lp_actions(sid) if a['action_key']==audit['action_key'])==audit
                if audit['order_id']=='rank-a':
                    assert final['entry_cancel_requested'] is True
                else:
                    assert audit['order_id'] in final.get('augment_cancel_requested',[])
            assert all(ids in (('rank-a',),('rank-b',),('rank-c',)) for ids in account.cancels[2:])
            assert account.cancels[2:].count(('rank-c',))==1
            assert account.cancels[2:].count(('rank-a',))<=1 and account.cancels[2:].count(('rank-b',))<=1
        else:
            assert final['yield_rotation_cancel_owners']=={}
            assert final['yield_rotation_protection_before']=={}
            assert not final.get('entry_cancel_requested') and final.get('augment_cancel_requested',[])==[]
            assert final['queue_protection']==baseline
            assert store.lp_actions(sid)==original_audits and account.cancels==[('rank-a',),('rank-b',)]
        assert not any(r['active_plan'] and r['phase']=='planned_auto' for r in finances)
        calls, audits = list(account.cancels), deepcopy(store.lp_actions(sid))
        protective[0] = False
        normal_book[0] = True
        monitor_threads.add(get_ident())
        explicit_monitor[0] = True
        try:
            normal_deadline, pause = monotonic()+5, Event()
            engine.lp_tick()
            assert normal_seen.wait(max(0,normal_deadline-monotonic())), 'Actual normal SDK book watchdog'
            while True:
                engine.lp_tick()
                current = store.lp_session(sid)
                if case.endswith('_protection'):
                    assert current['queue_protection']['levels']['0.39']['state']=='canceling'
                    assert 'rank-c' in current.get('augment_cancel_requested',[])
                    break
                if case.endswith('_stop'):
                    assert current['stop_requested'] is True
                    break
                if current['queue_protection']['levels']['0.39']['state']=='monitoring':
                    break
                assert monotonic()<normal_deadline, 'Public normal monitoring watchdog'
                pause.wait(min(.01,max(0,normal_deadline-monotonic())))
        finally:
            monitor_threads.discard(get_ident())
            explicit_monitor[0] = False
        before_reads = len(finances), len(result_packets)
        scheduler.request_check()
        assert not scheduler.run_due()
        engine.lp_auto_run_once()
        assert (len(finances), len(result_packets)) == before_reads
        assert account.cancels==calls and account.posts==[]
        assert store.lp_actions(sid)==audits
        _, restarted_adapter, restarted_account, _, restarted = runtime(orders=account.orders,public_client=public)
        restarted_balance = restarted_account.get_balance_allowance
        def current_balance(**kwargs):
            finances.append(dict(active_plan=False, phase='cold_before_deadline', as_of=runtime.clock[0].isoformat(), request=deepcopy(kwargs)))
            return restarted_balance(**kwargs)
        restarted_result = restarted_adapter.lp_order_result_snapshot
        def current_result(request):
            packet = restarted_result(request)
            result_packets.append(deepcopy(dict(request=request, packet=packet, phase='cold_before_deadline')))
            return packet
        monkeypatch.setattr(restarted_account, 'get_balance_allowance', current_balance)
        monkeypatch.setattr(restarted_adapter, 'lp_order_result_snapshot', current_result)
        restarted.lp_auto_run_once()
        assert (len(finances), len(result_packets)) == before_reads
        assert restarted.lp_auto_state()['desired_running'] is False
        assert store.lp_actions(sid) == audits
        assert restarted_account.cancels==restarted_account.posts==[]
        restarted_session = store.lp_session(sid)
        if case.endswith('_protection'):
            assert restarted_session['queue_protection']['levels']['0.39']['state']=='canceling'
            assert 'rank-c' in restarted_session.get('augment_cancel_requested',[])
        elif case.endswith('_stop'):
            assert restarted_session['stop_requested'] is True
        assert not any(r['active_plan'] and r['phase']=='planned_auto' for r in finances)
    finally:
        control_done.set()
        sender.join(5)
        for job in control_jobs:
            job.join(5)
        (tmp_path/'publication-observer.json').write_text(json.dumps(dict(case=case,rows=rows_seen,events=events,
            errors=errors,control_errors=control_errors,control=control_capture,finances=finances,
            actual_calls=account.cancels,result_packets=result_packets,current_states=current_states,
            original_round=plan,original_rotations=original_rotations,original_audits=original_audits,baseline=baseline,
            final_session=store.lp_session(sid),final_audits=store.lp_actions(sid),
            sender_alive=sender.is_alive(),control_alive=[j.is_alive() for j in control_jobs]),indent=2,default=str)+'\n')


@pytest.mark.parametrize('case', ['terminal_same_price','unrelated_live_then_terminal'])
def test_completed_ordinary_cancel_restores_later_yield_protection(runtime, monkeypatch, tmp_path, case):
    import json
    from time import monotonic
    from polymarket.models.clob.cancel import CancelOrdersResponse
    from polymarket.models.clob.order_book import OrderBookLevel
    from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic
    from tests.test_lp_order_registration_contract import _open_order

    engine = None
    healthy_book, pressure_book = Event(), Event()
    public = _CandidateSDKPublic(runtime.clock[0])
    source_book = public.get_order_book
    pressure, pressure_seen, phase = [False], Event(), ['setup']
    books, finances, exact_reads, result_rows, observations = [], [], [], [], []
    stage, diagnostics = ['import'], {}
    old_price = '.40' if case=='terminal_same_price' else '.39'
    original = {oid:_open_order(oid, 'BUY', price=price, original='20')
        for oid,price in [('rank-a','.39'),('rank-b','.39'),('rank-d','.40')]}
    def book(*, token_id):
        value = source_book(token_id=token_id).model_copy(update={
            'timestamp':runtime.clock[0], 'bids':(
                OrderBookLevel(price=Decimal('.40'),size=Decimal('3000' if pressure[0] else '1000')),
                OrderBookLevel(price=Decimal('.39'),size=Decimal('1000')))})
        books.append(dict(token_id=token_id,timestamp=value.timestamp,pressure=pressure[0],phase=phase[0],
            active_plan=bool(engine and engine.lp_auto_state()['active_plan']),value=value.model_dump(mode='json')))
        (pressure_book if pressure[0] else healthy_book).set()
        if pressure[0]:
            pressure_seen.set()
        return value
    monkeypatch.setattr(public,'get_order_book',book)
    store, adapter, account, lp, engine = runtime(orders=tuple(original.values()), public_client=public)
    terminal = {}
    balance, open_orders = account.get_balance_allowance, account.list_open_orders
    def observed_balance(**kwargs):
        finances.append(dict(phase=phase[0], active_plan=bool(engine.lp_auto_state()['active_plan']),
            at=runtime.clock[0].isoformat(), request=deepcopy(kwargs)))
        return balance(**kwargs)
    def observed_open_orders(**kwargs):
        values = open_orders(**kwargs)
        result_rows.append(dict(phase=phase[0], at=runtime.clock[0].isoformat(),
            orders=[dict(id=o.id,status=o.status,price=str(o.price))for o in values]))
        return values
    def exact(*, order_id):
        exact_reads.append(dict(phase=phase[0],order_id=order_id))
        return terminal.get(order_id, original[order_id])
    def cancel(*, order_ids):
        account.cancels.append(order_ids)
        runtime.clock[0] += timedelta(seconds=1)
        if order_ids==('old-manual',):
            terminal['old-manual'] = original['old-manual'].model_copy(update={'status':'CANCELED'})
            account.orders = tuple(o for o in account.orders if o.id!='old-manual')
            return CancelOrdersResponse(canceled=order_ids,not_canceled={})
        assert order_ids==('rank-d',), (stage[0],order_ids)
        if pressure[0]:
            return CancelOrdersResponse(canceled=order_ids,not_canceled={})
        return CancelOrdersResponse(canceled=(),not_canceled={'rank-d':'venue_denied'})
    monkeypatch.setattr(account,'get_balance_allowance',observed_balance)
    monkeypatch.setattr(account,'list_open_orders',observed_open_orders)
    monkeypatch.setattr(account,'get_order',exact,raising=False)
    monkeypatch.setattr(account,'cancel_orders',cancel)
    import sqlite3
    result_packets, full_packets, state_records = [], [], []
    result_reader = adapter.lp_order_result_snapshot
    def observed_results(request):
        packet = result_reader(request)
        result_packets.append(deepcopy(dict(stage=stage[0],phase=phase[0],request=request,packet=packet)))
        return packet
    monkeypatch.setattr(adapter,'lp_order_result_snapshot',observed_results)
    full_reader = adapter.lp_account_snapshot_shared
    def observed_full(**kwargs):
        packet = full_reader(**kwargs)
        full_packets.append(deepcopy(dict(stage=stage[0],phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),packet=packet)))
        return packet
    monkeypatch.setattr(adapter,'lp_account_snapshot_shared',observed_full)
    def document():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state=deepcopy(engine.lp_auto_state())
        state_records.append(dict(label=label,state=state,session=store.lp_session(sid),document=document(),
            audits=store.lp_actions(sid),orders=[o.model_dump(mode='json') for o in account.orders],
            cancels=list(account.cancels),posts=deepcopy(account.posts)))
        return state
    def paused_shape(state):
        assert state['desired_running'] is False and state['active_plan'] is None
        assert state['last_round']['actions']==[] and state['last_round']['checked_at']
        assert state['plan_wait']['kind']=='round'
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(state['plan_wait']['started_at'])+timedelta(seconds=30)
    def dispatched(state):
        assert state['active_plan'] is None and state['last_round']['completed_at']
        assert state['plan_wait']['kind']=='round'
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(state['last_round']['completed_at'])+timedelta(seconds=30)
    def no_core_io():
        counts=(len(finances),len(books),len(result_packets),len(full_packets))
        last=deepcopy(engine.lp_auto_state()['last_round']);wait=deepcopy(engine.lp_auto_state()['plan_wait'])
        scheduler.request_check();scheduler.run_due();engine.lp_auto_run_once()
        assert (len(finances),len(books),len(result_packets),len(full_packets))==counts
        assert engine.lp_auto_state()['last_round']==last and engine.lp_auto_state()['plan_wait']==wait
    pause = Event()
    sid = None
    def monitor_until(predicate, label):
        phase[0] = 'explicit_monitor'
        deadline = monotonic()+5
        healthy_book.clear();pressure_book.clear()
        while True:
            assert monotonic()<deadline, label
            value = lp.reconcile_facts(sid,monitor=True)
            observed = dict(stage=stage[0],error=value[3],status=deepcopy(value[4]),
                book=deepcopy(value[1].get('book')) if value[1] else None)
            observations.append(observed)
            current = store.lp_session(sid)
            if ((pressure_book if pressure[0] else healthy_book).is_set() and predicate(current,observed)):
                return current,observed
            pause.wait(min(.01,max(0,deadline-monotonic())))
    try:
        engine.lp_auto_configure(dict(budget_usd='39',target_buy_count=2,buy_price_level=2))
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        session, = store.lp_active_sessions()
        sid = session['session_id']
        assert set(session['owned_order_ids'])==set(original)
        assert not engine.lp_auto_state()['desired_running']
        stage[0] = 'original_D_monitoring'
        session,_ = monitor_until(lambda s,o:s['queue_protection']['levels']['0.40']['state']=='monitoring'
            and s['queue_protection']['levels']['0.40']['order_id']=='rank-d', 'Original genuine D monitoring watchdog')
        original_d_baseline = deepcopy(session['queue_protection']['levels']['0.40'])
        assert Decimal(original_d_baseline['baseline_front'])==980
        stage[0],phase[0] = 'public_old_manual_import','setup'
        runtime.clock[0] += timedelta(seconds=1)
        original['old-manual'] = _open_order('old-manual','BUY',price=old_price,original='20')
        account.orders = (*account.orders,original['old-manual'])
        adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        assert store.lp_active_sessions()[0]['session_id']==sid
        assert set(store.lp_session(sid)['owned_order_ids'])==set(original)
        stage[0],phase[0] = 'public_manual_cancel','explicit_manual'
        manual_result = engine.lp_cancel_orders(dict(confirm=True,order_ids=['old-manual']))
        assert manual_result['canceled']==['old-manual'] and manual_result['not_canceled']=={}
        stage[0] = 'terminal_manual_publication'
        session,_ = monitor_until(lambda s,o:s['order_history']['old-manual']['status']=='CANCELED'
            and s['order_history']['old-manual']['size_matched'] in ('0',Decimal(0))
            and s['queue_protection']['levels']['0.39']['state']=='monitoring', 'Public original terminal history watchdog')
        manual_audit, = [deepcopy(a)for a in store.lp_actions(sid)if a.get('order_id')=='old-manual']
        assert manual_audit['state']=='accepted' and manual_audit['role']=='reconciliation_cancel'
        assert manual_audit['session_id']==sid and not manual_audit.get('rotation_round_id')
        assert any(a.get('context')=='dashboard_manual_cancel'for a in store.lp_actions('manual'))
        assert {o.id for o in account.orders}=={'rank-a','rank-b','rank-d'}
        stage[0] = 'public_D_monitoring'
        session,_ = monitor_until(lambda s,o:s['queue_protection']['levels']['0.40']['state']=='monitoring'
            and s['queue_protection']['levels']['0.40']['order_id']=='rank-d', 'Genuine D price-level monitoring watchdog')
        baseline = deepcopy(session['queue_protection'])
        assert Decimal(baseline['levels']['0.40']['baseline_front'])==980
        for key in ('order_id','baseline_front','baseline_price','baseline_source',
                    'baseline_source_timestamp','baseline_book_received_at','baseline_book_hash','baseline_version'):
            assert baseline['levels']['0.40'].get(key)==original_d_baseline.get(key), key
        assert session['order_history']['old-manual']['status']=='CANCELED'
        assert next(a for a in store.lp_actions(sid)if a['action_key']==manual_audit['action_key'])==manual_audit
        stage[0],phase[0] = 'original_D_approval','planned_auto'
        engine.lp_auto_set_desired_running(True)
        scheduler = LPAutoScheduler(engine,clock=lambda:runtime.clock[0])
        assert scheduler.run_due()
        sent = engine.lp_auto_state()
        plan = deepcopy(sent['last_round'])
        dispatched(sent);diagnostics['original_D_round']=plan
        assert [a['order_id']for a in plan['actions']]==['rank-d']
        assert [t['order_id']for t in plan['targets']]==['rank-a','rank-b']
        original_d=store.lp_session(sid)['order_history']['rank-d']
        assert original_d['order_id']=='rank-d' and original_d['side']=='BUY'
        assert Decimal(original_d['price'])==Decimal('.40') and Decimal(original_d.get('original_size',original_d.get('quantity')))==20
        assert 'rank-d' in store.lp_session(sid)['owned_order_ids']
        d_audit, = [deepcopy(a)for a in store.lp_actions(sid)if a.get('order_id')=='rank-d']
        assert d_audit['state']=='rejected' and d_audit['reason']=='venue_denied'
        assert d_audit['rotation_round_id']==plan['round_id']
        assert account.cancels==[('old-manual',),('rank-d',)] and account.posts==[]
        engine.lp_auto_set_desired_running(False)
        independent_image = deepcopy(store.lp_session(sid))
        if case=='unrelated_live_then_terminal':
            stage[0] = 'external_old_LIVE_after_approval'
            account.orders = (*account.orders,original['old-manual'])
            phase[0]='explicit_dashboard'
            adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
            assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
            independent_image=deepcopy(store.lp_session(sid))
        stage[0],phase[0] = 'original_D_result','current_cleanup'
        result_start = len(result_rows)
        runtime.clock[0] = datetime.fromisoformat(sent['plan_wait']['deadline'])
        result_at = runtime.clock[0]
        assert scheduler.run_due()
        state = capture('paused_original_D_cleanup')
        paused_shape(state)
        assert state['last_round']['round_id']!=plan['round_id']
        assert next(a for a in store.lp_actions(sid) if a['action_key']==d_audit['action_key'])==d_audit
        assert state['slots']['occupied']==(4 if case=='unrelated_live_then_terminal' else 3)
        assert Decimal(state['funds']['buy_reserved_usd'])==Decimal('31.40' if case=='unrelated_live_then_terminal' else '23.60')
        if case=='terminal_same_price':
            assert Decimal(state['funds']['buy_reserved_usd'])==Decimal('23.60')
        assert state['funds']['status']=='known' and datetime.fromisoformat(state['funds']['as_of'])>=result_at
        assert result_rows[result_start:] and not any(r['phase']=='planned_auto' and r['order_id']=='old-manual'for r in exact_reads)
        if case=='unrelated_live_then_terminal':
            assert any(o['id']=='old-manual' and o['status']=='LIVE'
                for r in result_rows[result_start:]for o in r['orders'])
        restored = store.lp_session(sid)
        assert restored['queue_protection']['levels']['0.40']==baseline['levels']['0.40'], (stage[0],restored,baseline)
        assert restored['queue_protection']['levels']['0.39']==independent_image['queue_protection']['levels']['0.39']
        assert not restored.get('yield_rotation_cancel_owners') and not restored.get('yield_rotation_protection_before')
        assert 'rank-d' not in restored.get('augment_cancel_requested',[]) and not restored.get('entry_cancel_requested')
        if case=='unrelated_live_then_terminal':
            assert restored.get('entry_cancel_requested')==independent_image.get('entry_cancel_requested')
            for key in ('augment_cancel_requested','owned_cancel_requested'):
                assert [oid for oid in restored.get(key,[]) if oid!='rank-d']==[
                    oid for oid in independent_image.get(key,[]) if oid!='rank-d'], key
        for old in (manual_audit,d_audit):
            assert next(a for a in store.lp_actions(sid)if a['action_key']==old['action_key'])==old
        calls,audits = list(account.cancels),deepcopy(store.lp_actions(sid))
        no_core_io()
        _,_,restart_account,_,restart_engine = runtime(orders=account.orders,public_client=public)
        restart_engine.lp_auto_run_once()
        assert restart_account.cancels==restart_account.posts==[]
        assert account.cancels==calls and account.posts==[] and store.lp_actions(sid)==audits
        if case=='unrelated_live_then_terminal':
            stage[0] = 'later_exact_terminal_publication'
            account.orders = tuple(o for o in account.orders if o.id!='old-manual')
            runtime.clock[0] += timedelta(seconds=1)
            session,_ = monitor_until(lambda s,o:s['order_history']['old-manual']['status']=='CANCELED'
                and s['queue_protection']['levels']['0.40']['state']=='monitoring', 'Later ordinary terminal completion watchdog')
            phase[0] = 'explicit_dashboard'
            adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
            assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
            later = engine.lp_auto_state()
            assert datetime.fromisoformat(later['funds']['as_of'])>result_at
            assert datetime.fromisoformat(later['funds']['as_of'])>datetime.fromisoformat(sent['funds']['as_of'])
            assert {o.id for o in account.orders}=={'rank-a','rank-b','rank-d'}
            assert later['slots']['occupied']==3 and Decimal(later['funds']['buy_reserved_usd'])==Decimal('23.60')
            for key in ('order_id','baseline_front','baseline_price','baseline_source',
                    'baseline_source_timestamp','baseline_book_received_at','baseline_book_hash','baseline_version'):
                assert session['queue_protection']['levels']['0.40'][key]==baseline['levels']['0.40'][key], key
            for old in (manual_audit,d_audit):
                assert next(a for a in store.lp_actions(sid)if a['action_key']==old['action_key'])==old
        stage[0] = 'distinct_public_D_protection'
        pressure[0] = True
        runtime.clock[0] += timedelta(seconds=1)
        pressure_at = runtime.clock[0]
        session,consumed = monitor_until(lambda s,o:bool(o['book'])
            and o['book']['token_id']==session['token_id'] and o['book']['timestamp']==pressure_at
            and s['queue_protection']['levels']['0.40']['state']=='canceling'
            and s['queue_protection']['levels']['0.40'].get('cancel_reason')=='queue_ahead_ratio', 'Actual consumed D protective book watchdog')
        assert pressure_seen.is_set() and consumed['status']['session_id']==sid
        assert session['queue_protection']['levels']['0.40']['cancel_targets']==['rank-d']
        assert Decimal(session['queue_protection']['levels']['0.40']['ratio'])==Decimal(980)/Decimal(3000)
        protection_audit, = [a for a in store.lp_actions(sid)if a.get('role')=='entry-protection-cancel']
        assert protection_audit['state']=='accepted' and protection_audit['targets']==['rank-d']
        assert protection_audit['action_key'] not in (manual_audit['action_key'],d_audit['action_key'])
        assert 'rank-d' in session.get('augment_cancel_requested',[])
        assert account.cancels==calls+[('rank-d',)] and account.posts==[]
        assert any(o.id=='rank-d' and o.status=='LIVE'for o in account.orders)
        for old in (manual_audit,d_audit):
            assert next(a for a in store.lp_actions(sid)if a['action_key']==old['action_key'])==old
        assert not any(r['active_plan'] and r['phase']=='planned_auto'for r in finances)
        assert any(r['phase']=='explicit_monitor'for r in finances)
        assert not any(r['active_plan'] and r['phase']=='planned_auto' for r in books)
        diagnostics['completed']='full'
    finally:
        diagnostics.update(state=engine.lp_auto_state(),document=document(),state_records=state_records,
            result_packets=result_packets,full_packets=full_packets,
            current_orders=[o.model_dump(mode='json') for o in account.orders])
        diagnostics.update(case=case,stage=stage[0],clock=runtime.clock[0].isoformat(),books=books,finances=finances,
            exact_reads=exact_reads,result_rows=result_rows,observations=observations,calls=account.cancels,
            posts=account.posts,sessions=store.lp_sessions(),audits=store.lp_actions(sid)if sid else [])
        (tmp_path/'ordinary-terminal-diagnostics.json').write_text(json.dumps(diagnostics,indent=2,default=str)+'\n')


@pytest.mark.parametrize('window', ['atomic_closure_commit'])
def test_cancel_owner_retirement_recovers_after_committed_session(runtime, monkeypatch, tmp_path, window):
    import json
    import hashlib
    from pathlib import Path
    import sqlite3
    from threading import Thread, get_ident
    from time import monotonic
    from polymarket.models.clob.cancel import CancelOrdersResponse
    from polymarket.models.clob.order_book import OrderBookLevel
    from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic
    from tests.test_lp_order_registration_contract import _open_order

    public = _CandidateSDKPublic(runtime.clock[0])
    original_book, pressure, phase = public.get_order_book, [False], ['setup']
    books, finances, commits, errors, observations = [], [], [], [], []
    result_packets, restart_states = [], []
    packet_ready, pressure_seen = [False], Event()
    original = {oid:_open_order(oid,'BUY',price=price,original='20')
        for oid,price in [('rank-a','.39'),('rank-b','.39'),('rank-d','.40')]}
    def book(*, token_id):
        value = original_book(token_id=token_id).model_copy(update={'timestamp':runtime.clock[0], 'bids':(
            OrderBookLevel(price=Decimal('.40'),size=Decimal('3000' if pressure[0] else '1000')),
            OrderBookLevel(price=Decimal('.39'),size=Decimal('1000')))})
        books.append(dict(token=token_id,timestamp=value.timestamp,pressure=pressure[0],phase=phase[0]))
        if pressure[0]:
            pressure_seen.set()
        return value
    monkeypatch.setattr(public,'get_order_book',book)
    store,adapter,account,lp,engine = runtime(orders=tuple(original.values()),public_client=public)
    def venue(*, order_ids):
        account.cancels.append(order_ids)
        runtime.clock[0] += timedelta(seconds=1)
        assert order_ids==('rank-d',)
        return CancelOrdersResponse(canceled=(),not_canceled={'rank-d':'venue_denied'})
    monkeypatch.setattr(account,'cancel_orders',venue)
    monkeypatch.setattr(account,'get_order',lambda *,order_id:original[order_id],raising=False)
    def observe_balance(client):
        balance = client.get_balance_allowance
        def read(**kwargs):
            finances.append(dict(phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),
                timestamp=runtime.clock[0].isoformat(),request=deepcopy(kwargs)))
            return balance(**kwargs)
        monkeypatch.setattr(client,'get_balance_allowance',read)
    observe_balance(account)
    engine.lp_auto_configure(dict(budget_usd='39',target_buy_count=2,buy_price_level=2))
    assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
    session, = store.lp_active_sessions()
    sid, pause = session['session_id'], Event()
    def monitor(service, predicate, label):
        phase[0] = 'explicit_monitor'
        deadline = monotonic()+5
        while True:
            assert monotonic()<deadline, label
            value = service.reconcile_facts(sid,monitor=True)
            current = store.lp_session(sid)
            observations.append(dict(error=value[3],status=deepcopy(value[4]),
                book=deepcopy(value[1].get('book'))if value[1]else None))
            if predicate(current,value):
                return current,value
            pause.wait(min(.01,max(0,deadline-monotonic())))
    session,_ = monitor(lp,lambda s,v:s['queue_protection']['levels']['0.40']['state']=='monitoring',
        'Original D monitoring watchdog')
    baseline = deepcopy(session['queue_protection'])
    assert baseline['levels']['0.40']['order_id']=='rank-d'
    assert Decimal(baseline['levels']['0.40']['baseline_front'])==980
    phase[0] = 'planned_auto'
    engine.lp_auto_set_desired_running(True)
    scheduler = LPAutoScheduler(engine,clock=lambda:runtime.clock[0])
    assert scheduler.run_due()
    sent = engine.lp_auto_state()
    plan = deepcopy(sent['last_round'])
    assert sent['active_plan'] is None and plan['completed_at']
    assert [a['order_id'] for a in plan['actions']] == ['rank-d']
    assert plan['actions'][0]['state'] == 'rejected' and plan['actions'][0]['reason'] == 'venue_denied'
    assert [t['order_id'] for t in plan['targets']] == ['rank-a', 'rank-b']
    assert all(Decimal(t['price']) == Decimal('.39') and Decimal(t['quantity']) == 20 for t in plan['targets'])
    assert sent['slots']['occupied'] == 3 and Decimal(sent['funds']['buy_reserved_usd']) == Decimal('23.60')
    original_completed = datetime.fromisoformat(plan['completed_at'])
    assert datetime.fromisoformat(sent['plan_wait']['deadline']) == original_completed + timedelta(seconds=30)
    with sqlite3.connect(store.path) as connection:
        initial_document = json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    original_rotation = deepcopy(initial_document['account_rotations']['rank-d'])
    assert (original_rotation['session_id'], original_rotation['order_id'], original_rotation['price'],
        original_rotation['original_quantity']) == (sid, 'rank-d', '0.40', '20')
    assert original_rotation['rotation_round_id'] == plan['round_id']
    assert original_rotation['rotation_cancel_outcome'] == 'rejected'
    audit, = deepcopy(store.lp_actions(sid))
    assert audit['state']=='rejected' and audit['reason']=='venue_denied'
    assert audit['rotation_round_id']==plan['round_id'] and audit['order_id']=='rank-d'
    assert account.cancels==[('rank-d',)] and account.posts==[]
    assert not any(f['active_plan'] and f['phase'] == 'planned_auto' for f in finances)
    owned = store.lp_session(sid)
    assert owned['yield_rotation_cancel_owners']['rank-d'] == plan['round_id']
    assert owned['yield_rotation_protection_before']['rank-d'] == baseline
    engine.lp_auto_set_desired_running(False)
    assert engine.lp_auto_state()['desired_running'] is False
    interrupted, sender_ids = Event(), []
    result_reader = adapter.lp_order_result_snapshot
    def observed_result(request):
        packet = result_reader(request)
        result_packets.append(deepcopy(dict(request=request, packet=packet, thread=get_ident(), phase=phase[0])))
        if phase[0] == 'current_cleanup' and request.get('owned_order_ids') == ['rank-d']:
            assert packet['authenticated'] is True
            assert hashlib.sha256(str(packet['wallet_address']).strip().casefold().encode()).hexdigest() == initial_document['account_id']
            assert all(packet[key] is True for key in ('open_orders_complete', 'positions_complete',
                'trades_complete', 'pagination_complete'))
            row, = [row for row in packet['orders'] if row['order_id'] == 'rank-d']
            assert row['token_id'] == owned['token_id'] and row['side'] == 'BUY' and row['status'] == 'LIVE'
            assert Decimal(row['price']) == Decimal('.40') and Decimal(row['original_size']) == 20
            assert row['fill_quantity_known'] is True and Decimal(row['size_matched']) == 0
            packet_ready[0] = True
        return packet
    monkeypatch.setattr(adapter, 'lp_order_result_snapshot', observed_result)
    connect = sqlite3.connect
    class EndAfterCommit(BaseException):
        pass
    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            command = sql.strip().upper()
            if command in ('BEGIN', 'BEGIN IMMEDIATE'):
                self.before_session = self.updated_session = None
            if command.startswith('UPDATE LP_SESSIONS ') and sid in parameters:
                before_row = super().execute('SELECT payload FROM lp_sessions WHERE session_id=?', (sid,)).fetchone()
                self.before_session = json.loads(before_row[0]) if before_row else None
                for value in parameters:
                    if isinstance(value, str) and value.startswith('{'):
                        self.updated_session = json.loads(value)
            result = super().execute(sql, parameters)
            if (command == 'COMMIT' and packet_ready[0] and not interrupted.is_set()
                    and sender_ids and get_ident() == sender_ids[0]):
                before = getattr(self, 'before_session', None)
                payload = getattr(self, 'updated_session', None)
                if (before is None or payload is None
                        or (before.get('yield_rotation_cancel_owners') or {}).get('rank-d') != plan['round_id']
                        or (payload.get('yield_rotation_cancel_owners') or {}).get('rank-d') == plan['round_id']):
                    return result
                # Intercept FIRST exact original owner removal regardless of
                # baseline/saved/settled state. Assert atomicity only afterward.
                interrupted.set()
                assert not self.in_transaction, 'Native first removal COMMIT must finish'
                with connect(store.path) as reader:
                    durable = json.loads(reader.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
                    actual = json.loads(reader.execute('SELECT payload FROM lp_sessions WHERE session_id=?', (sid,)).fetchone()[0])
                current_audits = store.lp_actions(sid)
                commits.append(deepcopy(dict(window=window, before_session=before, session=actual, pool=durable,
                    audit=current_audits, native_commit_returned=True, in_transaction=self.in_transaction, thread=get_ident())))
                assert actual == payload
                assert set(actual['owned_order_ids']) == set(original)
                assert actual['queue_protection']['levels']['0.40'] == baseline['levels']['0.40']
                assert actual['queue_protection']['levels']['0.39'] == baseline['levels']['0.39']
                assert actual['queue_protection'] == baseline
                assert not actual.get('yield_rotation_cancel_owners') and not actual.get('yield_rotation_protection_before')
                assert 'rank-d' not in actual.get('augment_cancel_requested', [])
                assert actual.get('entry_cancel_requested') == session.get('entry_cancel_requested')
                assert actual.get('stop_requested') == session.get('stop_requested')
                assert actual.get('stop_loss_latched') == session.get('stop_loss_latched')
                rotation = durable['account_rotations']['rank-d']
                assert rotation['state'] == 'active' and rotation['rotation_settled_at']
                assert all(rotation[k] == original_rotation[k] for k in ('rotation_round_id', 'rotation_requested_at',
                    'session_id', 'order_id', 'token_id', 'condition_id', 'price', 'quantity', 'original_quantity'))
                assert current_audits == [audit] and durable['desired_running'] is False
                assert durable['rounds'][plan['round_id']] == initial_document['rounds'][plan['round_id']]
                assert {o.id for o in account.orders if o.status == 'LIVE'} == set(original)
                assert sum(o.price * (o.original_size - o.size_matched) for o in account.orders
                    if o.status == 'LIVE') == Decimal('23.60')
                raise EndAfterCommit(window)
            return result
    def observed_connect(database,*args,**kwargs):
        if Path(database).resolve()==store.path.resolve():
            kwargs['factory'] = ObservedConnection
        return connect(database,*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',observed_connect)
    phase[0] = 'current_cleanup'
    runtime.clock[0] = datetime.fromisoformat(sent['plan_wait']['deadline'])
    def original_sender():
        sender_ids.append(get_ident())
        try:
            scheduler.run_due()
        except BaseException as exc:
            errors.append(exc)
    sender = Thread(target=original_sender)
    sender.start()
    try:
        assert interrupted.wait(5), 'Exact native post-COMMIT boundary watchdog'
        sender.join(5)
        assert not sender.is_alive() and len(errors)==1 and isinstance(errors[0],EndAfterCommit), errors
        for path in (tmp_path/'execution.lock',Path(str(store.path)+'.lp-auto-round.lock')):
            assert path.exists()
            with path.open('r+')as resource:
                fcntl.flock(resource.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                fcntl.flock(resource.fileno(),fcntl.LOCK_UN)
        assert len(commits) == 1 and packet_ready[0]
        assert store.lp_actions(sid) == [audit]
        assert engine.lp_auto_state()['active_plan'] is None
        assert engine.lp_auto_state()['slots']['occupied'] == 3
        assert Decimal(engine.lp_auto_state()['funds']['buy_reserved_usd']) == Decimal('23.60')
        _,_,restarted_account,restarted_lp,restarted = runtime(orders=account.orders,public_client=public)
        monkeypatch.setattr(restarted_account,'get_order',lambda *,order_id:original[order_id],raising=False)
        observe_balance(restarted_account)
        def protection(*, order_ids):
            restarted_account.cancels.append(order_ids)
            runtime.clock[0] += timedelta(seconds=1)
            assert order_ids==('rank-d',) and pressure[0]
            return CancelOrdersResponse(canceled=order_ids,not_canceled={})
        monkeypatch.setattr(restarted_account,'cancel_orders',protection)
        phase[0] = 'paused_current_check'
        restarted.lp_auto_run_once()
        state = restarted.lp_auto_state()
        restart_states.append(deepcopy(state))
        assert state['desired_running'] is False and state['active_plan'] is None
        assert state['slots']['occupied'] == 3 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('23.60')
        assert state['plan_wait']['kind'] == 'round'
        started = datetime.fromisoformat(state['plan_wait']['started_at'])
        deadline = datetime.fromisoformat(state['plan_wait']['deadline'])
        assert deadline == started + timedelta(seconds=30)
        with connect(store.path) as reader:
            resumed_document = json.loads(reader.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
        assert resumed_document['account_rotations']['rank-d'] == commits[0]['pool']['account_rotations']['rank-d']
        assert resumed_document['rounds'][plan['round_id']] == initial_document['rounds'][plan['round_id']]
        assert resumed_document['account_financial_facts']['financial_status'] == 'known'
        assert datetime.fromisoformat(resumed_document['account_financial_facts']['checked_at']) >= datetime.fromisoformat(
            initial_document['account_financial_facts']['checked_at'])
        restored = store.lp_session(sid)
        assert restored['queue_protection'] == baseline
        assert not restored.get('yield_rotation_cancel_owners') and not restored.get('yield_rotation_protection_before')
        assert 'rank-d' not in restored.get('augment_cancel_requested', [])
        assert restored.get('entry_cancel_requested') == session.get('entry_cancel_requested')
        assert restored.get('stop_requested') == session.get('stop_requested')
        assert restored.get('stop_loss_latched') == session.get('stop_loss_latched')
        assert store.lp_actions(sid) == [audit]
        assert restarted_account.cancels == restarted_account.posts == []
        before_reads = len(finances), len(result_packets), len(books)
        wake = LPAutoScheduler(restarted, clock=lambda: runtime.clock[0])
        for elapsed in (1, 29):
            runtime.clock[0] = started + timedelta(seconds=elapsed)
            wake.request_check()
            assert not wake.run_due()
            restarted.lp_auto_run_once()
            assert (len(finances), len(result_packets), len(books)) == before_reads
            assert store.lp_actions(sid) == [audit]
            assert store.lp_session(sid)['queue_protection'] == baseline
        _, again_adapter, again_account, _, again = runtime(orders=account.orders, public_client=public)
        observe_balance(again_account)
        again_result = again_adapter.lp_order_result_snapshot
        def observed_again_result(request):
            packet = again_result(request)
            result_packets.append(deepcopy(dict(request=request, packet=packet, thread=get_ident(), phase='cold_before_deadline')))
            return packet
        monkeypatch.setattr(again_adapter, 'lp_order_result_snapshot', observed_again_result)
        again.lp_auto_run_once()
        assert again_account.cancels == again_account.posts == []
        assert (len(finances), len(result_packets), len(books)) == before_reads
        assert store.lp_actions(sid) == [audit] and store.lp_session(sid)['queue_protection'] == baseline
        pressure[0] = True
        runtime.clock[0] += timedelta(seconds=1)
        pressure_at = runtime.clock[0]
        restored,consumed = monitor(restarted_lp,lambda s,v:bool(v[1]) and bool(v[1].get('book'))
            and v[1]['book']['token_id']==session['token_id'] and v[1]['book']['timestamp']==pressure_at
            and s['queue_protection']['levels']['0.40'].get('cancel_reason')=='queue_ahead_ratio',
            'Actual distinct D protective consumption watchdog')
        assert pressure_seen.is_set() and consumed[4]['session_id']==sid
        assert Decimal(restored['queue_protection']['levels']['0.40']['ratio'])==Decimal(980)/Decimal(3000)
        assert restored['queue_protection']['levels']['0.40']['cancel_targets']==['rank-d']
        assert restored['queue_protection']['levels']['0.40']['state']=='canceling'
        assert restarted_account.cancels==[('rank-d',)] and restarted_account.posts==[]
        protective, = [a for a in store.lp_actions(sid)if a.get('role')=='entry-protection-cancel']
        assert protective['state']=='accepted' and protective['action_key']!=audit['action_key']
        assert any(o.id=='rank-d' and o.status=='LIVE'for o in restarted_account.orders)
        assert next(a for a in store.lp_actions(sid)if a['action_key']==audit['action_key'])==audit
        assert not any(f['active_plan'] and f['phase']=='planned_auto'for f in finances)
    finally:
        sender.join(5)
        (tmp_path/'committed-closure-observer.json').write_text(json.dumps(dict(window=window,commits=commits,
            errors=[repr(e)for e in errors],sender_alive=sender.is_alive(),books=books,finances=finances,
            observations=observations,result_packets=result_packets,restart_states=restart_states,
            original_round=plan,original_rotation=original_rotation,baseline=baseline,
            packet_ready=packet_ready[0],pressure_seen=pressure_seen.is_set(),
            sessions=store.lp_sessions(),audits=store.lp_actions(sid)),indent=2,default=str)+'\n')


@pytest.mark.parametrize('outcome', ['accepted_live', 'unknown_then_terminal', 'prior_refusal_then_unknown'])
def test_later_facts_publication_restores_deferred_yield_protection(runtime, monkeypatch, tmp_path, outcome):
    import json
    from time import monotonic
    from polymarket.models.clob.cancel import CancelOrdersResponse
    from polymarket.models.clob.order_book import OrderBookLevel
    from open_trader.polymarket_trading import PolymarketTradingError
    from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic
    from tests.test_lp_order_registration_contract import _open_order

    engine = None
    healthy_book, pressure_book = Event(), Event()
    public = _CandidateSDKPublic(runtime.clock[0])
    source_book, pressure, phase = public.get_order_book, [False], ['setup']
    books, finances, observations, diagnostics = [], [], [], {}
    stage, sid, terminal = ['import'], None, {}
    prior_audit, prior_calls = None, []
    original = {oid:_open_order(oid,'BUY',price=price,original='20')
        for oid,price in [('rank-a','.39'),('rank-b','.39'),('rank-d','.40')]}
    def book(*, token_id):
        value = source_book(token_id=token_id).model_copy(update={'timestamp':runtime.clock[0], 'bids':(
            OrderBookLevel(price=Decimal('.40'),size=Decimal('3000' if pressure[0] else '1000')),
            OrderBookLevel(price=Decimal('.39'),size=Decimal('1000')))})
        books.append(dict(token_id=token_id,timestamp=value.timestamp,pressure=pressure[0],phase=phase[0],
            active_plan=bool(engine and engine.lp_auto_state()['active_plan']),value=value.model_dump(mode='json')))
        (pressure_book if pressure[0] else healthy_book).set()
        return value
    monkeypatch.setattr(public,'get_order_book',book)
    store,adapter,account,lp,engine = runtime(orders=tuple(original.values()),public_client=public)
    balance = account.get_balance_allowance
    def financial(**kwargs):
        finances.append(dict(phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),
            at=runtime.clock[0].isoformat(),request=deepcopy(kwargs)))
        return balance(**kwargs)
    def cancel(*, order_ids):
        account.cancels.append(order_ids)
        runtime.clock[0] += timedelta(seconds=1)
        if order_ids==('extra',):
            assert stage[0]=='ordinary_extra_cancel'
            if outcome in ('unknown_then_terminal','prior_refusal_then_unknown'):
                raise TimeoutError('original extra cancel outcome unknown')
            return CancelOrdersResponse(canceled=order_ids,not_canceled={})
        assert order_ids==('rank-d',)
        return (CancelOrdersResponse(canceled=order_ids,not_canceled={}) if pressure[0] else
            CancelOrdersResponse(canceled=(),not_canceled={'rank-d':'venue_denied'}))
    monkeypatch.setattr(account,'get_balance_allowance',financial)
    monkeypatch.setattr(account,'get_order',lambda *,order_id:terminal.get(order_id,original[order_id]),raising=False)
    monkeypatch.setattr(account,'cancel_orders',cancel)
    import sqlite3
    result_packets, full_packets, state_records = [], [], []
    result_reader = adapter.lp_order_result_snapshot
    def observed_results(request):
        packet = result_reader(request)
        result_packets.append(deepcopy(dict(stage=stage[0],phase=phase[0],request=request,packet=packet)))
        return packet
    monkeypatch.setattr(adapter,'lp_order_result_snapshot',observed_results)
    full_reader = adapter.lp_account_snapshot_shared
    def observed_full(**kwargs):
        packet = full_reader(**kwargs)
        full_packets.append(deepcopy(dict(stage=stage[0],phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),packet=packet)))
        return packet
    monkeypatch.setattr(adapter,'lp_account_snapshot_shared',observed_full)
    def document():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state=deepcopy(engine.lp_auto_state())
        state_records.append(dict(label=label,state=state,session=store.lp_session(sid),document=document(),
            audits=store.lp_actions(sid),orders=[o.model_dump(mode='json') for o in account.orders],
            cancels=list(account.cancels),posts=deepcopy(account.posts)))
        return state
    def paused_shape(state):
        assert state['desired_running'] is False and state['active_plan'] is None
        assert state['last_round']['actions']==[] and state['last_round']['checked_at']
        assert state['plan_wait']['kind']=='round'
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(state['plan_wait']['started_at'])+timedelta(seconds=30)
    def dispatched(state):
        assert state['active_plan'] is None and state['last_round']['completed_at']
        assert state['plan_wait']['kind']=='round'
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(state['last_round']['completed_at'])+timedelta(seconds=30)
    def no_core_io():
        counts=(len(finances),len(books),len(result_packets),len(full_packets))
        last=deepcopy(engine.lp_auto_state()['last_round']);wait=deepcopy(engine.lp_auto_state()['plan_wait'])
        scheduler.request_check();scheduler.run_due();engine.lp_auto_run_once()
        assert (len(finances),len(books),len(result_packets),len(full_packets))==counts
        assert engine.lp_auto_state()['last_round']==last and engine.lp_auto_state()['plan_wait']==wait
    pause = Event()
    def monitor_until(predicate, label):
        phase[0] = 'explicit_monitor'
        deadline = monotonic()+5
        healthy_book.clear();pressure_book.clear()
        while True:
            assert monotonic()<deadline,label
            value = lp.reconcile_facts(sid,monitor=True)
            observed = dict(stage=stage[0],error=value[3],status=deepcopy(value[4]),
                book=deepcopy(value[1].get('book'))if value[1]else None)
            observations.append(observed)
            current = store.lp_session(sid)
            if ((pressure_book if pressure[0] else healthy_book).is_set() and predicate(current,observed)):
                return current,observed
            pause.wait(min(.01,max(0,deadline-monotonic())))
    try:
        engine.lp_auto_configure(dict(budget_usd='39',target_buy_count=2,buy_price_level=2))
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        session, = store.lp_active_sessions()
        sid = session['session_id']
        session,_ = monitor_until(lambda s,o:s['queue_protection']['levels']['0.40']['state']=='monitoring',
            'Genuine original D monitoring watchdog')
        baseline = deepcopy(session['queue_protection'])
        assert baseline['levels']['0.40']['order_id']=='rank-d'
        assert Decimal(baseline['levels']['0.40']['baseline_front'])==980
        scheduler = LPAutoScheduler(engine,clock=lambda:runtime.clock[0])
        if outcome=='prior_refusal_then_unknown':
            stage[0],phase[0] = 'prior_D_approval','planned_auto'
            engine.lp_auto_set_desired_running(True)
            assert scheduler.run_due()
            prior_sent = engine.lp_auto_state()
            prior_plan = deepcopy(prior_sent['last_round'])
            dispatched(prior_sent);diagnostics['prior_D_round']=prior_plan
            assert [a['order_id']for a in prior_plan['actions']]==['rank-d']
            assert [t['order_id']for t in prior_plan['targets']]==['rank-a','rank-b']
            prior_audit, = deepcopy(store.lp_actions(sid))
            assert prior_audit['state']=='rejected' and prior_audit['reason']=='venue_denied'
            assert prior_audit['rotation_round_id']==prior_plan['round_id']
            engine.lp_auto_set_desired_running(False)
            phase[0]='current_cleanup'
            runtime.clock[0] = datetime.fromisoformat(prior_sent['plan_wait']['deadline'])
            assert scheduler.run_due()
            prior_done=capture('prior_D_paused_cleanup')
            paused_shape(prior_done)
            assert prior_done['last_round']['round_id']!=prior_plan['round_id']
            assert next(a for a in store.lp_actions(sid) if a['action_key']==prior_audit['action_key'])==prior_audit
            prior_session = store.lp_session(sid)
            assert not prior_session.get('yield_rotation_cancel_owners') and not prior_session.get('yield_rotation_protection_before')
            assert prior_session['queue_protection']['levels']['0.40']['state']=='monitoring'
            assert prior_session['queue_protection']['levels']['0.40']['order_id']=='rank-d'
            assert Decimal(prior_session['queue_protection']['levels']['0.40']['baseline_front'])==980
            assert store.lp_actions(sid)==[prior_audit] and account.cancels==[('rank-d',)] and account.posts==[]
            prior_calls = [('rank-d',)]
            runtime.clock[0] = datetime.fromisoformat(prior_done['plan_wait']['deadline'])
            stage[0],phase[0] = 'fresh_second_round_publication','explicit_dashboard'
            adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
            assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
            session,_ = monitor_until(lambda s,o:bool(o['book']) and o['book']['timestamp']==runtime.clock[0]
                and s['queue_protection']['levels']['0.40']['state']=='monitoring', 'Actual second-round normal book watchdog')
            baseline = deepcopy(session['queue_protection'])
            assert baseline['levels']['0.40']['order_id']=='rank-d'
            assert Decimal(baseline['levels']['0.40']['baseline_front'])==980
            for key in ('baseline_front','baseline_price','baseline_source','baseline_source_timestamp',
                        'baseline_book_received_at','baseline_book_hash','baseline_version'):
                assert baseline['levels']['0.40'][key]==prior_session['queue_protection']['levels']['0.40'][key],key
        stage[0],phase[0] = 'original_D_approval','planned_auto'
        engine.lp_auto_set_desired_running(True)
        assert scheduler.run_due()
        sent = engine.lp_auto_state()
        plan = deepcopy(sent['last_round'])
        dispatched(sent);diagnostics['original_D_round']=plan
        assert [a['order_id']for a in plan['actions']]==['rank-d']
        assert [t['order_id']for t in plan['targets']]==['rank-a','rank-b']
        d_audit, = [deepcopy(a)for a in store.lp_actions(sid)if a.get('rotation_round_id')==plan['round_id']]
        if prior_audit is not None:
            import sqlite3
            assert plan['round_id']!=prior_plan['round_id']
            assert plan['actions'][0]['action_id']!=prior_plan['actions'][0]['action_id']
            assert d_audit['action_id']!=prior_audit['action_id'] and d_audit['action_key']!=prior_audit['action_key']
            assert next(a for a in store.lp_actions(sid)if a['action_key']==prior_audit['action_key'])==prior_audit
            assert account.cancels==[('rank-d',),('rank-d',)] and account.posts==[]
            with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True)as reader:
                actual = json.loads(reader.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
            assert actual['account_rotations']['rank-d']['rotation_round_id']==plan['round_id']
            assert actual['account_rotations']['rank-d']['session_id']==sid
            diagnostics['prior_round'] = dict(plan=prior_plan,audit=prior_audit,deadline=prior_done['plan_wait']['deadline'])

        assert d_audit['state']=='rejected' and d_audit['reason']=='venue_denied'
        assert d_audit['rotation_round_id']==plan['round_id']
        stage[0],phase[0] = 'public_extra_import','explicit_dashboard'
        runtime.clock[0] += timedelta(seconds=1)
        original['extra'] = _open_order('extra','BUY',price='.40',original='20')
        account.orders = (*account.orders,original['extra'])
        adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        assert set(store.lp_session(sid)['owned_order_ids'])==set(original)
        assert store.lp_session(sid)['yield_rotation_protection_before']['rank-d']==baseline
        stage[0],phase[0] = 'ordinary_extra_cancel','explicit_manual'
        if outcome in ('unknown_then_terminal','prior_refusal_then_unknown'):
            with pytest.raises(PolymarketTradingError)as unknown:
                engine.lp_cancel_orders(dict(confirm=True,order_ids=['extra']))
            diagnostics['actual_public_error'] = dict(type=type(unknown.value).__name__,code=unknown.value.error_code)
            assert unknown.value.error_code
        else:
            result = engine.lp_cancel_orders(dict(confirm=True,order_ids=['extra']))
            assert result==dict(requested=1,canceled=['extra'],not_canceled={},skipped=[])
        extra_audit, = [deepcopy(a)for a in store.lp_actions(sid)if a.get('order_id')=='extra']
        assert extra_audit['state']==('unknown'if outcome in ('unknown_then_terminal','prior_refusal_then_unknown')else 'accepted')
        assert extra_audit['role']=='reconciliation_cancel' and extra_audit['session_id']==sid
        assert not extra_audit.get('rotation_round_id') and extra_audit['action_id']!=d_audit['action_id']
        stage[0],phase[0] = 'explicit_four_order_finance','explicit_dashboard'
        adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        explicit = capture('actual_manual_extra_four_order_publication')
        assert explicit['slots']['occupied']==4 and Decimal(explicit['funds']['buy_reserved_usd'])==Decimal('31.60')
        assert explicit['funds']['status']=='known' and explicit['funds']['as_of']
        assert {o.id for o in account.orders if o.status=='LIVE'}=={'rank-a','rank-b','rank-d','extra'}
        assert datetime.fromisoformat(explicit['funds']['as_of'])>datetime.fromisoformat(sent['funds']['as_of'])
        independent = deepcopy(store.lp_session(sid))
        engine.lp_auto_set_desired_running(False)
        stage[0],phase[0] = 'original_D_result','current_cleanup'
        runtime.clock[0] = datetime.fromisoformat(sent['plan_wait']['deadline'])
        assert scheduler.run_due()
        state=capture('paused_original_D_deferred_baseline')
        paused_shape(state)
        assert state['last_round']['round_id']!=plan['round_id']
        assert next(a for a in store.lp_actions(sid) if a['action_key']==d_audit['action_key'])==d_audit
        round_deadline = state['plan_wait']['deadline']
        assert state['slots']['occupied']==4 and Decimal(state['funds']['buy_reserved_usd'])==Decimal('31.60')
        assert state['funds']['status']=='known' and datetime.fromisoformat(state['funds']['as_of'])>=datetime.fromisoformat(explicit['funds']['as_of'])
        deferred = deepcopy(store.lp_session(sid))
        assert deferred['yield_rotation_protection_before']['rank-d']==baseline
        assert not deferred.get('yield_rotation_cancel_owners')
        level = deferred['queue_protection']['levels']['0.40']
        assert level['state']=='canceling' and level['cancel_reason']=='yield_rotation'
        assert 'rank-d'in level['cancel_targets']
        for key in ('entry_cancel_requested','owned_cancel_requested'):
            assert deferred.get(key)==independent.get(key),key
        assert [x for x in deferred.get('augment_cancel_requested',[])if x!='rank-d']==[
            x for x in independent.get('augment_cancel_requested',[])if x!='rank-d']
        assert next(a for a in store.lp_actions(sid)if a['action_key']==extra_audit['action_key'])==extra_audit
        assert next(a for a in store.lp_actions(sid)if a['action_key']==d_audit['action_key'])==d_audit
        if prior_audit is not None:
            assert next(a for a in store.lp_actions(sid)if a['action_key']==prior_audit['action_key'])==prior_audit
        assert account.cancels==prior_calls+[('rank-d',),('extra',)] and account.posts==[]
        stage[0],phase[0] = 'later_extra_terminal','explicit_monitor'
        terminal['extra'] = original['extra'].model_copy(update={'status':'CANCELED'})
        account.orders = tuple(o for o in account.orders if o.id!='extra')
        runtime.clock[0] += timedelta(seconds=1)
        report = lp.reconcile_facts(sid,report_only=True)
        diagnostics['report_error'] = report[3]
        assert store.lp_session(sid)['yield_rotation_protection_before']['rank-d']==baseline
        assert account.cancels==prior_calls+[('rank-d',),('extra',)] and account.posts==[]
        session,_ = monitor_until(lambda s,o:not s.get('yield_rotation_protection_before')
            and s['queue_protection']['levels']['0.40']['state']=='monitoring'
            and s['queue_protection']['levels']['0.40']['order_id']=='rank-d'
            and s['order_history']['extra']['status']=='CANCELED', 'Actual normal deferred restoration watchdog')
        for key in ('order_id','baseline_front','baseline_price','baseline_source','baseline_source_timestamp',
                    'baseline_book_received_at','baseline_book_hash','baseline_version'):
            assert session['queue_protection']['levels']['0.40'][key]==baseline['levels']['0.40'][key],key
        resolved = next(a for a in store.lp_actions(sid)if a['action_key']==extra_audit['action_key'])
        assert resolved['state']=='accepted'
        for key,value in extra_audit.items():
            if key not in ('state','updated_at'):
                assert resolved[key]==value,key
        assert next(a for a in store.lp_actions(sid)if a['action_key']==d_audit['action_key'])==d_audit
        if prior_audit is not None:
            assert next(a for a in store.lp_actions(sid)if a['action_key']==prior_audit['action_key'])==prior_audit
        assert account.cancels==prior_calls+[('rank-d',),('extra',)] and account.posts==[]
        stage[0],phase[0] = 'explicit_three_order_finance','explicit_dashboard'
        adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        later = engine.lp_auto_state()
        assert later['active_plan']is None and later['last_round']==state['last_round']
        assert later['plan_wait']['deadline']==round_deadline
        assert later['slots']['occupied']==3 and Decimal(later['funds']['buy_reserved_usd'])==Decimal('23.60')
        assert later['funds']['status']=='known'
        assert datetime.fromisoformat(later['funds']['as_of'])>datetime.fromisoformat(explicit['funds']['as_of'])
        stage[0] = 'distinct_D_ordinary_protection'
        pressure[0] = True
        runtime.clock[0] += timedelta(seconds=1)
        pressure_at = runtime.clock[0]
        session,consumed = monitor_until(lambda s,o:bool(o['book'])
            and o['book']['token_id']==session['token_id'] and o['book']['timestamp']==pressure_at
            and s['queue_protection']['levels']['0.40']['state']=='canceling'
            and s['queue_protection']['levels']['0.40'].get('cancel_reason')=='queue_ahead_ratio',
            'Actual distinct D protective consumption watchdog')
        assert consumed['status']['session_id']==sid
        assert Decimal(session['queue_protection']['levels']['0.40']['ratio'])==Decimal(980)/Decimal(3000)
        assert session['queue_protection']['levels']['0.40']['cancel_targets']==['rank-d']
        protective, = [a for a in store.lp_actions(sid)if a.get('role')=='entry-protection-cancel']
        assert protective['state']=='accepted' and protective['action_key']not in (d_audit['action_key'],extra_audit['action_key'])
        assert any(o.id=='rank-d' and o.status=='LIVE'for o in account.orders)
        assert account.cancels==prior_calls+[('rank-d',),('extra',),('rank-d',)] and account.posts==[]
        immutable = deepcopy(store.lp_actions(sid))
        stage[0] = 'no_duplicate_normal_publication'
        lp.reconcile_facts(sid,monitor=True)
        no_core_io()
        _,_,restart_account,_,restart = runtime(orders=account.orders,public_client=public)
        restart.lp_auto_run_once()
        assert restart_account.cancels==restart_account.posts==[]
        assert account.cancels==prior_calls+[('rank-d',),('extra',),('rank-d',)] and account.posts==[]
        assert store.lp_actions(sid)==immutable
        assert runtime.clock[0]<datetime.fromisoformat(round_deadline)
        assert not any(f['active_plan'] and f['phase']=='planned_auto'for f in finances)
        assert any(f['phase']=='explicit_dashboard'for f in finances) and any(f['phase']=='explicit_monitor'for f in finances)
        assert not any(r['active_plan'] and r['phase']=='planned_auto' for r in books)
        diagnostics['completed']='full'
    finally:
        diagnostics.update(state=engine.lp_auto_state(),document=document(),state_records=state_records,
            result_packets=result_packets,full_packets=full_packets,
            current_orders=[o.model_dump(mode='json') for o in account.orders])
        diagnostics.update(outcome=outcome,stage=stage[0],clock=runtime.clock[0].isoformat(),books=books,
            finances=finances,observations=observations,calls=account.cancels,posts=account.posts,
            sessions=store.lp_sessions(),audits=store.lp_actions(sid)if sid else [])
        (tmp_path/'deferred-publication-diagnostics.json').write_text(json.dumps(diagnostics,indent=2,default=str)+'\n')


@pytest.mark.parametrize('terminal_timing', ['terminal_together', 'independent_later'])
def test_distinct_round_cancel_requests_restore_survivor_protection(runtime, monkeypatch, tmp_path, terminal_timing):
    import json
    from time import monotonic
    from polymarket.models.clob.cancel import CancelOrdersResponse
    from polymarket.models.clob.order_book import OrderBookLevel
    from open_trader.polymarket_trading import PolymarketTradingError
    from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic
    from tests.test_lp_order_registration_contract import _open_order

    engine = None
    healthy_book, pressure_book = Event(), Event()
    public = _CandidateSDKPublic(runtime.clock[0])
    source_book, pressure, phase = public.get_order_book, [False], ['setup']
    books, finances, observations, diagnostics = [], [], [], {}
    stage, sid, terminal = ['import'], None, {}
    d_attempts = []
    original = {oid:_open_order(oid,'BUY',price=price,original='20')
        for oid,price in [('rank-a','.39'),('rank-b','.39'),('rank-d','.40')]}
    def book(*, token_id):
        value = source_book(token_id=token_id).model_copy(update={'timestamp':runtime.clock[0], 'bids':(
            OrderBookLevel(price=Decimal('.40'),size=Decimal('3000' if pressure[0] else '1000')),
            OrderBookLevel(price=Decimal('.39'),size=Decimal('1000')))})
        books.append(dict(token_id=token_id,timestamp=value.timestamp,pressure=pressure[0],phase=phase[0],
            active_plan=bool(engine and engine.lp_auto_state()['active_plan']),value=value.model_dump(mode='json')))
        (pressure_book if pressure[0] else healthy_book).set()
        return value
    monkeypatch.setattr(public,'get_order_book',book)
    store,adapter,account,lp,engine = runtime(orders=tuple(original.values()),public_client=public)
    balance = account.get_balance_allowance
    def financial(**kwargs):
        finances.append(dict(phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),
            at=runtime.clock[0].isoformat(),request=deepcopy(kwargs)))
        return balance(**kwargs)
    def cancel(*, order_ids):
        account.cancels.append(order_ids)
        runtime.clock[0] += timedelta(seconds=1)
        if order_ids==('extra',):
            assert stage[0]=='ordinary_extra_cancel'
            raise TimeoutError('original extra cancel outcome unknown')
        if order_ids==('survivor',):
            assert pressure[0]
            return CancelOrdersResponse(canceled=order_ids,not_canceled={})
        assert order_ids==('rank-d',) and not pressure[0]
        return (CancelOrdersResponse(canceled=order_ids,not_canceled={}) if stage[0]=='original_D_approval' else
            CancelOrdersResponse(canceled=(),not_canceled={'rank-d':'venue_denied'}))
    monkeypatch.setattr(account,'get_balance_allowance',financial)
    monkeypatch.setattr(account,'get_order',lambda *,order_id:terminal.get(order_id,original[order_id]),raising=False)
    monkeypatch.setattr(account,'cancel_orders',cancel)
    import sqlite3
    result_packets, full_packets, state_records = [], [], []
    result_reader = adapter.lp_order_result_snapshot
    def observed_results(request):
        packet = result_reader(request)
        result_packets.append(deepcopy(dict(stage=stage[0],phase=phase[0],request=request,packet=packet)))
        return packet
    monkeypatch.setattr(adapter,'lp_order_result_snapshot',observed_results)
    full_reader = adapter.lp_account_snapshot_shared
    def observed_full(**kwargs):
        packet = full_reader(**kwargs)
        full_packets.append(deepcopy(dict(stage=stage[0],phase=phase[0],active_plan=bool(engine.lp_auto_state()['active_plan']),packet=packet)))
        return packet
    monkeypatch.setattr(adapter,'lp_account_snapshot_shared',observed_full)
    def document():
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True) as connection:
            return json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
    def capture(label):
        state=deepcopy(engine.lp_auto_state())
        state_records.append(dict(label=label,state=state,session=store.lp_session(sid),document=document(),
            audits=store.lp_actions(sid),orders=[o.model_dump(mode='json') for o in account.orders],
            cancels=list(account.cancels),posts=deepcopy(account.posts)))
        return state
    def paused_shape(state):
        assert state['desired_running'] is False and state['active_plan'] is None
        assert state['last_round']['actions']==[] and state['last_round']['checked_at']
        assert state['plan_wait']['kind']=='round'
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(state['plan_wait']['started_at'])+timedelta(seconds=30)
    def dispatched(state):
        assert state['active_plan'] is None and state['last_round']['completed_at']
        assert state['plan_wait']['kind']=='round'
        assert datetime.fromisoformat(state['plan_wait']['deadline'])==datetime.fromisoformat(state['last_round']['completed_at'])+timedelta(seconds=30)
    def no_core_io():
        counts=(len(finances),len(books),len(result_packets),len(full_packets))
        last=deepcopy(engine.lp_auto_state()['last_round']);wait=deepcopy(engine.lp_auto_state()['plan_wait'])
        scheduler.request_check();scheduler.run_due();engine.lp_auto_run_once()
        assert (len(finances),len(books),len(result_packets),len(full_packets))==counts
        assert engine.lp_auto_state()['last_round']==last and engine.lp_auto_state()['plan_wait']==wait
    pause = Event()
    def monitor_until(predicate, label):
        phase[0] = 'explicit_monitor'
        deadline = monotonic()+5
        healthy_book.clear();pressure_book.clear()
        while True:
            assert monotonic()<deadline,label
            value = lp.reconcile_facts(sid,monitor=True)
            observed = dict(stage=stage[0],error=value[3],status=deepcopy(value[4]),
                book=deepcopy(value[1].get('book'))if value[1]else None)
            observations.append(observed)
            current = store.lp_session(sid)
            if ((pressure_book if pressure[0] else healthy_book).is_set() and predicate(current,observed)):
                return current,observed
            pause.wait(min(.01,max(0,deadline-monotonic())))
    try:
        engine.lp_auto_configure(dict(budget_usd='39',target_buy_count=2,buy_price_level=2))
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        session, = store.lp_active_sessions()
        sid = session['session_id']
        session,_ = monitor_until(lambda s,o:s['queue_protection']['levels']['0.40']['state']=='monitoring',
            'Genuine original D monitoring watchdog')
        baseline = deepcopy(session['queue_protection'])
        assert baseline['levels']['0.40']['order_id']=='rank-d'
        assert Decimal(baseline['levels']['0.40']['baseline_front'])==980
        scheduler = LPAutoScheduler(engine,clock=lambda:runtime.clock[0])
        stage[0],phase[0] = 'original_D_approval','planned_auto'
        engine.lp_auto_set_desired_running(True)
        assert scheduler.run_due()
        sent = engine.lp_auto_state()
        plan = deepcopy(sent['last_round'])
        dispatched(sent);capture('first_D_ACK_LIVE_locally_complete')
        assert [a['order_id']for a in plan['actions']]==['rank-d']
        assert [t['order_id']for t in plan['targets']]==['rank-a','rank-b']
        first_audit, = deepcopy(store.lp_actions(sid))
        assert first_audit['state']=='accepted' and first_audit['rotation_round_id']==plan['round_id']
        assert account.cancels==[('rank-d',)] and account.posts==[]
        assert sent['slots']['occupied']==3 and Decimal(sent['funds']['buy_reserved_usd'])==Decimal('23.60')
        runtime.clock[0]=datetime.fromisoformat(sent['plan_wait']['deadline'])-timedelta(seconds=1)
        no_core_io()
        assert store.lp_actions(sid)==[first_audit] and account.cancels==[('rank-d',)] and account.posts==[]
        stage[0],phase[0] = 'distinct_current_D_refusal','planned_auto'
        runtime.clock[0] = datetime.fromisoformat(sent['plan_wait']['deadline'])
        assert scheduler.run_due()
        retried=capture('second_current_D_refusal_locally_complete')
        second_plan=deepcopy(retried['last_round']);dispatched(retried)
        assert second_plan['round_id']!=plan['round_id']
        assert [a['order_id'] for a in second_plan['actions']]==['rank-d']
        assert second_plan['actions'][0]['action_id']!=plan['actions'][0]['action_id']
        assert [{k:t.get(k) for k in ('condition_id','token_id','session_id','order_id','price','quantity')} for t in second_plan['targets']]==[
            {k:t.get(k) for k in ('condition_id','token_id','session_id','order_id','price','quantity')} for t in plan['targets']]
        history=store.lp_session(sid)['order_history']['rank-d']
        assert Decimal(history['price'])==Decimal('.40') and Decimal(history.get('original_size',history.get('quantity')))==20
        assert retried['slots']['occupied']==3 and Decimal(retried['funds']['buy_reserved_usd'])==Decimal('23.60')
        d_attempts = [deepcopy(a)for a in store.lp_actions(sid)if a.get('order_id')=='rank-d']
        assert len(d_attempts)==2
        assert next(a for a in d_attempts if a['action_key']==first_audit['action_key'])==first_audit
        d_audit, = [a for a in d_attempts if a['action_key']!=first_audit['action_key']]
        assert d_audit['state']=='rejected' and d_audit['reason']=='venue_denied'
        assert {a['rotation_round_id']for a in d_attempts}=={plan['round_id'],second_plan['round_id']}
        assert len({a['action_id']for a in d_attempts})==len({a['action_key']for a in d_attempts})==2
        assert all(a['session_id']==sid and a['role']=='reconciliation_cancel'for a in d_attempts)
        assert account.cancels==[('rank-d',),('rank-d',)] and account.posts==[]
        import sqlite3
        with sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True)as reader:
            actual = json.loads(reader.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
        rotation = actual['account_rotations']['rank-d']
        assert rotation['rotation_round_id']==second_plan['round_id'] and rotation['session_id']==sid
        assert rotation['rotation_cancel_outcome']=='rejected' and rotation['rotation_rejection_reason']=='venue_denied'
        diagnostics['original_plan'],diagnostics['retry'] = plan,retried
        diagnostics['second_round']=second_plan
        stage[0],phase[0] = 'public_extra_import','explicit_dashboard'
        runtime.clock[0] += timedelta(seconds=1)
        original['extra'] = _open_order('extra','BUY',price='.40',original='20')
        account.orders = (*account.orders,original['extra'])
        adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        assert set(store.lp_session(sid)['owned_order_ids'])==set(original)
        assert store.lp_session(sid)['yield_rotation_protection_before']['rank-d']==baseline
        stage[0],phase[0] = 'ordinary_extra_cancel','explicit_manual'
        with pytest.raises(PolymarketTradingError)as unknown:
            engine.lp_cancel_orders(dict(confirm=True,order_ids=['extra']))
        diagnostics['actual_public_error'] = dict(type=type(unknown.value).__name__,code=unknown.value.error_code)
        assert unknown.value.error_code=='timeout'
        extra_audit, = [deepcopy(a)for a in store.lp_actions(sid)if a.get('order_id')=='extra']
        assert extra_audit['state']=='unknown'
        assert extra_audit['role']=='reconciliation_cancel' and extra_audit['session_id']==sid
        assert not extra_audit.get('rotation_round_id') and extra_audit['action_id']!=d_audit['action_id']
        stage[0],phase[0] = 'explicit_four_order_finance','explicit_dashboard'
        adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        explicit = capture('actual_manual_extra_four_order_publication')
        assert explicit['slots']['occupied']==4 and Decimal(explicit['funds']['buy_reserved_usd'])==Decimal('31.60')
        assert explicit['funds']['status']=='known' and explicit['funds']['as_of']
        assert {o.id for o in account.orders if o.status=='LIVE'}=={'rank-a','rank-b','rank-d','extra'}
        assert datetime.fromisoformat(explicit['funds']['as_of'])>datetime.fromisoformat(sent['funds']['as_of'])
        independent = deepcopy(store.lp_session(sid))
        engine.lp_auto_set_desired_running(False)
        stage[0],phase[0] = 'original_D_result','current_cleanup'
        runtime.clock[0] = datetime.fromisoformat(retried['plan_wait']['deadline'])
        assert scheduler.run_due()
        state=capture('paused_distinct_D_requests_deferred_baseline')
        paused_shape(state)
        assert state['last_round']['round_id'] not in (plan['round_id'],second_plan['round_id'])
        assert [a for a in store.lp_actions(sid) if a.get('order_id')=='rank-d']==d_attempts
        round_deadline = state['plan_wait']['deadline']
        assert state['slots']['occupied']==4 and Decimal(state['funds']['buy_reserved_usd'])==Decimal('31.60')
        assert state['funds']['status']=='known' and datetime.fromisoformat(state['funds']['as_of'])>=datetime.fromisoformat(explicit['funds']['as_of'])
        deferred = deepcopy(store.lp_session(sid))
        assert deferred['yield_rotation_protection_before']['rank-d']==baseline
        assert not deferred.get('yield_rotation_cancel_owners')
        level = deferred['queue_protection']['levels']['0.40']
        assert level['state']=='canceling' and level['cancel_reason']=='yield_rotation'
        assert 'rank-d'in level['cancel_targets']
        for key in ('entry_cancel_requested','owned_cancel_requested'):
            assert deferred.get(key)==independent.get(key),key
        assert [x for x in deferred.get('augment_cancel_requested',[])if x!='rank-d']==[
            x for x in independent.get('augment_cancel_requested',[])if x!='rank-d']
        assert next(a for a in store.lp_actions(sid)if a['action_key']==extra_audit['action_key'])==extra_audit
        assert [a for a in store.lp_actions(sid)if a.get('order_id')=='rank-d']==d_attempts
        assert account.cancels==[('rank-d',),('rank-d',),('extra',)] and account.posts==[]
        stage[0],phase[0] = 'public_survivor_import','explicit_dashboard'
        runtime.clock[0] += timedelta(seconds=1)
        original['survivor'] = _open_order('survivor','BUY',price='.40',original='20')
        account.orders = (*account.orders,original['survivor'])
        adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        imported = store.lp_session(sid)
        assert set(imported['owned_order_ids'])==set(original)
        assert imported['yield_rotation_protection_before']['rank-d']==baseline
        survivor = imported['order_history']['survivor']
        assert survivor['status']=='LIVE' and survivor['side']=='BUY' and Decimal(survivor['size_matched'])==0
        assert Decimal(survivor.get('original_size',survivor.get('quantity')))==20
        five = engine.lp_auto_state()
        assert five['slots']['occupied']==5 and Decimal(five['funds']['buy_reserved_usd'])==Decimal('39.60')
        assert Decimal(five['funds']['inventory_cost_usd'])==0 and Decimal(five['funds']['total_usd'])==39
        assert five['last_round']==state['last_round'] and five['plan_wait']['deadline']==round_deadline
        stage[0],phase[0] = 'D_terminal','explicit_monitor'
        terminal['rank-d'] = original['rank-d'].model_copy(update={'status':'CANCELED'})
        account.orders = tuple(o for o in account.orders if o.id!='rank-d')
        runtime.clock[0] += timedelta(seconds=1)
        if terminal_timing=='independent_later':
            persisted,_ = monitor_until(lambda s,o:s['order_history']['rank-d']['status']=='CANCELED'
                and s['queue_protection']['levels']['0.40']['state']=='canceled', 'Actual persisted canceled-yield watchdog')
            assert persisted['queue_protection']['levels']['0.40']['cancel_reason']=='yield_rotation'
            assert persisted['yield_rotation_protection_before']['rank-d']==baseline
            assert persisted['queue_protection']['levels']['0.40']['order_id']=='rank-d'
            assert next(a for a in store.lp_actions(sid)if a['action_key']==extra_audit['action_key'])==extra_audit
            assert persisted['order_history']['extra']['status']=='LIVE'
            diagnostics['persisted_canceled'] = persisted
            assert account.cancels==[('rank-d',),('rank-d',),('extra',)] and account.posts==[]
        stage[0] = 'independent_terminal'
        terminal['extra'] = original['extra'].model_copy(update={'status':'CANCELED'})
        account.orders = tuple(o for o in account.orders if o.id!='extra')
        runtime.clock[0] += timedelta(seconds=1)
        report = lp.reconcile_facts(sid,report_only=True)
        diagnostics['report_error'] = report[3]
        assert store.lp_session(sid)['yield_rotation_protection_before']['rank-d']==baseline
        assert account.cancels==[('rank-d',),('rank-d',),('extra',)] and account.posts==[]
        session,_ = monitor_until(lambda s,o:not s.get('yield_rotation_protection_before')
            and s['queue_protection']['levels']['0.40']['state']=='monitoring'
            and s['queue_protection']['levels']['0.40']['order_id']=='survivor'
            and s['order_history']['extra']['status']=='CANCELED', 'Actual request-set survivor restoration watchdog')
        for key in ('baseline_front','baseline_price','baseline_source','baseline_source_timestamp',
                    'baseline_book_received_at','baseline_book_hash','baseline_version'):
            assert session['queue_protection']['levels']['0.40'][key]==baseline['levels']['0.40'][key],key
        resolved = next(a for a in store.lp_actions(sid)if a['action_key']==extra_audit['action_key'])
        assert resolved['state']=='accepted'
        for key,value in extra_audit.items():
            if key not in ('state','updated_at'):
                assert resolved[key]==value,key
        assert [a for a in store.lp_actions(sid)if a.get('order_id')=='rank-d']==d_attempts
        assert account.cancels==[('rank-d',),('rank-d',),('extra',)] and account.posts==[]
        stage[0],phase[0] = 'explicit_three_order_finance','explicit_dashboard'
        adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
        assert engine.refresh_lp_dashboard_snapshot()['state']=='ready'
        later = engine.lp_auto_state()
        assert later['active_plan']is None and later['last_round']==state['last_round']
        assert later['plan_wait']['deadline']==round_deadline
        assert later['slots']['occupied']==3 and Decimal(later['funds']['buy_reserved_usd'])==Decimal('23.60')
        assert later['funds']['status']=='known'
        assert datetime.fromisoformat(later['funds']['as_of'])>datetime.fromisoformat(explicit['funds']['as_of'])
        stage[0] = 'distinct_survivor_ordinary_protection'
        pressure[0] = True
        runtime.clock[0] += timedelta(seconds=1)
        pressure_at = runtime.clock[0]
        session,consumed = monitor_until(lambda s,o:bool(o['book'])
            and o['book']['token_id']==session['token_id'] and o['book']['timestamp']==pressure_at
            and s['queue_protection']['levels']['0.40']['state']=='canceling'
            and s['queue_protection']['levels']['0.40'].get('cancel_reason')=='queue_ahead_ratio',
            'Actual distinct D protective consumption watchdog')
        assert consumed['status']['session_id']==sid
        assert Decimal(session['queue_protection']['levels']['0.40']['ratio'])==Decimal(980)/Decimal(3000)
        assert session['queue_protection']['levels']['0.40']['cancel_targets']==['survivor']
        protective, = [a for a in store.lp_actions(sid)if a.get('role')=='entry-protection-cancel']
        assert protective['state']=='accepted' and protective['action_key']not in (d_audit['action_key'],extra_audit['action_key'])
        assert any(o.id=='survivor' and o.status=='LIVE'for o in account.orders)
        assert account.cancels==[('rank-d',),('rank-d',),('extra',),('survivor',)] and account.posts==[]
        immutable = deepcopy(store.lp_actions(sid))
        stage[0] = 'no_duplicate_normal_publication'
        lp.reconcile_facts(sid,monitor=True)
        no_core_io()
        _,_,restart_account,_,restart = runtime(orders=account.orders,public_client=public)
        restart.lp_auto_run_once()
        assert restart_account.cancels==restart_account.posts==[]
        assert account.cancels==[('rank-d',),('rank-d',),('extra',),('survivor',)] and account.posts==[]
        assert store.lp_actions(sid)==immutable
        assert runtime.clock[0]<datetime.fromisoformat(round_deadline)
        assert not any(f['active_plan'] and f['phase']=='planned_auto'for f in finances)
        assert any(f['phase']=='explicit_dashboard'for f in finances) and any(f['phase']=='explicit_monitor'for f in finances)
        assert not any(r['active_plan'] and r['phase']=='planned_auto' for r in books)
        diagnostics['completed']='full'
    finally:
        diagnostics.update(state=engine.lp_auto_state(),document=document(),state_records=state_records,
            result_packets=result_packets,full_packets=full_packets,
            current_orders=[o.model_dump(mode='json') for o in account.orders])
        diagnostics.update(terminal_timing=terminal_timing,stage=stage[0],clock=runtime.clock[0].isoformat(),books=books,
            finances=finances,observations=observations,calls=account.cancels,posts=account.posts,
            sessions=store.lp_sessions(),audits=store.lp_actions(sid)if sid else [])
        (tmp_path/'request-set-publication-diagnostics.json').write_text(json.dumps(diagnostics,indent=2,default=str)+'\n')


@pytest.mark.parametrize('outcome', ['live', 'terminal_zero'])
def test_already_bound_buy_receipt_settles_plan_once(tmp_path, monkeypatch, outcome):
    """An original SDK receipt survives normal account binding before apply."""
    import json
    from collections import Counter
    from time import monotonic
    from polymarket.models.clob.order_response import AcceptedOrder
    from tests.test_lp_auto_plan_scheduler import restart_engine

    # Restore the business clock even when the result-reader fixture advances it.
    monkeypatch.setattr(pool, 'NOW', pool.NOW)
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch)
    install_order_result_reader(exchange, monkeypatch)
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    post = exchange.lp_post_order
    held, sid, original = None, None, None
    diagnostics = dict(outcome=outcome, stage='defer_original_receipt')
    receipt = []
    phase, full_reads, result_packets = ['original_execution'], [], []
    result_reader = exchange.lp_order_result_snapshot
    def observed_result(request):
        packet = result_reader(request)
        for field in ('orders', 'open_orders'):
            for row in packet[field]:
                if (receipt and row.get('order_id') == receipt[0].order_id
                        and row.get('token_id') == row.get('condition_id') == 'B' and row.get('side') == 'BUY'
                        and row.get('status') in ('LIVE', 'CANCELED')
                        and Decimal(str(row['price'])) == Decimal('.39')
                        and Decimal(str(row['original_size'])) == 20 and Decimal(str(row['size_matched'])) == 0):
                    row['fill_quantity_known'] = True
        result_packets.append(deepcopy(dict(phase=phase[0], request=request, packet=packet)))
        return packet
    monkeypatch.setattr(exchange, 'lp_order_result_snapshot', observed_result)
    for name in ('lp_account_snapshot', 'lp_account_snapshot_shared'):
        read = getattr(exchange, name)
        def observed_account(*args, _read=read, _name=name, **kwargs):
            observed = dict(phase=phase[0], method=_name, active_plan=bool(engine.lp_auto_state()['active_plan']))
            full_reads.append(observed)
            if phase[0] == 'metadata_recovery':
                observed['failed'] = True
                raise TimeoutError('Current full account unavailable; original bound result recovery remains independent')
            observed['failed'] = False
            return _read(*args, **kwargs)
        monkeypatch.setattr(exchange, name, observed_account)

    def accepted_before_apply(signed):
        nonlocal held
        response = post(signed)
        if signed['token_id'] == 'B' and not receipt:
            watchdog = monotonic() + 5
            while True:
                current = engine.lp_auto_state()
                siblings = [a for a in current['last_round']['actions'] if a['kind'] == 'buy' and a['token_id'] in {'C', 'E', 'F', 'G'}]
                if len(siblings) == 4 and all(a['state'] == 'success' for a in siblings):
                    for action in siblings:
                        actual = store.lp_session(action['session_id'])
                        actual_audit, = store.lp_actions(action['session_id'])
                        assert actual['entry_order_id'] == action['order_id'] and actual['submit_status'] == 'accepted'
                        assert actual_audit['state'] == 'accepted' and actual_audit['submit_receipt_at'] and actual_audit['submit_finished_at']
                    diagnostics['sibling_publication'] = deepcopy(dict(public=current, sessions=store.lp_sessions(),
                        audits={a['session_id']:store.lp_actions(a['session_id']) for a in siblings}))
                    break
                assert monotonic() < watchdog, 'Original CEFG public completion watchdog'
                Event().wait(.005)
            held = engine._lock_path.open('a+')
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = AcceptedOrder(order_id=response['order_id'], status='live',
                making_amount=Decimal('7.80'), taking_amount=Decimal('20'),
                trade_ids=(), transactions_hashes=())
            receipt.append(result)
            return result
        return response


    catalog_read = exchange.lp_reward_catalog
    def prepare_current_candidates(service):
        def catalog(*, condition_ids=None, **kwargs):
            result = catalog_read(condition_ids=tuple(exchange.rewards) if condition_ids is None else condition_ids, **kwargs)
            return {**result, 'state': 'known', 'complete': True}
        def history(token_ids, *, start_ts, end_ts, **kwargs):
            return dict(state='known', history={token:[dict(t=start_ts,p='.390'),dict(t=end_ts,p='.391')] for token in token_ids})
        def competition(**kwargs):
            return dict(state='known',complete=True,checked_at=pool.NOW,round_checked_at=pool.NOW,
                competitiveness={token:(Decimal('2.5'),pool.NOW) for token in exchange.rewards})
        monkeypatch.setattr(exchange,'lp_reward_catalog',catalog)
        monkeypatch.setattr(exchange,'lp_market_metadata',exchange.lp_market_metadata_fresh,raising=False)
        monkeypatch.setattr(exchange,'lp_price_history',history,raising=False)
        monkeypatch.setattr(exchange,'lp_market_competitiveness',competition,raising=False)
        competition_state=service.refresh_competition_cache()
        preparation=service.refresh_price_history()
        qualification=service.refresh_candidates(force=True)
        diagnostics['cold_public_preparation']=deepcopy(dict(competition=competition_state,
            preparation=preparation,qualification=qualification))
        assert competition_state['state']=='known' and competition_state['complete']
        assert preparation['preparation_outcome']=='success',preparation
        row,=[row for row in qualification['candidates'] if row['condition_id']=='B']
        assert row['selected_direction']['token_id']=='B' and row['selected_direction']['eligible'] is True

    monkeypatch.setattr(exchange, 'lp_post_order', accepted_before_apply)
    try:
        assert scheduler.run_due()
        before = engine.lp_auto_state()
        original = deepcopy(before['last_round'])
        original_targets = deepcopy(before['last_round']['targets'])
        assert original['completed_at'] and before['active_plan'] is None
        assert datetime.fromisoformat(before['plan_wait']['deadline']) == datetime.fromisoformat(original['completed_at']) + timedelta(seconds=30)
        first, = [a for a in original['actions'] if a['kind'] == 'buy' and a['token_id'] == 'B']
        assert first['state'] == 'unknown'
        assert len(original['actions']) == 5 and all(a['state'] == 'success' for a in original['actions'] if a is not first)
        diagnostics['original_local_completion'] = deepcopy(before)
        original_oid = receipt[0].order_id
        request_id = first.get('request_id', first['action_id'])
        session, = [s for s in store.lp_sessions()
            if s['idempotency_key'] == 'lp-auto:' + request_id]
        sid = session['session_id']
        audit, = store.lp_actions(sid)
        assert audit['state'] == 'accepted' and audit['post_started'] is True
        assert audit['order_id'] == receipt[0].order_id == original_oid
        assert audit['submit_stage'] == 'receipt_received'
        assert session['submit_stage'] == 'sending' and 'submit_status' not in session
        assert not session.get('entry_order_id')
        assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
        immutable_audit = deepcopy(store.lp_actions(sid))
        sibling_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id']))
            for s in store.lp_sessions() if s['session_id'] != sid}

        diagnostics['stage'] = 'normal_public_account_binding'
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
        bundle = pool._fresh_registration_bundle(exchange, lp)
        assert bundle['read_started_at'] > datetime.fromisoformat(audit['submit_finished_at'])
        assert lp.register_account_snapshot(bundle)['state'] == 'registered'
        bound = deepcopy(store.lp_session(sid))
        assert bound['entry_order_id'] == receipt[0].order_id
        assert 'submit_status' not in bound and bound['submit_stage'] == 'sending'
        assert bound['reservation_coverage']['state'] == 'covered'
        assert bound['order_history'][original_oid]['status'] == 'LIVE'
        assert store.lp_actions(sid) == immutable_audit
        original_protection = deepcopy(bound['queue_protection'])
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
        held = None

        terminal_before, original_intent = None, None
        if outcome == 'terminal_zero':
            diagnostics['stage'] = 'normal_public_cancel_and_terminal_reconciliation'
            assert engine.lp_stop(sid)['state'] == 'review'
            assert exchange.cancels == [original_oid]
            exact, = [o for o in exchange.orders if o['order_id'] == original_oid]
            assert exact['status'] == 'CANCELED' and Decimal(exact['size_matched']) == 0
            monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(microseconds=1))
            terminal_bundle = pool._fresh_registration_bundle(exchange, lp)
            assert not any(o['order_id'] == original_oid for o in terminal_bundle['open_orders'])
            assert lp.register_account_snapshot(terminal_bundle)['state'] == 'registered'
            assert store.lp_session(sid)['state'] == 'complete'
            lp.reconcile_facts(sid)
            terminal_before = deepcopy(store.lp_session(sid))
            assert terminal_before['state'] == 'complete'
            assert terminal_before['entry_order_id'] == original_oid
            assert terminal_before['order_history'][original_oid]['status'] == 'CANCELED'
            assert Decimal(terminal_before['order_history'][original_oid]['size_matched']) == 0
            assert terminal_before['position_reconciled'] is True
            assert terminal_before['stop_requested'] is True
            assert 'submit_status' not in terminal_before and terminal_before['submit_stage'] == 'sending'
            assert all(a in store.lp_actions(sid) for a in immutable_audit)
            immutable_audit = deepcopy(store.lp_actions(sid))
            original_intent, = [i for i in engine.lp_auto_state()['intents'] if i['intent_id'] == request_id]
            original_intent = deepcopy(original_intent)

        diagnostics['stage'] = 'recover_original_request'
        financial_before = deepcopy(engine.lp_auto_state()['funds'])
        captured_intent, = [i for i in engine.lp_auto_state()['intents'] if i['intent_id'] == request_id]
        captured_intent = deepcopy(captured_intent)
        assert captured_intent['session_id'] == sid and captured_intent['intent_id'] == request_id
        diagnostics['captured_pre_recovery_intent'] = deepcopy(captured_intent)
        engine.lp_auto_set_desired_running(False)
        phase[0] = 'metadata_recovery'
        monkeypatch.setattr(pool, 'NOW', datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline']))
        scheduler.request_check()
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        current_intent, = [i for i in state['intents'] if i['intent_id'] == request_id]
        assert current_intent == captured_intent
        canonical = store.lp_session(sid)
        assert canonical['session_id'] == captured_intent['session_id']
        assert canonical['idempotency_key'] == 'lp-auto:' + request_id
        assert canonical['entry_order_id'] == original_oid
        assert original_oid in canonical['owned_order_ids']
        assert canonical['order_history'][original_oid]['side'] == 'BUY'
        assert canonical['order_history'][original_oid]['token_id'] == captured_intent['token_id']
        assert Decimal(canonical['order_history'][original_oid]['price']) == Decimal(captured_intent['price'])
        original_history = canonical['order_history'][original_oid]
        original_quantity = original_history['original_size'] if 'original_size' in original_history else original_history['quantity']
        assert Decimal(original_quantity) == Decimal(captured_intent['quantity']) == 20
        assert audit['session_id'] == sid and audit['order_id'] == original_oid
        assert audit in store.lp_actions(sid)
        assert state['last_round']['round_id'] != original['round_id']
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        assert state['last_round']['actions'] == []
        assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G'])
        assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20
            and p['post_only'] is True for p in exchange.posts)
        assert any(r['phase'] == 'metadata_recovery' and r['failed'] for r in full_reads)
        assert not any(r['active_plan'] for r in full_reads)
        diagnostics['current_result_recovery'] = deepcopy(state)
        recovered = store.lp_session(sid)
        assert recovered['submit_status'] == 'accepted' and recovered['submit_stage'] == 'receipt_received'
        assert recovered['submit_receipt_at'] == audit['submit_receipt_at']
        assert recovered['submit_finished_at'] == audit['submit_finished_at']
        assert state['funds']['as_of'] == financial_before['as_of']
        assert recovered['reservation_coverage'] == bound['reservation_coverage']
        preserved_fields = ('state', 'stop_requested', 'stop_loss_latched', 'entry_cancel_requested',
            'review_status', 'queue_protection', 'order_history', 'owned_order_ids',
            'reservation_coverage', 'account_coverage_retired', 'position_reconciled',
            'buy_filled_quantity', 'buy_cost', 'buy_fees', 'sold_quantity', 'sold_revenue',
            'sell_fees', 'residual_quantity', 'orders_terminal', 'fee_status')
        if outcome == 'live':
            assert recovered['state'] == 'entry_open'
            assert recovered['queue_protection'] == original_protection
            assert live_orders(exchange)['B'] == original_oid and exchange.cancels == []
        else:
            for key in preserved_fields:
                assert recovered.get(key) == terminal_before.get(key), key
            assert set(live_orders(exchange)) == {'C', 'E', 'F', 'G'}
            assert exchange.cancels == [original_oid]
            assert next(i for i in state['intents'] if i['intent_id'] == request_id) == original_intent
        assert store.lp_actions(sid) == immutable_audit
        assert {key: store.lp_actions(key) for key in sibling_audits} == sibling_audits
        assert state['slots']['occupied'] == (5 if outcome == 'live' else 4)
        assert Decimal(state['funds']['buy_reserved_usd']) == (Decimal('39') if outcome == 'live' else Decimal('31.20'))

        diagnostics['stage'] = 'duplicate_wakes_and_restart'
        completed_round = deepcopy(state['last_round'])
        posts = deepcopy(exchange.posts)
        cancels = deepcopy(exchange.cancels)
        for _ in range(2):
            scheduler.request_check()
            assert not scheduler.run_due()
            engine.lp_auto_run_once()
        restarted = restart_engine(engine, exchange)
        restored_scheduler = LPAutoScheduler(restarted, clock=lambda: pool.NOW)
        restored_scheduler.request_check()
        restored_scheduler.run_due()
        assert restarted.lp_auto_state()['last_round'] == completed_round
        assert exchange.posts == posts and exchange.cancels == cancels
        assert store.lp_actions(sid) == immutable_audit
        if outcome == 'live':
            assert store.lp_session(sid)['queue_protection'] == original_protection
        else:
            for key in preserved_fields:
                assert store.lp_session(sid).get(key) == terminal_before.get(key), key
        assert len([s for s in store.lp_sessions() if s['token_id'] == 'B']) == 1

        if outcome == 'terminal_zero':
            diagnostics['stage'] = 'fresh_next_round_after_full_interval'
            completed = datetime.fromisoformat(completed_round['completed_at'])
            assert datetime.fromisoformat(restarted.lp_auto_state()['plan_wait']['deadline']) == completed + timedelta(seconds=30)
            monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=29))
            restored_scheduler.request_check()
            assert not restored_scheduler.run_due()
            assert restarted.lp_auto_state()['last_round'] == completed_round
            assert exchange.posts == posts and exchange.cancels == cancels
            monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=30))
            phase[0] = 'healthy_next_current'
            prepare_current_candidates(restarted._lp)
            restarted.lp_auto_set_desired_running(True)
            assert restored_scheduler.run_due()
            new_state = restarted.lp_auto_state()
            assert new_state['last_round']['round_id'] != original['round_id']
            assert new_state['last_round']['completed_at'] and new_state['active_plan'] is None
            new_action, = [a for a in new_state['last_round']['actions'] if a['kind'] == 'buy']
            assert new_action['state'] == 'success' and new_action['action_id'] != first['action_id']
            assert new_action.get('request_id', new_action['action_id']) != request_id
            assert new_action['session_id'] != sid and new_action['order_id'] != original_oid
            assert Counter(p['token_id'] for p in exchange.posts) == Counter(['B', 'C', 'E', 'F', 'G', 'B'])
            assert all(Decimal(p['price']) == Decimal('.39') and Decimal(p['quantity']) == 20
                for p in exchange.posts)
            assert live_orders(exchange)['B'] == new_action['order_id'] and exchange.cancels == [original_oid]
            assert next(o for o in exchange.orders if o['order_id'] == original_oid)['status'] == 'CANCELED'
            assert store.lp_actions(sid) == immutable_audit
            assert new_state['slots']['occupied'] == 5 and Decimal(new_state['funds']['buy_reserved_usd']) == 39
            for key in preserved_fields:
                assert store.lp_session(sid).get(key) == terminal_before.get(key), key
        assert {key: store.lp_actions(key) for key in sibling_audits} == sibling_audits
        diagnostics['stage'] = 'complete'
    finally:
        if held is not None:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
        diagnostics.update(clock=pool.NOW.isoformat(), original=original, full_reads=full_reads, result_packets=result_packets,
            session=store.lp_session(sid) if sid else None,
            audits=store.lp_actions(sid) if sid else [],
            posts=exchange.posts, cancels=exchange.cancels, state=engine.lp_auto_state())
        (tmp_path/'already-bound-receipt-diagnostics.json').write_text(
            json.dumps(diagnostics, indent=2, default=str) + '\n')


def test_bound_terminal_receipt_recovers_with_unavailable_exact_lookup(runtime, monkeypatch, tmp_path):
    """Durable canceled-zero authority survives a real SDK wrapped null."""
    import json
    from collections import Counter
    from time import monotonic
    from polymarket.models.clob.account import OpenOrder
    from polymarket.models.clob.cancel import CancelOrdersResponse
    from polymarket.models.clob.order_response import AcceptedOrder
    from tests.test_lp_auto_refill_contract import RefillPublic, prepare
    from tests.test_lp_account_reservation_reconciliation import _refill_identity
    from tests.test_lp_order_registration_contract import _open_order

    monkeypatch.setattr(pool, 'NOW', pool.NOW)  # Restore the SDK/runtime business clock.
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24'}
    store, adapter, account, lp, engine, _ = prepare(
        runtime, budget='16', target=2, count=2, public=public)
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    venue, receipts, packets, lookups, finances = {}, [], [], [], []
    posts, cancels = account.posts, account.cancels
    sign = account.create_limit_order
    held, sid, original = None, None, None
    unavailable, incomplete = [False], [False]
    phase, diagnostics = ['setup'], {}
    first_venue_row = Event()

    def install_external_carriers(current_adapter, current_account, current_engine):
        balance = current_account.get_balance_allowance
        reader = current_adapter.lp_order_result_snapshot
        current_account.posts, current_account.cancels = posts, cancels

        def observed_balance(**kwargs):
            observed = dict(phase=phase[0], at=runtime.clock[0],
                active_plan=bool(current_engine.lp_auto_state()['active_plan']))
            finances.append(observed)
            if phase[0] == 'auto_recovery':
                observed['failed'] = True
                raise TimeoutError('Current planning balance unavailable; exact request result remains independent')
            observed['failed'] = False
            return balance(**kwargs)

        def open_orders(**kwargs):
            current_account.order_reads += 1
            if incomplete[0]:
                return None
            return tuple(order for order in venue.values() if order.status == 'LIVE')

        def exact(*, order_id):
            lookups.append(dict(phase=phase[0], order_id=order_id))
            if unavailable[0] and order_id == 'terminal-receipt-1':
                return OpenOrder.parse_response(None)
            return venue[order_id]

        def post(signed):
            nonlocal held
            index = next(i for i in (1, 2) if signed.token_id == _refill_identity(i)[2])
            original_first = index == 1 and not first_venue_row.is_set()
            if index == 2 and not first_venue_row.is_set():
                assert first_venue_row.wait(5), 'Actual target1 venue-row watchdog'
            runtime.clock[0] += timedelta(seconds=1)
            posts.append(signed)
            oid = f'terminal-receipt-{len(posts)}'
            quantity = Decimal(signed.taker_amount) / 1000000
            price = Decimal(signed.maker_amount) / signed.taker_amount
            venue[oid] = _open_order(oid, 'BUY', price=str(price), original=str(quantity),
                token_id=signed.token_id, order_type='GTD', expiration=signed.expiration).model_copy(
                    update={'market': _refill_identity(index)[1], 'condition_id': _refill_identity(index)[1]})
            current_account.orders = tuple(o for o in venue.values() if o.status == 'LIVE')
            result = AcceptedOrder(order_id=oid, status='live',
                making_amount=Decimal('8'), taking_amount=Decimal('20'),
                trade_ids=(), transactions_hashes=())
            receipts.append(result)
            if original_first:
                first_venue_row.set()
                watchdog = monotonic() + 5
                while True:
                    public_state = current_engine.lp_auto_state()
                    sibling, = [a for a in public_state['last_round']['actions']
                        if a['kind'] == 'buy' and a['token_id'] == _refill_identity(2)[2]]
                    sibling_session = store.lp_session(sibling['session_id']) if sibling.get('session_id') else None
                    sibling_audits = store.lp_actions(sibling['session_id']) if sibling_session else []
                    if (sibling['state'] == 'success' and sibling_session.get('submit_status') == 'accepted'
                            and sibling_session.get('entry_order_id') == sibling.get('order_id')
                            and len(sibling_audits) == 1 and sibling_audits[0]['state'] == 'accepted'
                            and sibling_audits[0]['submit_receipt_at'] and sibling_audits[0]['submit_finished_at']):
                        diagnostics['sibling_publication'] = deepcopy(dict(action=sibling,
                            session=sibling_session, audits=sibling_audits, posts=posts))
                        break
                    assert monotonic() < watchdog, 'Original target2 public completion watchdog'
                    Event().wait(.005)
                held = current_engine._lock_path.open('a+')
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return result

        def cancel(*, order_ids):
            cancels.append(order_ids)
            for oid in order_ids:
                assert venue[oid].size_matched == 0
                venue[oid] = venue[oid].model_copy(update={'status': 'CANCELED'})
            current_account.orders = tuple(o for o in venue.values() if o.status == 'LIVE')
            return CancelOrdersResponse(canceled=order_ids, not_canceled={})

        def observed_results(request):
            result = reader(request)
            packets.append(dict(phase=phase[0], request=deepcopy(request), result=deepcopy(result)))
            return result

        monkeypatch.setattr(current_account, 'get_balance_allowance', observed_balance)
        monkeypatch.setattr(current_account, 'list_open_orders', open_orders)
        monkeypatch.setattr(current_account, 'get_order', exact, raising=False)
        monkeypatch.setattr(current_account, 'create_limit_order', sign, raising=False)
        monkeypatch.setattr(current_account, 'post_order', post)
        monkeypatch.setattr(current_account, 'cancel_orders', cancel)
        monkeypatch.setattr(current_adapter, 'lp_order_result_snapshot', observed_results)


    def prepare_current_candidates(service, current_adapter):
        import io
        opposite = f'0x{301:064x}'
        original_book = public.get_order_book
        def cold_book(*, token_id):
            book = original_book(token_id=token_id)
            if token_id == opposite:
                book = book.model_copy(update={
                    'bids': (book.bids[0].model_copy(update={'price': Decimal('.60')}),
                        book.bids[1].model_copy(update={'price': Decimal('.59')})),
                    'asks': (book.asks[0].model_copy(update={'price': Decimal('.62')}),)})
                diagnostics['opposite_cold_book'] = dict(token_id=token_id,
                    bids=[dict(price=level.price,size=level.size) for level in book.bids],
                    asks=[dict(price=level.price,size=level.size) for level in book.asks])
            return book
        monkeypatch.setattr(public, 'get_order_book', cold_book)
        def response(request, **kwargs):
            if request.data:
                body = json.loads(request.data)
                payload = {'history': {token: [{'t': t, 'p': .6 if token == opposite else .4}
                    for t in range(body['start_ts'], body['end_ts'] + 1, 60)] for token in body['markets']}}
            else:
                payload = {'data': [{'condition_id': _refill_identity(i)[1], 'market_competitiveness': 10}
                    for i in range(1, 7)], 'next_cursor': 'LTE='}
            return io.BytesIO(json.dumps(payload).encode())
        monkeypatch.setattr(current_adapter, '_urlopen_fn', response)
        prepared = service.refresh_price_history()
        competition = service.refresh_competition_cache()
        qualified = service.refresh_candidates(force=True)
        diagnostics['cold_public_preparation'] = deepcopy(dict(prepared=prepared, competition=competition,
            qualified=qualified))
        assert prepared['preparation_outcome'] == 'success', prepared
        assert competition['state'] == 'known'
        row, = [row for row in qualified['candidates'] if row['condition_id'] == _refill_identity(1)[1]]
        assert row['selected_direction']['token_id'] == _refill_identity(1)[2]
        assert row['selected_direction']['eligible'] is True

    install_external_carriers(adapter, account, engine)
    preserved_fields = ('state', 'stop_requested', 'stop_loss_latched', 'entry_cancel_requested',
        'review_status', 'queue_protection', 'order_history', 'owned_order_ids',
        'reservation_coverage', 'account_coverage_retired', 'position_reconciled',
        'buy_filled_quantity', 'buy_cost', 'buy_fees', 'sold_quantity', 'sold_revenue',
        'sell_fees', 'residual_quantity', 'orders_terminal', 'fee_status', 'fees',
        'account_checked_at', 'facts_checked_at', 'financial_block_reason', 'trade_events',
        'verified_order_fills')
    try:
        diagnostics['stage'] = 'original_real_receipt'
        assert scheduler.run_due()
        sent = engine.lp_auto_state()
        original = deepcopy(sent['last_round'])
        targets = deepcopy(sent['last_round']['targets'])
        first, = [a for a in original['actions'] if a['kind'] == 'buy' and a['token_id'] == _refill_identity(1)[2]]
        second, = [a for a in original['actions'] if a['kind'] == 'buy' and a['token_id'] == _refill_identity(2)[2]]
        assert sent['active_plan'] is None and original['completed_at']
        assert first['state'] == 'unknown' and second['state'] == 'success'
        assert datetime.fromisoformat(sent['plan_wait']['deadline']) == datetime.fromisoformat(original['completed_at']) + timedelta(seconds=30)
        diagnostics['original_local_completion'] = deepcopy(sent)
        sibling_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id']))
            for s in store.lp_sessions() if s['idempotency_key'] != 'lp-auto:' + first.get('request_id', first['action_id'])}
        assert [target['token_id'] for target in targets] == [_refill_identity(i)[2] for i in (1, 2)]
        assert all(Decimal(target['price']) == Decimal('.40') and Decimal(target['quantity']) == 20
            for target in targets)
        request_id = first.get('request_id', first['action_id'])
        session, = [s for s in store.lp_sessions() if s['idempotency_key'] == 'lp-auto:' + request_id]
        sid = session['session_id']
        audit, = store.lp_actions(sid)
        assert audit['state'] == 'accepted' and audit['post_started'] is True
        assert audit['order_id'] == receipts[0].order_id == 'terminal-receipt-1'
        assert audit['submit_stage'] == 'receipt_received'
        assert audit['submit_receipt_at'] and audit['submit_finished_at']
        assert session['submit_stage'] == 'sending' and session.get('submit_status') is None
        assert not session.get('entry_order_id') and len(posts) == 2 and cancels == []
        accepted_audit = deepcopy(audit)

        diagnostics['stage'] = 'public_registration_and_terminal_proof'
        runtime.clock[0] += timedelta(seconds=1)
        bundle = adapter.lp_account_snapshot_shared(max_age_seconds=-1,
            trade_generation_provider=store.lp_trade_generation)
        assert bundle['read_started_at'] > datetime.fromisoformat(audit['submit_finished_at'])
        assert lp.register_account_snapshot(bundle)['state'] == 'registered'
        bound = deepcopy(store.lp_session(sid))
        assert bound['entry_order_id'] == receipts[0].order_id
        assert bound.get('submit_status') is None and bound['submit_stage'] == 'sending'
        assert bound['reservation_coverage']['state'] == 'covered'
        assert bound['order_history']['terminal-receipt-1']['status'] == 'LIVE'
        assert store.lp_actions(sid) == [accepted_audit]
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
        held = None
        assert engine.lp_stop(sid)['state'] == 'review'
        assert cancels == [('terminal-receipt-1',)]
        assert venue['terminal-receipt-1'].status == 'CANCELED'
        assert venue['terminal-receipt-1'].size_matched == 0
        runtime.clock[0] += timedelta(seconds=1)
        bundle = adapter.lp_account_snapshot_shared(max_age_seconds=-1,
            trade_generation_provider=store.lp_trade_generation)
        assert lp.register_account_snapshot(bundle)['state'] == 'registered'
        lp.reconcile_facts(sid)
        terminal = deepcopy(store.lp_session(sid))
        assert terminal['state'] == 'complete' and terminal['orders_terminal'] is True
        assert terminal['position_reconciled'] is True and terminal['fee_status'] == 'known'
        assert terminal['entry_order_id'] == receipts[0].order_id
        assert terminal['stop_requested'] is True
        assert terminal.get('submit_status') is None and terminal['submit_stage'] == 'sending'
        history = terminal['order_history']['terminal-receipt-1']
        assert history['status'] == 'CANCELED' and Decimal(history['size_matched']) == 0
        assert history['side'] == 'BUY' and history['token_id'] == _refill_identity(1)[2]
        assert Decimal(history['price']) == Decimal('.40') and Decimal(history['original_size']) == 20
        for key in ('buy_filled_quantity', 'sold_quantity', 'residual_quantity', 'buy_cost', 'fees'):
            assert Decimal(terminal[key]) == 0, key
        assert not terminal.get('facts_error') and not terminal.get('financial_block_reason')
        assert datetime.fromisoformat(terminal['facts_checked_at']) > datetime.fromisoformat(audit['submit_finished_at'])
        immutable_audits = deepcopy(store.lp_actions(sid))
        assert accepted_audit in immutable_audits
        financial_asof = engine.lp_auto_state()['funds']['as_of']
        assert financial_asof

        captured_intent, = [i for i in engine.lp_auto_state()['intents'] if i['intent_id'] == request_id]
        captured_intent = deepcopy(captured_intent)
        assert captured_intent['session_id'] == sid and captured_intent['intent_id'] == request_id
        diagnostics['captured_pre_recovery_intent'] = deepcopy(captured_intent)
        engine.lp_auto_set_desired_running(False)
        diagnostics['stage'] = 'incomplete_current_negative'
        phase[0] = 'auto_recovery'
        unavailable[0], incomplete[0] = True, True
        runtime.clock[0] = datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline'])
        assert scheduler.run_due()
        waiting = engine.lp_auto_state()
        assert waiting['active_plan'] is None and waiting['last_round']['completed_at']
        assert store.lp_session(sid).get('submit_status') is None
        assert len(posts) == 2 and cancels == [('terminal-receipt-1',)]
        assert waiting['plan_wait']['kind'] == 'round'
        failed_at = runtime.clock[0]
        assert datetime.fromisoformat(waiting['plan_wait']['deadline']) == failed_at + timedelta(seconds=30)
        reads = (len(packets), len(lookups), len(finances), account.order_reads, account.trade_reads, account.position_reads)
        for seconds in (1, 29):
            runtime.clock[0] = failed_at + timedelta(seconds=seconds)
            scheduler.request_check()
            assert not scheduler.run_due()
            engine.lp_auto_run_once()
            assert (len(packets), len(lookups), len(finances), account.order_reads, account.trade_reads, account.position_reads) == reads
            assert len(posts) == 2 and cancels == [('terminal-receipt-1',)]
            assert store.lp_session(sid).get('submit_status') is None

        diagnostics['stage'] = 'complete_current_null_positive'
        incomplete[0] = False
        runtime.clock[0] = failed_at + timedelta(seconds=30)
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        packet = packets[-1]
        assert 'terminal-receipt-1' in packet['request']['owned_order_ids']
        result = packet['result']
        assert result['order_read_errors']['terminal-receipt-1'] == 'order_read_failed'
        assert not any(row['order_id'] == 'terminal-receipt-1' for row in result['orders'] + result['open_orders'])
        assert all(result[key] is True for key in ('authenticated', 'open_orders_complete',
            'pagination_complete', 'trades_complete', 'positions_complete'))
        current_intent, = [i for i in state['intents'] if i['intent_id'] == request_id]
        assert current_intent == captured_intent
        canonical = store.lp_session(sid)
        assert canonical['session_id'] == captured_intent['session_id']
        assert canonical['idempotency_key'] == 'lp-auto:' + request_id
        assert canonical['entry_order_id'] == receipts[0].order_id
        assert receipts[0].order_id in canonical['owned_order_ids']
        assert canonical['order_history'][receipts[0].order_id]['side'] == 'BUY'
        assert canonical['order_history'][receipts[0].order_id]['token_id'] == captured_intent['token_id']
        assert Decimal(canonical['order_history'][receipts[0].order_id]['price']) == Decimal(captured_intent['price'])
        assert Decimal(canonical['order_history'][receipts[0].order_id]['original_size']) == Decimal(captured_intent['quantity'])
        assert audit['session_id'] == sid and audit['order_id'] == receipts[0].order_id
        assert audit in store.lp_actions(sid)
        assert state['last_round']['round_id'] != original['round_id']
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        assert state['last_round']['actions'] == []
        assert Counter(p.token_id for p in posts) == Counter(_refill_identity(i)[2] for i in (1, 2))
        assert all(p.maker_amount == 8000000 and p.taker_amount == 20000000
            and p.post_only is True for p in posts)
        diagnostics['current_result_recovery'] = deepcopy(dict(state=state, original_intent=current_intent))
        assert {key: store.lp_actions(key) for key in sibling_audits} == sibling_audits
        recovered = store.lp_session(sid)
        assert recovered['submit_status'] == 'accepted' and recovered['submit_stage'] == 'receipt_received'
        assert recovered['submit_receipt_at'] == accepted_audit['submit_receipt_at']
        assert recovered['submit_finished_at'] == accepted_audit['submit_finished_at']
        for key in preserved_fields:
            assert recovered.get(key) == terminal.get(key), key
        assert store.lp_actions(sid) == immutable_audits and cancels == [('terminal-receipt-1',)]
        assert state['funds']['as_of'] == financial_asof
        assert [call for call in finances if call['phase'] == 'auto_recovery' and call['failed']]
        assert not [call for call in finances if call['active_plan']]

        diagnostics['stage'] = 'duplicate_wakes_and_public_restart'
        completed = deepcopy(state['last_round'])
        for _ in range(2):
            scheduler.request_check()
            assert not scheduler.run_due()
            engine.lp_auto_run_once()
        restart_store, restart_adapter, restart_account, restart_lp, restarted = runtime(
            orders=tuple(o for o in venue.values() if o.status == 'LIVE'), public_client=public)
        install_external_carriers(restart_adapter, restart_account, restarted)
        restored_scheduler = LPAutoScheduler(restarted, clock=lambda: runtime.clock[0])
        restored_scheduler.request_check()
        restored_scheduler.run_due()
        restarted.lp_auto_run_once()
        assert restarted.lp_auto_state()['last_round'] == completed
        assert len(posts) == 2 and cancels == [('terminal-receipt-1',)]
        assert restart_store.lp_actions(sid) == immutable_audits
        for key in preserved_fields:
            assert restart_store.lp_session(sid).get(key) == terminal.get(key), key
        assert restart_store.lp_session(sid)['submit_receipt_at'] == accepted_audit['submit_receipt_at']

        diagnostics['stage'] = 'full_interval_and_new_identity'
        completed_at = datetime.fromisoformat(completed['completed_at'])
        assert datetime.fromisoformat(restarted.lp_auto_state()['plan_wait']['deadline']) == completed_at + timedelta(seconds=30)
        runtime.clock[0] = completed_at + timedelta(seconds=29)
        restored_scheduler.request_check()
        assert not restored_scheduler.run_due()
        assert len(posts) == 2 and restarted.lp_auto_state()['last_round'] == completed
        runtime.clock[0] = completed_at + timedelta(seconds=30)
        phase[0] = 'next_round_setup'
        prepare_current_candidates(restart_lp, restart_adapter)
        restarted.lp_auto_set_desired_running(True)
        phase[0] = 'next_round'
        assert restored_scheduler.run_due()
        new_state = restarted.lp_auto_state()
        assert new_state['last_round']['round_id'] != original['round_id']
        new_action, = [a for a in new_state['last_round']['actions'] if a['kind'] == 'buy']
        assert new_action['state'] == 'success' and new_action['action_id'] != first['action_id']
        assert new_action.get('request_id', new_action['action_id']) != request_id
        assert new_action['session_id'] != sid and new_action['order_id'] == 'terminal-receipt-3'
        assert new_state['last_round']['completed_at'] and new_state['active_plan'] is None
        assert Counter(p.token_id for p in posts) == Counter(_refill_identity(i)[2] for i in (1, 2, 1))
        assert all(p.maker_amount == 8000000 and p.taker_amount == 20000000 for p in posts)
        assert {o.id for o in venue.values() if o.status == 'LIVE'} == {'terminal-receipt-2', 'terminal-receipt-3'}
        assert new_state['slots']['occupied'] == 2 and Decimal(new_state['funds']['buy_reserved_usd']) == 16
        assert cancels == [('terminal-receipt-1',)] and venue['terminal-receipt-1'].status == 'CANCELED'
        assert restart_store.lp_actions(sid) == immutable_audits
        for key in preserved_fields:
            assert restart_store.lp_session(sid).get(key) == terminal.get(key), key
        assert {key: restart_store.lp_actions(key) for key in sibling_audits} == sibling_audits
        diagnostics['stage'] = 'complete'
    finally:
        first_venue_row.set()
        if held is not None:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
        diagnostics.update(clock=runtime.clock[0], original=original, packets=packets, lookups=lookups,
            finances=finances, posts=posts, cancels=cancels, session=store.lp_session(sid) if sid else None,
            audits=store.lp_actions(sid) if sid else [], state=engine.lp_auto_state())
        (tmp_path/'bound-terminal-null-diagnostics.json').write_text(json.dumps(diagnostics, indent=2, default=str) + '\n')


def test_bound_live_unknown_metadata_recovers_original_accepted_receipt(runtime, monkeypatch, tmp_path):
    """Public late-receipt reconciliation can leave known LIVE metadata unknown."""
    import json
    from collections import Counter
    from time import monotonic
    from polymarket.models.clob.order_response import AcceptedOrder
    from tests.test_lp_auto_refill_contract import RefillPublic, prepare
    from tests.test_lp_account_reservation_reconciliation import _refill_identity
    from tests.test_lp_order_registration_contract import _open_order

    monkeypatch.setattr(pool, 'NOW', pool.NOW)  # Restore the SDK/runtime business clock.
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24'}
    store, adapter, account, lp, engine, _ = prepare(
        runtime, budget='16', target=2, count=2, public=public)
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    venue, receipts, packets, finances = {}, [], [], []
    posts, cancels = account.posts, account.cancels
    sign = account.create_limit_order
    held, sid, original = None, None, None
    phase, diagnostics = ['setup'], {}
    first_venue_row = Event()

    def install_external_carriers(current_adapter, current_account, current_engine):
        balance = current_account.get_balance_allowance
        reader = current_adapter.lp_order_result_snapshot
        current_account.posts, current_account.cancels = posts, cancels

        def observed_balance(**kwargs):
            observed = dict(phase=phase[0], at=runtime.clock[0],
                active_plan=bool(current_engine.lp_auto_state()['active_plan']))
            finances.append(observed)
            if phase[0] == 'auto_recovery':
                observed['failed'] = True
                raise TimeoutError('Current planning balance unavailable; exact request result remains independent')
            observed['failed'] = False
            return balance(**kwargs)

        def open_orders(**kwargs):
            current_account.order_reads += 1
            return tuple(venue.values())

        def exact(*, order_id):
            return venue[order_id]

        def post(signed):
            nonlocal held
            index = next(i for i in (1, 2) if signed.token_id == _refill_identity(i)[2])
            original_first = index == 1 and not first_venue_row.is_set()
            if index == 2 and not first_venue_row.is_set():
                assert first_venue_row.wait(5), 'Actual target1 venue-row watchdog'
            runtime.clock[0] += timedelta(seconds=1)
            posts.append(signed)
            oid = f'live-receipt-{len(posts)}'
            quantity = Decimal(signed.taker_amount) / 1000000
            price = Decimal(signed.maker_amount) / signed.taker_amount
            venue[oid] = _open_order(oid, 'BUY', price=str(price), original=str(quantity),
                token_id=signed.token_id, order_type='GTD', expiration=signed.expiration).model_copy(
                    update={'market': _refill_identity(index)[1], 'condition_id': _refill_identity(index)[1]})
            current_account.orders = tuple(venue.values())
            result = AcceptedOrder(order_id=oid, status='live',
                making_amount=Decimal('8'), taking_amount=Decimal('20'),
                trade_ids=(), transactions_hashes=())
            receipts.append(result)
            if original_first:
                first_venue_row.set()
                watchdog = monotonic() + 5
                while True:
                    public_state = current_engine.lp_auto_state()
                    sibling, = [a for a in public_state['last_round']['actions']
                        if a['kind'] == 'buy' and a['token_id'] == _refill_identity(2)[2]]
                    sibling_session = store.lp_session(sibling['session_id']) if sibling.get('session_id') else None
                    sibling_audits = store.lp_actions(sibling['session_id']) if sibling_session else []
                    if (sibling['state'] == 'success' and sibling_session.get('submit_status') == 'accepted'
                            and sibling_session.get('entry_order_id') == sibling.get('order_id')
                            and len(sibling_audits) == 1 and sibling_audits[0]['state'] == 'accepted'
                            and sibling_audits[0]['submit_receipt_at'] and sibling_audits[0]['submit_finished_at']):
                        diagnostics['sibling_publication'] = deepcopy(dict(action=sibling,
                            session=sibling_session, audits=sibling_audits, posts=posts))
                        break
                    assert monotonic() < watchdog, 'Original target2 public completion watchdog'
                    Event().wait(.005)
                held = current_engine._lock_path.open('a+')
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return result

        def observed_results(request):
            result = reader(request)
            packets.append(dict(phase=phase[0], request=deepcopy(request), result=deepcopy(result)))
            return result

        monkeypatch.setattr(current_account, 'get_balance_allowance', observed_balance)
        monkeypatch.setattr(current_account, 'list_open_orders', open_orders)
        monkeypatch.setattr(current_account, 'get_order', exact, raising=False)
        monkeypatch.setattr(current_account, 'create_limit_order', sign, raising=False)
        monkeypatch.setattr(current_account, 'post_order', post)
        monkeypatch.setattr(current_adapter, 'lp_order_result_snapshot', observed_results)


    def prepare_current_candidates(service, current_adapter):
        import io
        def response(request, **kwargs):
            if request.data:
                body = json.loads(request.data)
                payload = {'history': {token: [{'t': t, 'p': .4}
                    for t in range(body['start_ts'], body['end_ts'] + 1, 60)] for token in body['markets']}}
            else:
                payload = {'data': [{'condition_id': _refill_identity(i)[1], 'market_competitiveness': 10}
                    for i in range(1, 7)], 'next_cursor': 'LTE='}
            return io.BytesIO(json.dumps(payload).encode())
        monkeypatch.setattr(current_adapter, '_urlopen_fn', response)
        prepared = service.refresh_price_history()
        competition = service.refresh_competition_cache()
        qualified = service.refresh_candidates(force=True)
        diagnostics['cold_public_preparation'] = deepcopy(dict(prepared=prepared, competition=competition,
            qualified=qualified))
        assert prepared['preparation_outcome'] == 'success', prepared
        assert competition['state'] == 'known'
        excluded = qualified['funnel']['reasons']['base']
        for index in (1, 2):
            assert any(row['condition_id'] == _refill_identity(index)[1]
                and row['code'] == 'market_already_participating' for row in excluded)
        current_orders = tuple(o for o in restart_account.orders if o.status == 'LIVE')
        assert {o.id for o in current_orders} == {'live-receipt-1', 'live-receipt-2'}
        assert {o.token_id for o in current_orders} == {_refill_identity(i)[2] for i in (1, 2)}
        assert all(o.side == 'BUY' and o.price == Decimal('.40')
            and o.original_size == 20 and o.size_matched == 0 for o in current_orders)
        owned_sessions = [s for s in restart_store.lp_sessions() if s.get('entry_order_id') in {o.id for o in current_orders}]
        assert len(owned_sessions) == 2
        assert all(s['entry_order_id'] in s['owned_order_ids'] for s in owned_sessions)

    install_external_carriers(adapter, account, engine)
    preserved_fields = ('state', 'stop_requested', 'stop_loss_latched', 'entry_cancel_requested',
        'review_status', 'queue_protection', 'order_history', 'owned_order_ids',
        'reservation_coverage', 'account_coverage_retired', 'position_reconciled',
        'buy_filled_quantity', 'buy_cost', 'buy_fees', 'sold_quantity', 'sold_revenue',
        'sell_fees', 'residual_quantity', 'orders_terminal', 'fee_status', 'fees',
        'account_checked_at', 'facts_checked_at', 'financial_block_reason', 'trade_events',
        'verified_order_fills', 'submit_requested_at', 'submit_post_started_at', 'post_started')
    try:
        diagnostics['stage'] = 'original_real_receipt'
        assert scheduler.run_due()
        sent = engine.lp_auto_state()
        original = deepcopy(sent['last_round'])
        targets = deepcopy(sent['last_round']['targets'])
        first, = [a for a in original['actions'] if a['kind'] == 'buy' and a['token_id'] == _refill_identity(1)[2]]
        second, = [a for a in original['actions'] if a['kind'] == 'buy' and a['token_id'] == _refill_identity(2)[2]]
        assert sent['active_plan'] is None and original['completed_at']
        assert first['state'] == 'unknown' and second['state'] == 'success'
        assert datetime.fromisoformat(sent['plan_wait']['deadline']) == datetime.fromisoformat(original['completed_at']) + timedelta(seconds=30)
        diagnostics['original_local_completion'] = deepcopy(sent)
        sibling_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id']))
            for s in store.lp_sessions() if s['idempotency_key'] != 'lp-auto:' + first.get('request_id', first['action_id'])}
        assert [target['token_id'] for target in targets] == [_refill_identity(i)[2] for i in (1, 2)]
        assert all(Decimal(target['price']) == Decimal('.40') and Decimal(target['quantity']) == 20
            for target in targets)
        request_id = first.get('request_id', first['action_id'])
        session, = [s for s in store.lp_sessions() if s['idempotency_key'] == 'lp-auto:' + request_id]
        sid = session['session_id']
        audit, = store.lp_actions(sid)
        assert audit['state'] == 'accepted' and audit['post_started'] is True
        assert audit['order_id'] == receipts[0].order_id == 'live-receipt-1'
        assert audit['submit_stage'] == 'receipt_received'
        assert audit['submit_receipt_at'] and audit['submit_finished_at']
        assert session['submit_stage'] == 'sending' and session.get('submit_status') is None
        assert not session.get('entry_order_id') and len(posts) == 2 and cancels == []
        immutable_audits = deepcopy(store.lp_actions(sid))

        diagnostics['stage'] = 'public_late_receipt_unknown_producer'
        reconciled = lp.reconcile_facts(sid,
            apply_lock=(engine._acquire_global_lock, engine._release_global_lock))
        diagnostics['producer_error'] = reconciled[3]
        assert reconciled[3] == 'execution_lock'
        unknown = deepcopy(store.lp_session(sid))
        assert unknown['entry_order_id'] == receipts[0].order_id
        assert unknown['submit_status'] == 'unknown' and unknown['submit_stage'] == 'sending'
        assert unknown['post_started'] is True and unknown['facts_error'] == 'execution_lock'
        for key in ('submit_requested_at', 'submit_post_started_at'):
            assert unknown[key] and audit[key] and unknown[key] == audit[key], key
        assert not unknown.get('submit_receipt_at') and not unknown.get('submit_finished_at')
        assert store.lp_actions(sid) == immutable_audits
        assert held is not None and not held.closed
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
        held = None

        diagnostics['stage'] = 'public_full_registration_and_reconcile'
        runtime.clock[0] += timedelta(seconds=1)
        bundle = adapter.lp_account_snapshot_shared(max_age_seconds=-1,
            trade_generation_provider=store.lp_trade_generation)
        assert bundle['read_started_at'] > datetime.fromisoformat(audit['submit_finished_at'])
        assert lp.register_account_snapshot(bundle)['state'] == 'registered'
        reconciled = lp.reconcile_facts(sid)
        assert reconciled[3] is None, reconciled[3]
        cleared = store.lp_session(sid)
        assert cleared.get('facts_error') is None and cleared['submit_status'] == 'unknown'
        assert cleared['reservation_coverage']['state'] == 'covered'
        assert store.lp_actions(sid) == immutable_audits
        # A later real account publication resumes covered management only
        # after the preceding reconciliation clears the actual lock error.
        runtime.clock[0] += timedelta(seconds=1)
        bundle = adapter.lp_account_snapshot_shared(max_age_seconds=-1,
            trade_generation_provider=store.lp_trade_generation)
        assert lp.register_account_snapshot(bundle)['state'] == 'registered'
        bound = deepcopy(store.lp_session(sid))
        assert bound['entry_order_id'] == receipts[0].order_id
        assert bound['state'] == 'entry_open'
        assert bound['submit_status'] == 'unknown' and bound['submit_stage'] == 'sending'
        assert bound['reservation_coverage']['state'] == 'covered'
        assert bound['post_started'] is True and bound.get('facts_error') is None
        assert bound['order_history']['live-receipt-1']['status'] == 'LIVE'
        assert venue['live-receipt-1'].status == 'LIVE' and venue['live-receipt-1'].size_matched == 0
        for key in ('submit_requested_at', 'submit_post_started_at'):
            assert bound[key] and audit[key] and bound[key] == audit[key], key
        requested, started, received, finished = [datetime.fromisoformat(audit[key]) for key in
            ('submit_requested_at', 'submit_post_started_at', 'submit_receipt_at', 'submit_finished_at')]
        assert requested <= started <= received <= finished < bundle['read_started_at']
        assert not bound.get('submit_receipt_at') and not bound.get('submit_finished_at')
        assert store.lp_actions(sid) == immutable_audits and len(posts) == 2 and cancels == []
        financial_asof = engine.lp_auto_state()['funds']['as_of']
        assert financial_asof

        diagnostics['stage'] = 'recover_same_original_live_receipt'
        captured_intent, = [i for i in engine.lp_auto_state()['intents'] if i['intent_id'] == request_id]
        captured_intent = deepcopy(captured_intent)
        assert captured_intent['session_id'] == sid and captured_intent['intent_id'] == request_id
        diagnostics['captured_pre_recovery_intent'] = deepcopy(captured_intent)
        engine.lp_auto_set_desired_running(False)
        phase[0] = 'auto_recovery'
        runtime.clock[0] = datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline'])
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        result = packets[-1]['result']
        assert 'live-receipt-1' in packets[-1]['request']['owned_order_ids']
        assert all(result[key] is True for key in ('authenticated', 'open_orders_complete',
            'pagination_complete', 'trades_complete', 'positions_complete'))
        exact, = [row for row in result['orders'] if row['order_id'] == receipts[0].order_id]
        assert exact['status'] == 'LIVE' and exact['token_id'] == _refill_identity(1)[2]
        assert Decimal(exact['price']) == Decimal('.40') and Decimal(exact['original_size']) == 20
        assert result['read_started_at'] >= finished and not result['order_read_errors']
        current_intent, = [i for i in state['intents'] if i['intent_id'] == request_id]
        assert current_intent == captured_intent
        canonical = store.lp_session(sid)
        assert canonical['session_id'] == captured_intent['session_id']
        assert canonical['idempotency_key'] == 'lp-auto:' + request_id
        assert canonical['entry_order_id'] == receipts[0].order_id
        assert receipts[0].order_id in canonical['owned_order_ids']
        assert canonical['order_history'][receipts[0].order_id]['side'] == 'BUY'
        assert canonical['order_history'][receipts[0].order_id]['token_id'] == captured_intent['token_id']
        assert Decimal(canonical['order_history'][receipts[0].order_id]['price']) == Decimal(captured_intent['price'])
        assert Decimal(canonical['order_history'][receipts[0].order_id]['original_size']) == Decimal(captured_intent['quantity'])
        assert audit['session_id'] == sid and audit['order_id'] == receipts[0].order_id
        assert audit in store.lp_actions(sid)
        assert state['last_round']['round_id'] != original['round_id']
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        assert state['last_round']['actions'] == []
        assert Counter(p.token_id for p in posts) == Counter(_refill_identity(i)[2] for i in (1, 2))
        assert all(p.maker_amount == 8000000 and p.taker_amount == 20000000
            and p.post_only is True for p in posts)
        diagnostics['current_result_recovery'] = deepcopy(dict(state=state, original_intent=current_intent))
        assert {key: store.lp_actions(key) for key in sibling_audits} == sibling_audits
        recovered = store.lp_session(sid)
        assert recovered['submit_status'] == 'accepted' and recovered['submit_stage'] == 'receipt_received'
        assert recovered['submit_receipt_at'] == audit['submit_receipt_at']
        assert recovered['submit_finished_at'] == audit['submit_finished_at']
        for key in preserved_fields:
            assert recovered.get(key) == bound.get(key), key
        assert store.lp_actions(sid) == immutable_audits and cancels == []
        assert state['funds']['as_of'] == financial_asof
        assert [call for call in finances if call['phase'] == 'auto_recovery' and call['failed']]
        assert not [call for call in finances if call['active_plan']]

        diagnostics['stage'] = 'duplicate_wakes_and_public_restart'
        completed = deepcopy(state['last_round'])
        for _ in range(2):
            scheduler.request_check()
            assert not scheduler.run_due()
            engine.lp_auto_run_once()
        restart_store, restart_adapter, restart_account, restart_lp, restarted = runtime(
            orders=tuple(venue.values()), public_client=public)
        install_external_carriers(restart_adapter, restart_account, restarted)
        restored_scheduler = LPAutoScheduler(restarted, clock=lambda: runtime.clock[0])
        restored_scheduler.request_check()
        restored_scheduler.run_due()
        restarted.lp_auto_run_once()
        assert restarted.lp_auto_state()['last_round'] == completed
        assert len(posts) == 2 and cancels == [] and restart_store.lp_actions(sid) == immutable_audits
        assert restart_store.lp_session(sid)['submit_receipt_at'] == audit['submit_receipt_at']
        assert restart_store.lp_session(sid)['submit_finished_at'] == audit['submit_finished_at']
        for key in preserved_fields:
            assert restart_store.lp_session(sid).get(key) == bound.get(key), key

        diagnostics['stage'] = 'full_interval_retains_both_live_orders'
        completed_at = datetime.fromisoformat(completed['completed_at'])
        assert datetime.fromisoformat(restarted.lp_auto_state()['plan_wait']['deadline']) == completed_at + timedelta(seconds=30)
        runtime.clock[0] = completed_at + timedelta(seconds=29)
        restored_scheduler.request_check()
        assert not restored_scheduler.run_due()
        assert restarted.lp_auto_state()['last_round'] == completed and len(posts) == 2 and cancels == []
        runtime.clock[0] = completed_at + timedelta(seconds=30)
        phase[0] = 'next_round_setup'
        prepare_current_candidates(restart_lp, restart_adapter)
        restarted.lp_auto_set_desired_running(True)
        phase[0] = 'next_round'
        assert restored_scheduler.run_due()
        next_state = restarted.lp_auto_state()
        assert next_state['last_round']['round_id'] != original['round_id']
        assert not [a for a in next_state['last_round']['actions'] if a['kind'] == 'buy']
        assert {o.id for o in venue.values() if o.status == 'LIVE'} == {'live-receipt-1', 'live-receipt-2'}
        assert next_state['last_round']['completed_at'] and next_state['active_plan'] is None
        assert len(posts) == 2 and cancels == []
        assert {o.id for o in venue.values() if o.status == 'LIVE'} == {'live-receipt-1', 'live-receipt-2'}
        assert next_state['slots']['occupied'] == 2 and Decimal(next_state['funds']['buy_reserved_usd']) == 16
        assert restart_store.lp_actions(sid) == immutable_audits
        assert restart_store.lp_session(sid)['entry_order_id'] == receipts[0].order_id
        assert restart_store.lp_session(sid)['submit_status'] == 'accepted'
        assert {key: restart_store.lp_actions(key) for key in sibling_audits} == sibling_audits
        diagnostics['stage'] = 'complete'
    finally:
        first_venue_row.set()
        if held is not None:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
        diagnostics.update(clock=runtime.clock[0], original=original, packets=packets,
            finances=finances, posts=posts, cancels=cancels, session=store.lp_session(sid) if sid else None,
            audits=store.lp_actions(sid) if sid else [], state=engine.lp_auto_state())
        (tmp_path/'bound-live-unknown-diagnostics.json').write_text(json.dumps(diagnostics, indent=2, default=str) + '\n')


@pytest.mark.parametrize('lookup', ['unavailable', 'terminal_zero'])
def test_bound_unknown_terminal_receipt_recovers_original_accepted_send(runtime, monkeypatch, tmp_path, lookup):
    """The same accepted send retains authority after public unknown-to-zero completion."""
    import json
    from collections import Counter
    from time import monotonic
    from polymarket.models.clob.account import OpenOrder
    from polymarket.models.clob.cancel import CancelOrdersResponse
    from polymarket.models.clob.order_response import AcceptedOrder
    from tests.test_lp_auto_refill_contract import RefillPublic, prepare
    from tests.test_lp_account_reservation_reconciliation import _refill_identity
    from tests.test_lp_order_registration_contract import _open_order

    monkeypatch.setattr(pool, 'NOW', pool.NOW)  # Restore the SDK/runtime business clock.
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24'}
    store, adapter, account, lp, engine, _ = prepare(
        runtime, budget='16', target=2, count=2, public=public)
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    venue, receipts, packets, lookups, finances = {}, [], [], [], []
    posts, cancels = account.posts, account.cancels
    sign = account.create_limit_order
    held, sid, original = None, None, None
    unavailable, incomplete = [False], [False]
    phase, diagnostics = ['setup'], {}
    first_venue_row = Event()

    def install_external_carriers(current_adapter, current_account, current_engine):
        balance = current_account.get_balance_allowance
        reader = current_adapter.lp_order_result_snapshot
        current_account.posts, current_account.cancels = posts, cancels

        def observed_balance(**kwargs):
            observed = dict(phase=phase[0], at=runtime.clock[0],
                active_plan=bool(current_engine.lp_auto_state()['active_plan']))
            finances.append(observed)
            if phase[0] == 'auto_recovery':
                observed['failed'] = True
                raise TimeoutError('Current planning balance unavailable; exact request result remains independent')
            observed['failed'] = False
            return balance(**kwargs)

        def open_orders(**kwargs):
            current_account.order_reads += 1
            if incomplete[0]:
                return None
            return tuple(order for order in venue.values() if order.status == 'LIVE')

        def exact(*, order_id):
            lookups.append(dict(phase=phase[0], order_id=order_id))
            if lookup == 'unavailable' and unavailable[0] and order_id == 'unknown-terminal-1':
                return OpenOrder.parse_response(None)
            return venue[order_id]

        def post(signed):
            nonlocal held
            index = next(i for i in (1, 2) if signed.token_id == _refill_identity(i)[2])
            original_first = index == 1 and not first_venue_row.is_set()
            if index == 2 and not first_venue_row.is_set():
                assert first_venue_row.wait(5), 'Actual target1 venue-row watchdog'
            runtime.clock[0] += timedelta(seconds=1)
            posts.append(signed)
            oid = f'unknown-terminal-{len(posts)}'
            quantity = Decimal(signed.taker_amount) / 1000000
            price = Decimal(signed.maker_amount) / signed.taker_amount
            venue[oid] = _open_order(oid, 'BUY', price=str(price), original=str(quantity),
                token_id=signed.token_id, order_type='GTD', expiration=signed.expiration).model_copy(
                    update={'market': _refill_identity(index)[1], 'condition_id': _refill_identity(index)[1]})
            current_account.orders = tuple(o for o in venue.values() if o.status == 'LIVE')
            result = AcceptedOrder(order_id=oid, status='live',
                making_amount=Decimal('8'), taking_amount=Decimal('20'),
                trade_ids=(), transactions_hashes=())
            receipts.append(result)
            if original_first:
                first_venue_row.set()
                watchdog = monotonic() + 5
                while True:
                    public_state = current_engine.lp_auto_state()
                    sibling, = [a for a in public_state['last_round']['actions']
                        if a['kind'] == 'buy' and a['token_id'] == _refill_identity(2)[2]]
                    sibling_session = store.lp_session(sibling['session_id']) if sibling.get('session_id') else None
                    sibling_audits = store.lp_actions(sibling['session_id']) if sibling_session else []
                    if (sibling['state'] == 'success' and sibling_session.get('submit_status') == 'accepted'
                            and sibling_session.get('entry_order_id') == sibling.get('order_id')
                            and len(sibling_audits) == 1 and sibling_audits[0]['state'] == 'accepted'
                            and sibling_audits[0]['submit_receipt_at'] and sibling_audits[0]['submit_finished_at']):
                        diagnostics['sibling_publication'] = deepcopy(dict(action=sibling,
                            session=sibling_session, audits=sibling_audits, posts=posts))
                        break
                    assert monotonic() < watchdog, 'Original target2 public completion watchdog'
                    Event().wait(.005)
                held = current_engine._lock_path.open('a+')
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return result

        def cancel(*, order_ids):
            cancels.append(order_ids)
            for oid in order_ids:
                assert venue[oid].size_matched == 0
                venue[oid] = venue[oid].model_copy(update={'status': 'CANCELED'})
            current_account.orders = tuple(o for o in venue.values() if o.status == 'LIVE')
            return CancelOrdersResponse(canceled=order_ids, not_canceled={})

        def observed_results(request):
            result = reader(request)
            packets.append(dict(phase=phase[0], request=deepcopy(request), result=deepcopy(result)))
            return result

        monkeypatch.setattr(current_account, 'get_balance_allowance', observed_balance)
        monkeypatch.setattr(current_account, 'list_open_orders', open_orders)
        monkeypatch.setattr(current_account, 'get_order', exact, raising=False)
        monkeypatch.setattr(current_account, 'create_limit_order', sign, raising=False)
        monkeypatch.setattr(current_account, 'post_order', post)
        monkeypatch.setattr(current_account, 'cancel_orders', cancel)
        monkeypatch.setattr(current_adapter, 'lp_order_result_snapshot', observed_results)


    def prepare_current_candidates(service, current_adapter):
        import io
        opposite = f'0x{301:064x}'
        original_book = public.get_order_book
        def cold_book(*, token_id):
            book = original_book(token_id=token_id)
            if token_id == opposite:
                book = book.model_copy(update={
                    'bids': (book.bids[0].model_copy(update={'price': Decimal('.60')}),
                        book.bids[1].model_copy(update={'price': Decimal('.59')})),
                    'asks': (book.asks[0].model_copy(update={'price': Decimal('.62')}),)})
                diagnostics['opposite_cold_book'] = dict(token_id=token_id,
                    bids=[dict(price=level.price,size=level.size) for level in book.bids],
                    asks=[dict(price=level.price,size=level.size) for level in book.asks])
            return book
        monkeypatch.setattr(public, 'get_order_book', cold_book)
        def response(request, **kwargs):
            if request.data:
                body = json.loads(request.data)
                payload = {'history': {token: [{'t': t, 'p': .6 if token == opposite else .4}
                    for t in range(body['start_ts'], body['end_ts'] + 1, 60)] for token in body['markets']}}
            else:
                payload = {'data': [{'condition_id': _refill_identity(i)[1], 'market_competitiveness': 10}
                    for i in range(1, 7)], 'next_cursor': 'LTE='}
            return io.BytesIO(json.dumps(payload).encode())
        monkeypatch.setattr(current_adapter, '_urlopen_fn', response)
        prepared = service.refresh_price_history()
        competition = service.refresh_competition_cache()
        qualified = service.refresh_candidates(force=True)
        diagnostics['cold_public_preparation'] = deepcopy(dict(prepared=prepared, competition=competition,
            qualified=qualified))
        assert prepared['preparation_outcome'] == 'success', prepared
        assert competition['state'] == 'known'
        row, = [row for row in qualified['candidates'] if row['condition_id'] == _refill_identity(1)[1]]
        assert row['selected_direction']['token_id'] == _refill_identity(1)[2]
        assert row['selected_direction']['eligible'] is True

    install_external_carriers(adapter, account, engine)
    preserved_fields = ('state', 'stop_requested', 'stop_loss_latched', 'entry_cancel_requested',
        'review_status', 'queue_protection', 'order_history', 'owned_order_ids',
        'reservation_coverage', 'account_coverage_retired', 'position_reconciled',
        'buy_filled_quantity', 'buy_cost', 'buy_fees', 'sold_quantity', 'sold_revenue',
        'sell_fees', 'residual_quantity', 'orders_terminal', 'fee_status', 'fees',
        'account_checked_at', 'facts_checked_at', 'financial_block_reason', 'trade_events',
        'verified_order_fills', 'submit_requested_at', 'submit_post_started_at', 'post_started')
    try:
        diagnostics['stage'] = 'original_real_receipt'
        assert scheduler.run_due()
        sent = engine.lp_auto_state()
        original = deepcopy(sent['last_round'])
        targets = deepcopy(sent['last_round']['targets'])
        first, = [a for a in original['actions'] if a['kind'] == 'buy' and a['token_id'] == _refill_identity(1)[2]]
        second, = [a for a in original['actions'] if a['kind'] == 'buy' and a['token_id'] == _refill_identity(2)[2]]
        assert sent['active_plan'] is None and original['completed_at']
        assert first['state'] == 'unknown' and second['state'] == 'success'
        assert datetime.fromisoformat(sent['plan_wait']['deadline']) == datetime.fromisoformat(original['completed_at']) + timedelta(seconds=30)
        diagnostics['original_local_completion'] = deepcopy(sent)
        sibling_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id']))
            for s in store.lp_sessions() if s['idempotency_key'] != 'lp-auto:' + first.get('request_id', first['action_id'])}
        assert [target['token_id'] for target in targets] == [_refill_identity(i)[2] for i in (1, 2)]
        assert all(Decimal(target['price']) == Decimal('.40') and Decimal(target['quantity']) == 20
            for target in targets)
        request_id = first.get('request_id', first['action_id'])
        session, = [s for s in store.lp_sessions() if s['idempotency_key'] == 'lp-auto:' + request_id]
        sid = session['session_id']
        audit, = store.lp_actions(sid)
        assert audit['state'] == 'accepted' and audit['post_started'] is True
        assert audit['order_id'] == receipts[0].order_id == 'unknown-terminal-1'
        assert audit['submit_stage'] == 'receipt_received'
        assert audit['submit_receipt_at'] and audit['submit_finished_at']
        assert session['submit_stage'] == 'sending' and session.get('submit_status') is None
        assert not session.get('entry_order_id') and len(posts) == 2 and cancels == []
        accepted_audit = deepcopy(audit)

        immutable_audits = deepcopy(store.lp_actions(sid))
        diagnostics['stage'] = 'public_late_receipt_unknown_producer'
        reconciled = lp.reconcile_facts(sid,
            apply_lock=(engine._acquire_global_lock, engine._release_global_lock))
        diagnostics['producer_error'] = reconciled[3]
        assert reconciled[3] == 'execution_lock'
        unknown = deepcopy(store.lp_session(sid))
        assert unknown['entry_order_id'] == receipts[0].order_id
        assert unknown['submit_status'] == 'unknown' and unknown['submit_stage'] == 'sending'
        assert unknown['post_started'] is True and unknown['facts_error'] == 'execution_lock'
        for key in ('submit_requested_at', 'submit_post_started_at'):
            assert unknown[key] and audit[key] and unknown[key] == audit[key], key
        assert not unknown.get('submit_receipt_at') and not unknown.get('submit_finished_at')
        assert store.lp_actions(sid) == immutable_audits
        assert held is not None and not held.closed
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
        held = None

        diagnostics['stage'] = 'public_full_registration_and_reconcile'
        runtime.clock[0] += timedelta(seconds=1)
        bundle = adapter.lp_account_snapshot_shared(max_age_seconds=-1,
            trade_generation_provider=store.lp_trade_generation)
        assert bundle['read_started_at'] > datetime.fromisoformat(audit['submit_finished_at'])
        assert lp.register_account_snapshot(bundle)['state'] == 'registered'
        reconciled = lp.reconcile_facts(sid)
        assert reconciled[3] is None, reconciled[3]
        cleared = store.lp_session(sid)
        assert cleared.get('facts_error') is None and cleared['submit_status'] == 'unknown'
        assert cleared['reservation_coverage']['state'] == 'covered'
        assert store.lp_actions(sid) == immutable_audits
        # A later real account publication resumes covered management only
        # after the preceding reconciliation clears the actual lock error.
        runtime.clock[0] += timedelta(seconds=1)
        bundle = adapter.lp_account_snapshot_shared(max_age_seconds=-1,
            trade_generation_provider=store.lp_trade_generation)
        assert lp.register_account_snapshot(bundle)['state'] == 'registered'
        bound = deepcopy(store.lp_session(sid))
        assert bound['entry_order_id'] == receipts[0].order_id
        assert bound['state'] == 'entry_open'
        assert bound['submit_status'] == 'unknown' and bound['submit_stage'] == 'sending'
        assert bound['reservation_coverage']['state'] == 'covered'
        assert bound['post_started'] is True and bound.get('facts_error') is None
        assert bound['order_history']['unknown-terminal-1']['status'] == 'LIVE'
        assert venue['unknown-terminal-1'].status == 'LIVE' and venue['unknown-terminal-1'].size_matched == 0
        for key in ('submit_requested_at', 'submit_post_started_at'):
            assert bound[key] and audit[key] and bound[key] == audit[key], key
        requested, started, received, finished = [datetime.fromisoformat(audit[key]) for key in
            ('submit_requested_at', 'submit_post_started_at', 'submit_receipt_at', 'submit_finished_at')]
        assert requested <= started <= received <= finished < bundle['read_started_at']
        assert not bound.get('submit_receipt_at') and not bound.get('submit_finished_at')
        assert store.lp_actions(sid) == immutable_audits and len(posts) == 2 and cancels == []
        financial_asof = engine.lp_auto_state()['funds']['as_of']
        assert financial_asof

        diagnostics['stage'] = 'public_unknown_terminal_zero_proof'
        assert engine.lp_stop(sid)['state'] == 'review'
        assert cancels == [('unknown-terminal-1',)]
        assert venue['unknown-terminal-1'].status == 'CANCELED'
        assert venue['unknown-terminal-1'].size_matched == 0
        runtime.clock[0] += timedelta(seconds=1)
        bundle = adapter.lp_account_snapshot_shared(max_age_seconds=-1,
            trade_generation_provider=store.lp_trade_generation)
        assert lp.register_account_snapshot(bundle)['state'] == 'registered'
        lp.reconcile_facts(sid)
        terminal = deepcopy(store.lp_session(sid))
        assert terminal['state'] == 'complete' and terminal['orders_terminal'] is True
        assert terminal['position_reconciled'] is True and terminal['fee_status'] == 'known'
        assert terminal['entry_order_id'] == receipts[0].order_id
        assert terminal['stop_requested'] is True
        assert terminal['post_started'] is True
        for key in ('submit_requested_at', 'submit_post_started_at'):
            assert terminal[key] and audit[key] and terminal[key] == audit[key], key
        assert terminal.get('submit_status') == 'unknown' and terminal['submit_stage'] == 'sending'
        history = terminal['order_history']['unknown-terminal-1']
        assert history['status'] == 'CANCELED' and Decimal(history['size_matched']) == 0
        assert history['side'] == 'BUY' and history['token_id'] == _refill_identity(1)[2]
        assert Decimal(history['price']) == Decimal('.40') and Decimal(history['original_size']) == 20
        for key in ('buy_filled_quantity', 'sold_quantity', 'residual_quantity', 'buy_cost', 'sold_revenue', 'buy_fees', 'sell_fees', 'fees'):
            assert Decimal(terminal[key]) == 0, key
        assert not terminal.get('facts_error') and not terminal.get('financial_block_reason')
        assert datetime.fromisoformat(terminal['facts_checked_at']) > datetime.fromisoformat(audit['submit_finished_at'])
        immutable_audits = deepcopy(store.lp_actions(sid))
        assert accepted_audit in immutable_audits
        financial_asof = engine.lp_auto_state()['funds']['as_of']
        assert financial_asof

        captured_intent, = [i for i in engine.lp_auto_state()['intents'] if i['intent_id'] == request_id]
        captured_intent = deepcopy(captured_intent)
        assert captured_intent['session_id'] == sid and captured_intent['intent_id'] == request_id
        diagnostics['captured_pre_recovery_intent'] = deepcopy(captured_intent)
        engine.lp_auto_set_desired_running(False)
        phase[0] = 'auto_recovery'
        unavailable[0] = True
        positive_due = datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline'])
        if lookup == 'unavailable':
            diagnostics['stage'] = 'incomplete_current_negative'
            phase[0] = 'auto_recovery'
            unavailable[0], incomplete[0] = True, True
            runtime.clock[0] = datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline'])
            assert scheduler.run_due()
            waiting = engine.lp_auto_state()
            assert waiting['active_plan'] is None and waiting['last_round']['completed_at']
            assert store.lp_session(sid).get('submit_status') == 'unknown'
            assert len(posts) == 2 and cancels == [('unknown-terminal-1',)]
            assert waiting['plan_wait']['kind'] == 'round'
            failed_at = runtime.clock[0]
            assert datetime.fromisoformat(waiting['plan_wait']['deadline']) == failed_at + timedelta(seconds=30)
            reads = (len(packets), len(lookups), len(finances), account.order_reads, account.trade_reads, account.position_reads)
            for seconds in (1, 29):
                runtime.clock[0] = failed_at + timedelta(seconds=seconds)
                scheduler.request_check()
                assert not scheduler.run_due()
                engine.lp_auto_run_once()
                assert (len(packets), len(lookups), len(finances), account.order_reads, account.trade_reads, account.position_reads) == reads
                assert len(posts) == 2 and cancels == [('unknown-terminal-1',)]
                assert store.lp_session(sid).get('submit_status') == 'unknown'

            positive_due = failed_at + timedelta(seconds=30)

        diagnostics['stage'] = 'complete_current_terminal_positive'
        incomplete[0] = False
        runtime.clock[0] = positive_due
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        packet = packets[-1]
        assert 'unknown-terminal-1' in packet['request']['owned_order_ids']
        result = packet['result']
        assert not any(row['order_id'] == 'unknown-terminal-1' for row in result['open_orders'])
        if lookup == 'unavailable':
            assert result['order_read_errors']['unknown-terminal-1'] == 'order_read_failed'
            assert not any(row['order_id'] == 'unknown-terminal-1' for row in result['orders'])
        else:
            assert not result['order_read_errors']
            exact, = [row for row in result['orders'] if row['order_id'] == 'unknown-terminal-1']
            assert exact['status'] == 'CANCELED' and exact['fill_quantity_known'] is True
            assert Decimal(exact['size_matched']) == 0
            assert exact['side'] == 'BUY' and exact['token_id'] == _refill_identity(1)[2]
            assert Decimal(exact['price']) == Decimal('.40') and Decimal(exact['original_size']) == 20
        assert all(result[key] is True for key in ('authenticated', 'open_orders_complete',
            'pagination_complete', 'trades_complete', 'positions_complete'))
        current_intent, = [i for i in state['intents'] if i['intent_id'] == request_id]
        assert current_intent == captured_intent
        canonical = store.lp_session(sid)
        assert canonical['session_id'] == captured_intent['session_id']
        assert canonical['idempotency_key'] == 'lp-auto:' + request_id
        assert canonical['entry_order_id'] == receipts[0].order_id
        assert receipts[0].order_id in canonical['owned_order_ids']
        assert canonical['order_history'][receipts[0].order_id]['side'] == 'BUY'
        assert canonical['order_history'][receipts[0].order_id]['token_id'] == captured_intent['token_id']
        assert Decimal(canonical['order_history'][receipts[0].order_id]['price']) == Decimal(captured_intent['price'])
        assert Decimal(canonical['order_history'][receipts[0].order_id]['original_size']) == Decimal(captured_intent['quantity'])
        assert audit['session_id'] == sid and audit['order_id'] == receipts[0].order_id
        assert audit in store.lp_actions(sid)
        assert state['last_round']['round_id'] != original['round_id']
        assert state['last_round']['completed_at'] and state['active_plan'] is None
        assert state['last_round']['actions'] == []
        assert Counter(p.token_id for p in posts) == Counter(_refill_identity(i)[2] for i in (1, 2))
        assert all(p.maker_amount == 8000000 and p.taker_amount == 20000000
            and p.post_only is True for p in posts)
        diagnostics['current_result_recovery'] = deepcopy(dict(state=state, original_intent=current_intent))
        assert {key: store.lp_actions(key) for key in sibling_audits} == sibling_audits
        recovered = store.lp_session(sid)
        assert recovered['submit_status'] == 'accepted' and recovered['submit_stage'] == 'receipt_received'
        assert recovered['submit_receipt_at'] == accepted_audit['submit_receipt_at']
        assert recovered['submit_finished_at'] == accepted_audit['submit_finished_at']
        for key in preserved_fields:
            assert recovered.get(key) == terminal.get(key), key
        assert store.lp_actions(sid) == immutable_audits and cancels == [('unknown-terminal-1',)]
        assert state['funds']['as_of'] == financial_asof
        assert [call for call in finances if call['phase'] == 'auto_recovery' and call['failed']]
        assert not [call for call in finances if call['active_plan']]

        diagnostics['stage'] = 'duplicate_wakes_and_public_restart'
        completed = deepcopy(state['last_round'])
        for _ in range(2):
            scheduler.request_check()
            assert not scheduler.run_due()
            engine.lp_auto_run_once()
        restart_store, restart_adapter, restart_account, restart_lp, restarted = runtime(
            orders=tuple(o for o in venue.values() if o.status == 'LIVE'), public_client=public)
        install_external_carriers(restart_adapter, restart_account, restarted)
        restored_scheduler = LPAutoScheduler(restarted, clock=lambda: runtime.clock[0])
        restored_scheduler.request_check()
        restored_scheduler.run_due()
        restarted.lp_auto_run_once()
        assert restarted.lp_auto_state()['last_round'] == completed
        assert len(posts) == 2 and cancels == [('unknown-terminal-1',)]
        assert restart_store.lp_actions(sid) == immutable_audits
        for key in preserved_fields:
            assert restart_store.lp_session(sid).get(key) == terminal.get(key), key
        assert restart_store.lp_session(sid)['submit_receipt_at'] == accepted_audit['submit_receipt_at']

        diagnostics['stage'] = 'full_interval_and_new_identity'
        completed_at = datetime.fromisoformat(completed['completed_at'])
        assert datetime.fromisoformat(restarted.lp_auto_state()['plan_wait']['deadline']) == completed_at + timedelta(seconds=30)
        runtime.clock[0] = completed_at + timedelta(seconds=29)
        restored_scheduler.request_check()
        assert not restored_scheduler.run_due()
        assert len(posts) == 2 and restarted.lp_auto_state()['last_round'] == completed
        runtime.clock[0] = completed_at + timedelta(seconds=30)
        phase[0] = 'next_round_setup'
        prepare_current_candidates(restart_lp, restart_adapter)
        restarted.lp_auto_set_desired_running(True)
        phase[0] = 'next_round'
        assert restored_scheduler.run_due()
        new_state = restarted.lp_auto_state()
        assert new_state['last_round']['round_id'] != original['round_id']
        new_action, = [a for a in new_state['last_round']['actions'] if a['kind'] == 'buy']
        assert new_action['state'] == 'success' and new_action['action_id'] != first['action_id']
        assert new_action.get('request_id', new_action['action_id']) != request_id
        assert new_action['session_id'] != sid and new_action['order_id'] == 'unknown-terminal-3'
        assert new_state['last_round']['completed_at'] and new_state['active_plan'] is None
        assert Counter(p.token_id for p in posts) == Counter(_refill_identity(i)[2] for i in (1, 2, 1))
        assert all(p.maker_amount == 8000000 and p.taker_amount == 20000000 for p in posts)
        assert {o.id for o in venue.values() if o.status == 'LIVE'} == {'unknown-terminal-2', 'unknown-terminal-3'}
        assert new_state['slots']['occupied'] == 2 and Decimal(new_state['funds']['buy_reserved_usd']) == 16
        assert cancels == [('unknown-terminal-1',)] and venue['unknown-terminal-1'].status == 'CANCELED'
        assert restart_store.lp_actions(sid) == immutable_audits
        for key in preserved_fields:
            assert restart_store.lp_session(sid).get(key) == terminal.get(key), key
        assert {key: restart_store.lp_actions(key) for key in sibling_audits} == sibling_audits
        diagnostics['stage'] = 'complete'
    finally:
        first_venue_row.set()
        if held is not None:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
        diagnostics.update(lookup=lookup, clock=runtime.clock[0], original=original, packets=packets, lookups=lookups,
            finances=finances, posts=posts, cancels=cancels, session=store.lp_session(sid) if sid else None,
            audits=store.lp_actions(sid) if sid else [], state=engine.lp_auto_state())
        (tmp_path/'bound-unknown-terminal-diagnostics.json').write_text(json.dumps(diagnostics, indent=2, default=str) + '\n')
