"""Issue 336 current-facts contracts through the real SDK and public controls."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import fcntl
from collections import Counter
import os
from threading import get_ident

from tests.test_lp_account_reservation_reconciliation import runtime, _refill_identity
from tests.test_lp_auto_refill_contract import RefillPublic, prepare


def test_local_attempts_finish_round_without_settling_unknown(runtime, tmp_path, monkeypatch):
    public = RefillPublic(runtime.clock)
    public.sizes = {i: '50' for i in range(1, 7)}
    public.rates = {i: str(120 - i * 12) for i in range(1, 7)}
    store, adapter, account, lp, engine, _ = prepare(runtime, count=5, public=public)
    sign, post = account.create_limit_order, account.post_order
    held, refusals, preparations = [], [], []
    native_flock = fcntl.flock
    lock_path = tmp_path / 'execution.lock'
    refusal_thread = []

    def release_lock():
        while held:
            handle = held.pop()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def observed_flock(handle, operation):
        try:
            return native_flock(handle, operation)
        except BlockingIOError:
            descriptor = handle if isinstance(handle, int) else handle.fileno()
            if (held and refusal_thread == [get_ident()]
                    and os.fstat(descriptor).st_ino == lock_path.stat().st_ino):
                refusals.append('native_execution_lock')
                release_lock()
            raise

    monkeypatch.setattr(fcntl, 'flock', observed_flock)

    def signing(**kwargs):
        preparations.append(kwargs['token_id'])
        signed = sign(**kwargs)
        if kwargs['token_id'] == _refill_identity(4)[2] and not refusals:
            expected = {_refill_identity(i)[2]: outcome for i, outcome in
                ((1, 'success'), (2, 'success'), (3, 'rejected'), (5, 'unknown'))}
            deadline = monotonic() + 5
            while True:
                outcomes = {a['token_id']: a['state'] for a in engine.lp_auto_state()['last_round']['actions']}
                if all(outcomes.get(token) == outcome for token, outcome in expected.items()):
                    break
                assert monotonic() < deadline, 'Independent public outcome scheduling watchdog'
                Event().wait(.01)
            handle = lock_path.open('a+')
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                handle.close()
                raise
            held.append(handle)
            refusal_thread.append(get_ident())
        return signed

    def venue(signed):
        if signed.token_id == _refill_identity(3)[2]:
            account.posts.append(signed)
            return {'accepted': False, 'status': 'REJECTED', 'code': 'venue_denied'}
        if signed.token_id == _refill_identity(5)[2]:
            account.posts.append(signed)
            raise TimeoutError('simulated POST response lost')
        return post(signed)

    account.create_limit_order, account.post_order = signing, venue
    try:
        first = engine.lp_auto_run_once(round_id='mixed-attempts')
    finally:
        release_lock()
    assert refusals == ['native_execution_lock']
    assert Counter(preparations) == Counter(_refill_identity(i)[2] for i in range(1, 6))
    assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in (1, 2, 3, 5))
    assert first['active_plan'] is None and first['last_round']['completed_at'], first['last_round']
    assert [a['state'] for a in first['last_round']['actions']] == ['success', 'success', 'rejected', 'not_sent', 'unknown']
    assert first['slots']['occupied'] == 3, first
    assert Decimal(first['funds']['buy_reserved_usd']) + Decimal(first['funds']['isolated_reserved_usd']) == 60
    unknown = next(s for s in store.lp_sessions() if s['token_id'] == _refill_identity(5)[2])
    audit = deepcopy(store.lp_actions(unknown['session_id']))
    assert audit[0]['state'] == 'unknown' and audit[0]['post_started']

    def history_unavailable(**kwargs):
        raise TimeoutError('history transport only; current orders and funds remain complete')

    runtime.clock[0] = datetime.fromisoformat(first['plan_wait']['deadline'])
    lp.refresh_candidate_recommendations()
    account.list_account_trades = history_unavailable
    account.create_limit_order = sign
    account.post_order = post
    second = engine.lp_auto_run_once(round_id='fresh-two-free')
    assert second['last_round']['round_id'] == 'fresh-two-free', second['last_round']
    assert Counter(p.token_id for p in account.posts[4:]) == Counter(_refill_identity(i)[2] for i in (3, 4))
    assert second['slots']['occupied'] == 5, second
    assert Decimal(second['funds']['buy_reserved_usd']) + Decimal(second['funds']['isolated_reserved_usd']) == 100
    assert store.lp_actions(unknown['session_id']) == audit
    assert sum(p.token_id == _refill_identity(5)[2] for p in account.posts) == 1


import pytest
import json
from contextlib import contextmanager
from types import SimpleNamespace
from polymarket.models.clob import SignedOrder
from tests.test_lp_order_registration_contract import WALLET, _open_order


def _entry_sdk(account, clock):
    def sign(**kwargs):
        clock[0] += timedelta(microseconds=1)
        return SignedOrder(builder='0x1', expiration=kwargs['expiration'], maker=WALLET,
            maker_amount=int(kwargs['price'] * kwargs['size'] * 1000000), metadata='0x3',
            order_type='GTD', salt=1, side='BUY', signature='0x4', signature_type=0,
            signer=WALLET, taker_amount=int(kwargs['size'] * 1000000), timestamp=1,
            token_id=str(kwargs['token_id']), post_only=True)

    def post(signed):
        clock[0] += timedelta(microseconds=1)
        account.posts.append(signed)
        index = next(i for i in range(1, 7) if signed.token_id == _refill_identity(i)[2])
        oid = f'current-{index}-{len(account.posts)}'
        order = _open_order(oid, 'BUY', price=str(Decimal(signed.maker_amount) / signed.taker_amount),
            original=str(Decimal(signed.taker_amount) / 1000000), token_id=signed.token_id,
            outcome='YES').model_copy(update={'market': _refill_identity(index)[1],
                'condition_id': _refill_identity(index)[1]})
        account.orders += (order,)
        return {'order_id': oid, 'status': 'LIVE', 'accepted': True, 'size_matched': '0'}

    account.create_limit_order, account.post_order = sign, post
    return sign, post


class ColdPublic(RefillPublic):
    def list_current_rewards(self, *, sponsored=False):
        rows = super().list_current_rewards(sponsored=sponsored)
        class Pages:
            def iter_items(self):
                yield from rows
            def first_page(self):
                return SimpleNamespace(items=rows, has_more=False)
        return Pages()


def _prepare_public_inputs(lp, adapter):
    @contextmanager
    def http(request, **kwargs):
        if request.data:
            body = json.loads(request.data)
            payload = {'history': {t: [dict(t=body['start_ts'], p='.40'),
                dict(t=body['end_ts'], p='.401')] for t in body['markets']}}
        else:
            payload = {'data': [dict(condition_id=_refill_identity(i)[1],
                market_competitiveness='2.5') for i in range(1, 7)], 'next_cursor': 'LTE='}
        yield SimpleNamespace(read=lambda: json.dumps(payload).encode())
    adapter._urlopen_fn = http  # Existing actual adapter external HTTP carrier.
    lp.refresh_competition_cache()
    prepared = lp.refresh_price_history()
    assert prepared['preparation_outcome'] == 'success', prepared
    return lp.refresh_candidates(force=True)


@pytest.mark.parametrize('scenario', ['first_start', 'restart_unsent', 'restart_unknown'])
def test_start_and_restart_recompute_from_current_orders(runtime, scenario):
    public = ColdPublic(runtime.clock)
    public.rates = {i: str(120 - i * 12) for i in range(1, 7)}
    store, adapter, account, lp, engine, _ = prepare(runtime, count=6, public=public)
    sign, post = _entry_sdk(account, runtime.clock)
    original_audit = None
    if scenario == 'restart_unsent':
        def interrupted_sign(**kwargs):
            raise KeyboardInterrupt('simulated process interruption before POST')
        account.create_limit_order = interrupted_sign
        with pytest.raises(KeyboardInterrupt):
            engine.lp_auto_run_once(round_id='old-unsent')
        assert engine.lp_auto_state()['active_plan']['round_id'] == 'old-unsent'
        assert account.posts == []
    elif scenario == 'restart_unknown':
        def lost_post(signed):
            account.posts.append(signed)
            if signed.token_id == _refill_identity(1)[2]:
                raise TimeoutError('original POST unknown')
            return {'accepted': False, 'status': 'REJECTED', 'code': 'venue_denied'}
        account.post_order = lost_post
        first = engine.lp_auto_run_once(round_id='old-unknown')
        assert len(account.posts) == 5
        original_audit = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        assert first['slots']['occupied'] == 1
    # Current venue orders plus one unknown, when present, leave one lawful slot.
    originals = range(2, 5) if scenario == 'restart_unknown' else range(1, 5)
    account.orders = tuple(_open_order(f'current-original-{i}', 'BUY', price='.40', original='20',
        token_id=_refill_identity(i)[2], outcome='YES').model_copy(update={
            'market': _refill_identity(i)[1], 'condition_id': _refill_identity(i)[1]}) for i in originals)
    public.rates[6] = '1000'
    runtime.clock[0] += timedelta(seconds=61)
    if scenario != 'first_start':
        adapter.close()
        store, adapter, account, lp, engine = runtime(public_client=public, orders=account.orders)
        _entry_sdk(account, runtime.clock)
        _prepare_public_inputs(lp, adapter)
    lp.refresh_candidate_recommendations()
    if scenario == 'restart_unknown':
        def unavailable_history(**kwargs):
            raise TimeoutError('history-only fallback retains the original unknown')
        account.list_account_trades = unavailable_history
    state = engine.lp_auto_run_once(round_id='current-start')
    assert state['last_round']['round_id'] == 'current-start', state['last_round']
    assert Counter(p.token_id for p in account.posts) == Counter({_refill_identity(6)[2]: 1}), (state['last_round']['reason'], state['last_round']['candidate_filter'], state['last_round']['actions'])
    assert state['active_plan'] is None and state['last_round']['completed_at']
    assert state['slots']['occupied'] == 5
    assert {o.id for o in account.orders if o.id.startswith('current-original-')} == {f'current-original-{i}' for i in originals}
    if original_audit is not None:
        assert {sid: store.lp_actions(sid) for sid in original_audit} == original_audit


from tests import test_lp_auto_pool as pool
from tests.test_lp_auto_target_convergence import plan_setup, live_orders
from tests.test_lp_auto_plan_execution import install_order_result_reader
from open_trader.polymarket_lp_scheduler import LPAutoScheduler


@pytest.mark.parametrize('cancel_outcome', ['terminal_zero', 'ack_live'])
def test_capacity_dependent_replacement_uses_next_round_candidates(tmp_path, monkeypatch, cancel_outcome):
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch, ('A', 'B', 'C', 'D', 'E'),
        tokens=('A', 'B', 'C', 'D', 'E', 'F', 'G'))
    exchange.rewards.update({k: Decimal(v) for k, v in dict(A=1000, B=900, C=800, D=700, E=1, F=100, G='.5').items()})
    lp.refresh_candidate_recommendations()
    install_order_result_reader(exchange, monkeypatch)
    exchange.cancel_terminal = cancel_outcome == 'terminal_zero'
    scheduler = LPAutoScheduler(engine, clock=lambda: pool.NOW)
    assert scheduler.run_due()
    first = engine.lp_auto_state()
    assert exchange.cancels == ['original-E'], first['last_round']
    assert exchange.posts == [], 'No BUY can spend E before the next observation'
    assert first['active_plan'] is None and first['last_round']['completed_at']
    completed = datetime.fromisoformat(first['last_round']['completed_at'])
    assert datetime.fromisoformat(first['plan_wait']['deadline']) == completed + timedelta(seconds=30)
    exchange.rewards['G'] = Decimal(2000)
    before_reads = len(exchange.reads)
    monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=29))
    assert not scheduler.run_due()
    assert len(exchange.reads) == before_reads and exchange.posts == []
    monkeypatch.setattr(pool, 'NOW', completed + timedelta(seconds=30))
    lp.refresh_candidate_recommendations()
    assert scheduler.run_due()
    second = engine.lp_auto_state()
    assert second['last_round']['round_id'] != first['last_round']['round_id']
    assert len(exchange.reads) > before_reads
    expected = Counter({'G': 1}) if cancel_outcome == 'terminal_zero' else Counter()
    assert Counter(p['token_id'] for p in exchange.posts) == expected, second['last_round']
    assert 'F' not in [p['token_id'] for p in exchange.posts]
    assert second['slots']['occupied'] == 5
    for key in ('A', 'B', 'C', 'D'):
        assert live_orders(exchange)[key] == f'original-{key}'
    if cancel_outcome == 'ack_live':
        assert live_orders(exchange)['E'] == 'original-E'


def test_cancel_and_free_slot_buys_can_run_in_one_round(runtime):
    public = RefillPublic(runtime.clock)
    public.sizes = {i: '50' for i in range(1, 7)}
    public.rates = dict(enumerate(('1000', '900', '1', '600', '500', '400'), 1))
    store, adapter, account, lp, engine, _ = prepare(runtime, count=6, public=public)
    execution_market_reads, execution_order_reads = [], []
    for name in ('get_market', 'get_order_book', 'list_market_rewards'):
        external = getattr(public, name)
        def observed(*args, _external=external, _name=name, **kwargs):
            if engine.lp_auto_state()['active_plan'] is not None:
                execution_market_reads.append((_name, dict(kwargs)))
            return _external(*args, **kwargs)
        setattr(public, name, observed)
    current_orders = account.list_open_orders
    def observed_orders(**kwargs):
        if engine.lp_auto_state()['active_plan'] is not None:
            execution_order_reads.append(dict(kwargs))
        return current_orders(**kwargs)
    account.list_open_orders = observed_orders
    account.orders = tuple(_open_order(f'original-{i}', 'BUY', price='.40', original='50',
        token_id=_refill_identity(i)[2], outcome='YES').model_copy(update={
            'market': _refill_identity(i)[1], 'condition_id': _refill_identity(i)[1]}) for i in range(1, 4))
    cancel_started, release_cancel, cancel_finished = Event(), Event(), Event()
    cancel = account.cancel_orders
    def blocked_cancel(*, order_ids):
        session, = [s for s in store.lp_sessions() if 'original-3' in s.get('owned_order_ids', ())]
        assert any(a.get('order_id') == 'original-3' and a['state'] == 'pending'
            for a in store.lp_actions(session['session_id']))
        cancel_started.set()
        try:
            assert release_cancel.wait(5), 'Independent cancel watchdog'
            return cancel(order_ids=order_ids)
        finally:
            cancel_finished.set()
    account.cancel_orders = blocked_cancel
    with ThreadPoolExecutor(1) as workers:
        running = workers.submit(engine.lp_auto_run_once, round_id='independent-free-capacity')
        try:
            assert cancel_started.wait(5), 'Registered actual C cancel entered external I/O'
            deadline = monotonic() + 5
            expected = {_refill_identity(i)[2] for i in (4, 5)}
            while True:
                observed = engine.lp_auto_state()
                done = {a.get('token_id') for a in observed['last_round']['actions']
                    if a['kind'] == 'buy' and a['state'] == 'success'}
                if expected <= done:
                    break
                assert monotonic() < deadline, 'Independent D/E local completion watchdog'
                assert not cancel_finished.is_set(), 'D/E finish before blocked C cancel returns'
                Event().wait(.01)
            assert not cancel_finished.is_set()
            assert observed['slots']['occupied'] == 5
            assert Decimal(observed['funds']['buy_reserved_usd']) + Decimal(observed['funds']['isolated_reserved_usd']) == 100
            assert {o.id for o in account.orders} >= {'original-1', 'original-2', 'original-3'}
        finally:
            release_cancel.set()
            state = running.result(timeout=5)
    assert cancel_finished.is_set()
    assert account.cancels == [('original-3',)], state['last_round']
    assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in (4, 5)), state['last_round']
    assert all(Decimal(p.maker_amount) / 1000000 == 20 for p in account.posts)
    assert {o.id for o in account.orders} >= {'original-1', 'original-2', 'original-3'}
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) + Decimal(state['funds']['isolated_reserved_usd']) == 100
    assert state['active_plan'] is None and state['last_round']['completed_at']
    assert account.market_orders == []
    assert execution_order_reads, 'Planned cancel uses actual current order-result evidence'
    assert execution_market_reads == [], 'Planned cancels and BUYs reuse approved business qualification'


from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic


def test_independent_attempts_overlap_without_holding_mutation_lock(runtime):
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24'}
    store, adapter, account, lp, engine, _ = prepare(runtime, count=2, target=2, public=public)
    sign, post = _entry_sdk(account, runtime.clock)
    sign_started, sign_finished, release_sign, post_started, release_post, post_finished, b_accepted = (Event() for _ in range(7))
    audits_at_post = []

    def signing(**kwargs):
        if kwargs['token_id'] == _refill_identity(1)[2]:
            sign_started.set()
            try:
                assert release_sign.wait(5), 'Independent signing watchdog'
            finally:
                sign_finished.set()
        return sign(**kwargs)

    def venue(signed):
        session, = [s for s in store.lp_sessions() if s['token_id'] == signed.token_id]
        audit, = store.lp_actions(session['session_id'])
        assert audit['state'] == 'pending' and session['post_started'] is True
        audits_at_post.append((session['idempotency_key'], signed.token_id))
        if signed.token_id == _refill_identity(1)[2]:
            post_started.set()
            assert release_post.wait(5), 'Independent POST watchdog'
        try:
            result = post(signed)
        finally:
            if signed.token_id == _refill_identity(1)[2]:
                post_finished.set()
        if signed.token_id == _refill_identity(2)[2]:
            b_accepted.set()
        return result

    account.create_limit_order, account.post_order = signing, venue
    with ThreadPoolExecutor(2) as workers:
        running = workers.submit(engine.lp_auto_run_once, round_id='overlap')
        try:
            assert sign_started.wait(5), 'A external signing began'
            assert b_accepted.wait(5), 'Independent B reaches the venue while A signing waits'
            assert not sign_finished.is_set(), 'B completes before A external signing returns'
            assert not running.done()
            release_sign.set()
            assert post_started.wait(5), 'A actual POST remains dispatched'
            paused = workers.submit(engine.lp_auto_set_desired_running, False).result(timeout=5)
            assert paused['desired_running'] is False
            a_session, = [s for s in store.lp_sessions() if s['token_id'] == _refill_identity(1)[2]]
            assert lp.entry_send_inflight(a_session['session_id'])
            assert not post_finished.is_set(), 'Actual dispatched A POST remains blocked'
        finally:
            release_sign.set()
            release_post.set()
            state = running.result(timeout=5)
        assert post_finished.wait(5)
        deadline = monotonic() + 5
        while lp.entry_send_inflight(a_session['session_id']) and monotonic() < deadline:
            Event().wait(.01)
        assert not lp.entry_send_inflight(a_session['session_id'])
    assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in (1, 2))
    assert Counter(token for _, token in audits_at_post) == Counter(_refill_identity(i)[2] for i in (1, 2))
    assert len({request for request, _ in audits_at_post}) == 2
    assert state['desired_running'] is False and state['active_plan'] is None


@pytest.mark.parametrize('preparation', ['buy_sign', 'cancel_queued', 'manual_cancel_queued'])
def test_ended_round_cannot_send_late_preparation(runtime, preparation):
    if preparation == 'manual_cancel_queued':
        import uuid
        from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic
        public = _CandidateSDKPublic(runtime.clock[0])
        originals = (_open_order('rank-a', 'BUY', price='.40', original='20'),
            _open_order('rank-b', 'BUY', price='.40', original='20'),
            _open_order('rank-c', 'BUY', price='.39', original='20'))
        store, adapter, account, lp, engine = runtime(orders=originals, public_client=public)
        engine.lp_auto_configure(dict(budget_usd='39', target_buy_count=1, buy_price_level=2))
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        session, = store.lp_active_sessions()
        original_session = deepcopy(session)
        sid = session['session_id']
        auto_entered, auto_release, auto_finished = Event(), Event(), Event()
        manual_entered, manual_release, manual_finished = Event(), Event(), Event()
        controller_thread, calls = [], []
        manual_read_window = Event()
        evidence = dict(stage='original_auto_A', observations=[], calls=calls, finances=[], market_reads=[])
        running, controller = None, None
        def capture(label):
            state = deepcopy(engine.lp_auto_state())
            evidence['observations'].append(dict(label=label, state=state,
                sessions=deepcopy(store.lp_sessions()),
                audits={s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()},
                orders=deepcopy(account.orders), posts=deepcopy(account.posts), cancels=deepcopy(account.cancels)))
            return state
        balance = account.get_balance_allowance
        def observed_balance(**kwargs):
            evidence['finances'].append(dict(thread=get_ident(),
                explicit_manual=manual_read_window.is_set(),
                active_plan=bool(engine.lp_auto_state()['active_plan'])))
            return balance(**kwargs)
        account.get_balance_allowance = observed_balance
        for name in ('get_market', 'get_order_book', 'list_market_rewards'):
            reader = getattr(public, name)
            def observed_market(*args, _reader=reader, _name=name, **kwargs):
                evidence['market_reads'].append(dict(method=_name,thread=get_ident(),
                    explicit_manual=manual_read_window.is_set(),
                    active_plan=bool(engine.lp_auto_state()['active_plan'])))
                return _reader(*args,**kwargs)
            setattr(public,name,observed_market)
        def cancel(*, order_ids):
            role = 'manual' if get_ident() in controller_thread else 'auto'
            calls.append(dict(role=role,thread=get_ident(),order_ids=order_ids))
            account.cancels.append(order_ids)
            if role == 'manual':
                assert order_ids == ('rank-b',)
                manual_read_window.clear()
                manual_entered.set()
                try:
                    assert manual_release.wait(5), 'Independent actual manual B SDK watchdog'
                finally:
                    manual_finished.set()
            elif order_ids == ('rank-a',):
                auto_entered.set()
                try:
                    assert auto_release.wait(5), 'Independent actual auto A SDK watchdog'
                finally:
                    auto_finished.set()
            return CancelOrdersResponse(canceled=order_ids, not_canceled={})
        account.cancel_orders = cancel
        def manual_control():
            controller_thread.append(get_ident())
            manual_read_window.set()
            try:
                return engine.lp_cancel_orders(dict(confirm=True,order_ids=['rank-b']))
            finally:
                manual_read_window.clear()
        engine.lp_auto_set_desired_running(True)
        try:
            with ThreadPoolExecutor(2) as workers:
                running = workers.submit(engine.lp_auto_run_once, round_id='independent-manual-queued')
                try:
                    assert auto_entered.wait(5), 'Actual original auto A SDK entered'
                    original_round = deepcopy(engine.lp_auto_state()['last_round'])
                    original_audits = deepcopy(store.lp_actions(sid))
                    assert [a['order_id'] for a in original_round['actions']] == ['rank-a','rank-b']
                    assert Counter(a['order_id'] for a in original_audits) == Counter({'rank-a':1,'rank-b':1})
                    assert all(a['state']=='pending' and a['rotation_round_id']==original_round['round_id'] for a in original_audits)
                    capture('auto_A_held_original_batch_registered')
                    evidence['stage']='manual_public_reachability'
                    controller = workers.submit(manual_control)
                    assert manual_entered.wait(5), 'Public exact manual B SDK reachability watchdog'
                    pending = deepcopy(store.lp_actions(sid))
                    manual_pending, = [a for a in pending if a.get('order_id')=='rank-b'
                        and a.get('role')=='reconciliation_cancel' and not a.get('rotation_round_id')]
                    assert manual_pending['state']=='pending'
                    assert 'post_started' not in manual_pending and 'submit_stage' not in manual_pending
                    uuid.UUID(manual_pending['action_id'])
                    assert manual_pending['action_key'] not in {a['action_key'] for a in original_audits}
                    assert manual_pending['action_id'] not in {a['action_id'] for a in original_audits}
                    assert not manual_finished.is_set() and not controller.done()
                    evidence['manual_pending']=deepcopy(manual_pending)
                    capture('manual_B_pending_before_auto_A_release')
                    evidence['stage']='original_auto_cycle_completes_with_manual_pending'
                    auto_release.set()
                    ended = running.result(timeout=5)
                    assert auto_finished.wait(5)
                    capture('original_auto_cycle_closed_manual_B_still_pending')
                    after = store.lp_actions(sid)
                    actual_manual, = [a for a in after if a['action_key']==manual_pending['action_key']]
                    assert actual_manual == manual_pending and not manual_finished.is_set()
                    assert ended['active_plan'] is None and ended['last_round']['completed_at']
                    assert ended['last_round']['round_id']==original_round['round_id'] and ended['desired_running'] is True
                    assert datetime.fromisoformat(ended['plan_wait']['deadline']) == datetime.fromisoformat(ended['last_round']['completed_at'])+timedelta(seconds=30)
                    assert calls == [dict(role='auto',thread=calls[0]['thread'],order_ids=('rank-a',)),
                        dict(role='manual',thread=controller_thread[0],order_ids=('rank-b',))], 'Independent pending manual request forbids a second automatic B send'
                    original_auto_a, = [a for a in original_audits if a['order_id']=='rank-a'
                        and a['rotation_round_id']==original_round['round_id']]
                    original_auto_b, = [a for a in original_audits if a['order_id']=='rank-b'
                        and a['rotation_round_id']==original_round['round_id']]
                    for original in (original_auto_a, original_auto_b):
                        uuid.UUID(original['action_id'])
                        assert original['session_id']==sid and original['role']=='reconciliation_cancel'
                        assert original['submit_requested_at']
                    auto_a, = [a for a in after if a['action_key']==original_auto_a['action_key']
                        and a['action_id']==original_auto_a['action_id'] and a['session_id']==original_auto_a['session_id']]
                    auto_b, = [a for a in after if a['action_key']==original_auto_b['action_key']
                        and a['action_id']==original_auto_b['action_id'] and a['session_id']==original_auto_b['session_id']]
                    assert auto_a['order_id']=='rank-a' and auto_a['state']=='accepted' and auto_a['post_started'] is True
                    assert auto_b['order_id']=='rank-b' and auto_b['state']=='rejected' and auto_b['post_started'] is False
                    assert auto_b['submit_stage']=='pre_send_rejected' and auto_b['reason']=='rotation_protection_active'
                    action_b, = [a for a in ended['last_round']['actions'] if a['order_id']=='rank-b']
                    assert action_b['state']=='not_sent' and action_b['reason']=='rotation_protection_active'
                    assert ended['slots']['occupied']==3 and Decimal(ended['funds']['buy_reserved_usd'])==Decimal('23.80')
                    assert account.posts==[] and account.orders==originals
                    for original in original_audits:
                        current, = [a for a in after if a['action_key']==original['action_key']]
                        for key in ('action_id','action_key','session_id','created_at','order_id','role','rotation_round_id','submit_requested_at'):
                            assert current[key]==original[key],key
                    current = store.lp_session(sid)
                    for key in ('stop_requested','stop_loss_latched','order_identity_conflict','account_id','token_id','condition_id'):
                        assert current.get(key)==original_session.get(key),key
                    for oid in ('rank-a','rank-b','rank-c'):
                        row=current['order_history'][oid]
                        assert row['status']=='LIVE' and row['side']=='BUY' and Decimal(row['size_matched'])==0
                        assert Decimal(row.get('original_size',row['quantity']))==20
                    assert not [r for r in evidence['finances'] if r['active_plan'] and not r['explicit_manual']]
                    assert not [r for r in evidence['market_reads'] if r['active_plan'] and not r['explicit_manual']]
                    evidence['stage']='manual_B_actual_completion'
                    manual_release.set()
                    manual_result=controller.result(timeout=5)
                    assert manual_finished.wait(5)
                    evidence['manual_result']=deepcopy(manual_result)
                    accepted, = [a for a in store.lp_actions(sid) if a['action_key']==manual_pending['action_key']]
                    assert accepted == {**manual_pending,'state':'accepted','updated_at':accepted['updated_at']}
                    evidence['manual_accepted']=deepcopy(accepted)
                finally:
                    auto_release.set()
                    manual_release.set()
                    if running is not None:
                        try:
                            evidence['auto_final_result']=deepcopy(running.result(timeout=5))
                        except BaseException as exc:
                            evidence['auto_error']=dict(type=type(exc).__name__,message=str(exc))
                    if controller is not None:
                        try:
                            evidence['manual_final_result']=deepcopy(controller.result(timeout=5))
                        except BaseException as exc:
                            evidence['manual_error']=dict(type=type(exc).__name__,message=str(exc))
            evidence['stage']='duplicate_public_wakes_and_full_cold_silence'
            state=capture('both_actual_IO_lifetimes_finished')
            immutable=deepcopy(store.lp_actions(sid))
            assert state['slots']['occupied']==3 and Decimal(state['funds']['buy_reserved_usd'])==Decimal('23.80')
            deadline=datetime.fromisoformat(state['plan_wait']['deadline'])
            scheduler=LPAutoScheduler(engine,clock=lambda:runtime.clock[0])
            for _ in range(2):
                scheduler.request_check()
                assert not scheduler.run_due()
                early=engine.lp_auto_run_once(round_id=original_round['round_id'])
                assert early['active_plan'] is None and early['last_round']['round_id']==original_round['round_id']
            assert store.lp_actions(sid)==immutable
            assert account.cancels==[('rank-a',),('rank-b',)] and account.posts==[] and account.orders==originals
            adapter.close()
            store,adapter,account,lp,engine=runtime(orders=originals,public_client=public)
            replayed=[]
            def cold_cancel(**kwargs):
                replayed.append(kwargs)
                return CancelOrdersResponse(canceled=(),not_canceled={})
            account.cancel_orders=cold_cancel
            cold=LPAutoScheduler(engine,clock=lambda:runtime.clock[0])
            for seconds in (1,29):
                runtime.clock[0]=deadline-timedelta(seconds=30-seconds)
                cold.request_check()
                assert not cold.run_due()
                early=engine.lp_auto_run_once(round_id=original_round['round_id'])
                assert early['active_plan'] is None and early['last_round']['round_id']==original_round['round_id']
                assert early['slots']['occupied']==3 and Decimal(early['funds']['buy_reserved_usd'])==Decimal('23.80')
                assert replayed==[] and account.posts==[] and account.orders==originals
                assert store.lp_actions(sid)==immutable
            evidence['stage']='complete'
        finally:
            auto_release.set()
            manual_release.set()
            evidence.update(clock=runtime.clock[0],auto_finished=auto_finished.is_set(),manual_finished=manual_finished.is_set(),
                controller_thread=controller_thread,session_id=sid,state=engine.lp_auto_state(),sessions=store.lp_sessions(),
                final_audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()})
            (store.path.parent/'IC-manual-cancel-diagnostics.json').write_text(json.dumps(evidence,default=str,indent=2)+'\n')
        return
    if preparation == 'cancel_queued':
        from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic
        public = _CandidateSDKPublic(runtime.clock[0])
        originals = (_open_order('rank-a', 'BUY', price='.40', original='20'),
            _open_order('rank-b', 'BUY', price='.40', original='20'),
            _open_order('rank-c', 'BUY', price='.39', original='20'))
        store, adapter, account, lp, engine = runtime(orders=originals, public_client=public)
        engine.lp_auto_configure(dict(budget_usd='39', target_buy_count=1, buy_price_level=2))
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        session, = store.lp_active_sessions()
        started, release, finished = Event(), Event(), Event()
        def queued_cancel(*, order_ids):
            account.cancels.append(order_ids)
            if order_ids == ('rank-a',):
                started.set()
                try:
                    assert release.wait(5), 'Independent queued cancel watchdog'
                finally:
                    finished.set()
            return CancelOrdersResponse(canceled=order_ids, not_canceled={})
        account.cancel_orders = queued_cancel
        engine.lp_auto_set_desired_running(True)
        with ThreadPoolExecutor(2) as workers:
            running = workers.submit(engine.lp_auto_run_once, round_id='queued-cancel')
            try:
                assert started.wait(5)
                original_round = deepcopy(engine.lp_auto_state()['last_round'])
                assert [a['order_id'] for a in original_round['actions']] == ['rank-a', 'rank-b']
                original_audits = deepcopy(store.lp_actions(session['session_id']))
                assert Counter(a['order_id'] for a in original_audits) == Counter({'rank-a': 1, 'rank-b': 1})
                assert all(a['state'] == 'pending' for a in original_audits)
                assert account.cancels == [('rank-a',)] and account.posts == []
                paused = workers.submit(engine.lp_auto_set_desired_running, False).result(timeout=5)
                assert paused['desired_running'] is False
                ended = running.result(timeout=5)
                assert ended['active_plan'] is None and ended['last_round']['completed_at']
                assert ended['last_round']['round_id'] == original_round['round_id']
                assert not finished.is_set(), 'Original external A cancel still owns its lifetime'
                assert account.cancels == [('rank-a',)]
                resumed = engine.lp_auto_set_desired_running(True)
                assert resumed['desired_running'] is True and resumed['active_plan'] is None
                assert resumed['last_round']['round_id'] == original_round['round_id']
            finally:
                release.set()
                running.result(timeout=5)
            assert finished.wait(5)
            deadline = monotonic() + 5
            while True:
                current_audits = store.lp_actions(session['session_id'])
                a, = [row for row in current_audits if row['order_id'] == 'rank-a']
                b, = [row for row in current_audits if row['order_id'] == 'rank-b']
                if a['state'] != 'pending' and b['state'] != 'pending':
                    break
                assert monotonic() < deadline, 'Original typed cancel results watchdog'
                Event().wait(.01)
        assert account.cancels == [('rank-a',)], 'Queued original B permission stays revoked after resume'
        assert a['state'] == 'accepted' and b['state'] == 'rejected'
        assert b['reason'] == 'cycle_ended' and b['post_started'] is False
        assert b['submit_stage'] == 'pre_send_rejected'
        assert [(row['action_key'], row['order_id'], row['rotation_round_id']) for row in current_audits] == [
            (row['action_key'], row['order_id'], row['rotation_round_id']) for row in original_audits]
        state = engine.lp_auto_state()
        assert state['active_plan'] is None and state['desired_running'] is True
        assert state['slots']['occupied'] == 3 and Decimal(state['funds']['buy_reserved_usd']) == Decimal('23.80')
        assert account.posts == [] and account.orders == originals
        adapter.close()
        store, adapter, account, lp, engine = runtime(orders=originals, public_client=public)
        replayed = []
        account.cancel_orders = lambda **kwargs: replayed.append(kwargs) or CancelOrdersResponse(canceled=(), not_canceled={})
        early = engine.lp_auto_run_once(round_id=original_round['round_id'])
        assert early['desired_running'] is True and early['active_plan'] is None
        assert early['last_round']['round_id'] == original_round['round_id']
        assert replayed == [] and account.posts == []
        assert store.lp_actions(session['session_id']) == current_audits
        return
    store, adapter, account, lp, engine, _ = prepare(runtime, count=1, target=1)
    sign, post = _entry_sdk(account, runtime.clock)
    started, release, external_finished = Event(), Event(), Event()
    def slow_sign(**kwargs):
        started.set()
        try:
            assert release.wait(5), 'Independent external preparation watchdog'
            return sign(**kwargs)
        finally:
            external_finished.set()
    account.create_limit_order = slow_sign
    with ThreadPoolExecutor(1) as workers:
        running = workers.submit(engine.lp_auto_run_once, round_id='late-preparation')
        try:
            assert started.wait(5)
            session, = store.lp_sessions()
            assert lp.entry_send_inflight(session['session_id'])
            engine.lp_auto_set_desired_running(False)
            state = running.result(timeout=5)
            assert state['active_plan'] is None and state['last_round']['completed_at']
            assert state['last_round']['actions'][0]['state'] == 'not_sent'
            assert not external_finished.is_set(), 'Underlying signing still owns its lifetime'
            assert lp.entry_send_inflight(session['session_id'])
            assert account.posts == []
            resumed = engine.lp_auto_set_desired_running(True)
            assert resumed['desired_running'] is True and resumed['active_plan'] is None
            assert resumed['last_round']['round_id'] == 'late-preparation'
            assert lp.entry_send_inflight(session['session_id'])
        finally:
            release.set()
            running.result(timeout=5)
        assert external_finished.wait(5)
        deadline = monotonic() + 5
        while lp.entry_send_inflight(session['session_id']) and monotonic() < deadline:
            external_finished.wait(.01)
        assert not lp.entry_send_inflight(session['session_id'])
    assert account.posts == []
    audit, = store.lp_actions(session['session_id'])
    assert audit['post_started'] is False and audit['state'] == 'rejected'
    ended = engine.lp_auto_state()
    assert ended['desired_running'] is True and ended['active_plan'] is None
    assert ended['last_round']['round_id'] == 'late-preparation'
    assert ended['last_round']['actions'][0]['state'] == 'not_sent'


from polymarket.models.clob.order_book import OrderBookLevel


@pytest.mark.parametrize('scenario', ['changed_book', 'venue_rejection', 'planning_risk_failure'])
def test_market_qualification_is_planning_only_and_post_only_is_kept(runtime, scenario):
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24'}
    store, adapter, account, lp, engine, _ = prepare(runtime, count=2, target=2, public=public)
    sign, post = _entry_sdk(account, runtime.clock)
    original_book = public.get_order_book
    execution_reads = []
    for name in ('get_market', 'get_order_book', 'list_market_rewards'):
        external = getattr(public, name)
        def observed(*args, _external=external, _name=name, **kwargs):
            if engine.lp_auto_state()['active_plan'] is not None:
                execution_reads.append(_name)
            result = _external(*args, **kwargs)
            if (_name == 'get_order_book' and scenario == 'planning_risk_failure'
                    and kwargs['token_id'] == _refill_identity(1)[2]):
                result = result.model_copy(update={'bids': (
                    OrderBookLevel(price=Decimal('.40'), size=Decimal('1000')),
                    OrderBookLevel(price=Decimal('.10'), size=Decimal('1000')))})
            return result
        setattr(public, name, observed)

    def signing(**kwargs):
        assert kwargs['price'] == Decimal('.40') and kwargs['size'] == 20
        assert kwargs['post_only'] is True
        if scenario == 'changed_book':
            public.prices[1] = '.41'
            public.rates[1] = '0'
        return sign(**kwargs)

    def venue(signed):
        if scenario == 'venue_rejection' and signed.token_id == _refill_identity(1)[2]:
            account.posts.append(signed)
            return {'accepted': False, 'status': 'REJECTED', 'code': 'post_only_would_cross',
                'message': 'venue post-only refusal'}
        return post(signed)

    account.create_limit_order, account.post_order = signing, venue
    state = engine.lp_auto_run_once(round_id='planning-only')
    expected = (2,) if scenario == 'planning_risk_failure' else (1, 2)
    assert Counter(p.token_id for p in account.posts) == Counter(_refill_identity(i)[2] for i in expected), state['last_round']
    assert execution_reads == [], 'Business qualification reads end before fixed-plan execution'
    assert all(p.post_only is True and p.maker_amount == 8000000 and p.taker_amount == 20000000 for p in account.posts)
    assert state['active_plan'] is None and state['last_round']['completed_at']
    if scenario == 'venue_rejection':
        refused = next(a for a in state['last_round']['actions'] if a['token_id'] == _refill_identity(1)[2])
        assert refused['state'] == 'rejected' and refused['reason'] == 'post_only_would_cross'
    if scenario == 'planning_risk_failure':
        assert state['last_round']['candidate_filter']['recheck_reasons'].get('stress_loss_exceeded') == 1


@pytest.mark.parametrize('change', ['monitor_publication', 'new_buy_exposure'])
def test_observation_changes_are_judged_by_current_exposure(runtime, change):
    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24', 3: '12'}
    store, adapter, account, lp, engine, _ = prepare(runtime, count=3, target=2, budget='16', public=public)
    _entry_sdk(account, runtime.clock)
    account.orders = (_open_order('original-monitor', 'BUY', price='.40', original='20',
        token_id=_refill_identity(1)[2], outcome='YES').model_copy(update={
            'market': _refill_identity(1)[1], 'condition_id': _refill_identity(1)[1]}),)
    reached, release = Event(), Event()
    armed = [True]
    book = public.get_order_book
    def baseline_book(**kwargs):
        result = book(**kwargs)
        if armed[0] and kwargs['token_id'] == _refill_identity(1)[2]:
            armed[0] = False
            reached.set()
            assert release.wait(5), 'Independent registration preparation watchdog'
        return result
    public.get_order_book = baseline_book
    with ThreadPoolExecutor(2) as workers:
        running = workers.submit(engine.lp_auto_run_once, round_id='current-exposure')
        try:
            assert reached.wait(5), 'Actual current observation precedes baseline preparation'
            # Normal public publication of identical current exposure.
            published = workers.submit(engine.refresh_lp_dashboard_snapshot).result(timeout=5)
            assert published['state'] == 'ready', published
            if change == 'new_buy_exposure':
                market, condition, token = _refill_identity(3)
                preview = engine.lp_preview(dict(market_id=market, condition_id=condition,
                    token_id=token, outcome='YES', price='.40', quantity='20',
                    review_at=runtime.clock[0] + timedelta(minutes=10)))
                assert preview['state'] == 'previewed', preview
                added = engine.lp_start(preview['preview_id'], 'independent-new-buy')
                assert added['state'] == 'entry_open', added
        finally:
            release.set()
            state = running.result(timeout=5)
    if change == 'monitor_publication':
        assert Counter(p.token_id for p in account.posts) == Counter({_refill_identity(2)[2]: 1}), state['last_round']
        assert state['last_round']['completed_at'] and state['active_plan'] is None
    else:
        assert Counter(p.token_id for p in account.posts) == Counter({_refill_identity(3)[2]: 1}), state['last_round']
        assert len(account.orders) == 2 and sum(Decimal(o.price) * Decimal(o.original_size) for o in account.orders) == 16
    assert account.cancels == account.market_orders == []


from open_trader.polymarket_trading import TradingConfig


@pytest.mark.parametrize('failure', ['transport', 'incomplete_orders', 'wrong_account', 'history_only_transport'])
def test_failed_current_read_finishes_check_and_retries_next_interval(runtime, failure):
    store, adapter, account, lp, engine, _ = prepare(runtime, count=1, target=1)
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    balance, orders, history, config = account.get_balance_allowance, account.list_open_orders, account.list_account_trades, adapter.config
    def unavailable(**kwargs):
        runtime.clock[0] += timedelta(seconds=3)
        raise TimeoutError('simulated required current transport')
    def partial_orders(**kwargs):
        yield _open_order('incomplete-prefix', 'BUY', price='.40', original='20')
        unavailable()
    if failure == 'transport':
        account.get_balance_allowance = unavailable
    elif failure == 'incomplete_orders':
        account.list_open_orders = partial_orders
    elif failure == 'wrong_account':
        adapter.config = TradingConfig('0x' + 'b' * 40, '0x' + 'b' * 40)
    else:
        account.list_account_trades = unavailable
    assert scheduler.run_due()
    first = engine.lp_auto_state()
    assert first['active_plan'] is None
    if failure == 'history_only_transport':
        assert len(account.posts) == 1 and first['last_round']['completed_at']
        assert first['slots']['occupied'] == 1
        assert account.cancels == []
        return
    assert account.posts == account.cancels == []
    assert first['last_round']['targets'] == first['last_round']['actions'] == []
    ended = datetime.fromisoformat(first['plan_wait']['started_at'])
    assert datetime.fromisoformat(first['plan_wait']['deadline']) == ended + timedelta(seconds=30)
    before_reads = (account.balance_reads, account.order_reads, account.position_reads)
    runtime.clock[0] = ended + timedelta(seconds=29)
    scheduler.request_check()
    assert not scheduler.run_due()
    assert before_reads == (account.balance_reads, account.order_reads, account.position_reads)
    assert account.posts == account.cancels == []
    account.get_balance_allowance, account.list_open_orders, account.list_account_trades, adapter.config = balance, orders, history, config
    runtime.clock[0] = ended + timedelta(seconds=30)
    assert scheduler.run_due()
    second = engine.lp_auto_state()
    assert second['last_round']['round_id'] != first['last_round']['round_id']
    assert len(account.posts) == 1 and second['last_round']['completed_at'], second['last_round']
    assert account.cancels == []


from polymarket.models.clob.order_response import AcceptedOrder
from polymarket.models.clob.account import OpenOrder
from polymarket.models.clob.cancel import CancelOrdersResponse


@pytest.mark.parametrize('scenario', ['bound_live_unknown', 'bound_terminal_null', 'post_unknown'])
def test_request_recovery_does_not_require_resuming_old_plan(runtime, tmp_path, scenario):
    public = ColdPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, count=1, target=1, budget='8', public=public)
    sign, post = _entry_sdk(account, runtime.clock)
    held, venue = [], {}
    def release_lock():
        while held:
            handle = held.pop()
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
    def send(signed):
        if scenario == 'post_unknown':
            account.posts.append(signed)
            raise TimeoutError('real original POST unknown')
        response = post(signed)
        order = account.orders[-1].model_copy(update={'order_type': 'GTD', 'expiration': signed.expiration})
        account.orders = (order,)
        venue[order.id] = order
        handle = (tmp_path / 'execution.lock').open('a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        held.append(handle)
        return AcceptedOrder(order_id=order.id, status='live', making_amount=Decimal('8'),
            taking_amount=Decimal('20'), trade_ids=(), transactions_hashes=())
    def exact(*, order_id):
        return venue.get(order_id)
    def cancel(*, order_ids):
        account.cancels.append(order_ids)
        for oid in order_ids:
            venue[oid] = venue[oid].model_copy(update={'status': 'CANCELED'})
        account.orders = tuple(o for o in venue.values() if o.status == 'LIVE')
        return CancelOrdersResponse(canceled=order_ids, not_canceled={})
    account.post_order, account.get_order, account.cancel_orders = send, exact, cancel
    try:
        sent = engine.lp_auto_run_once(round_id='original-request')
        assert sent['active_plan'] is None and sent['last_round']['completed_at']
        session, = store.lp_sessions()
        sid = session['session_id']
        entry_audit, = store.lp_actions(sid)
        assert len(account.posts) == 1
        if scenario != 'post_unknown':
            assert entry_audit['state'] == 'accepted' and entry_audit['post_started'] is True
            if scenario == 'bound_live_unknown':
                result = lp.reconcile_facts(sid,
                    apply_lock=(engine._acquire_global_lock, engine._release_global_lock))
                assert result[3] == 'execution_lock'
                assert store.lp_session(sid)['submit_status'] == 'unknown'
            else:
                runtime.clock[0] += timedelta(seconds=1)
                packet = adapter.lp_account_snapshot_shared(max_age_seconds=-1, trade_generation_provider=store.lp_trade_generation)
                assert lp.register_account_snapshot(packet)['state'] == 'registered'
            release_lock()
            if scenario == 'bound_terminal_null':
                assert engine.lp_stop(sid)['state'] == 'review'
            for _ in range(2):
                runtime.clock[0] += timedelta(seconds=1)
                packet = adapter.lp_account_snapshot_shared(max_age_seconds=-1, trade_generation_provider=store.lp_trade_generation)
                assert lp.register_account_snapshot(packet)['state'] == 'registered'
                assert lp.reconcile_facts(sid)[3] is None
            bound = store.lp_session(sid)
            assert bound['entry_order_id'] == entry_audit['order_id']
            if scenario == 'bound_terminal_null':
                assert bound['state'] == 'complete' and bound['orders_terminal'] and bound['position_reconciled']
                assert bound['fee_status'] == 'known' and bound.get('submit_status') is None
                account.get_order = lambda **kwargs: OpenOrder.parse_response(None)
            else:
                assert bound['state'] == 'entry_open' and bound['submit_status'] == 'unknown'
        immutable_audits = deepcopy(store.lp_actions(sid))
    finally:
        release_lock()
    # A real intervening failed public cycle replaces last_round. Neither it
    # nor active_plan contains the original request's execution ownership.
    balance = account.get_balance_allowance
    account.get_balance_allowance = lambda **kwargs: (_ for _ in ()).throw(TimeoutError('intervening read failure'))
    runtime.clock[0] = datetime.fromisoformat(engine.lp_auto_state()['plan_wait']['deadline'])
    if scenario != 'post_unknown':
        open_orders = account.list_open_orders
        account.list_open_orders = lambda **kwargs: None
    try:
        intervening = engine.lp_auto_run_once(round_id='intervening-failed-read')
    finally:
        if scenario != 'post_unknown':
            account.list_open_orders = open_orders
    assert intervening['last_round']['round_id'] == 'intervening-failed-read'
    assert intervening['last_round']['actions'] == [] and intervening['active_plan'] is None
    if scenario != 'post_unknown':
        assert store.lp_session(sid).get('submit_status') == bound.get('submit_status')
        assert store.lp_actions(sid) == immutable_audits
    engine.lp_auto_set_desired_running(False)
    adapter.close()
    store, adapter, account, lp, engine = runtime(public_client=public, orders=account.orders)
    _entry_sdk(account, runtime.clock)
    account.get_order = (lambda **kwargs: OpenOrder.parse_response(None)) if scenario == 'bound_terminal_null' else exact
    if scenario == 'post_unknown':
        account.list_account_trades = lambda **kwargs: (_ for _ in ()).throw(TimeoutError('history-only transport'))
    before = deepcopy(store.lp_session(sid))
    if scenario != 'post_unknown':
        assert before.get('submit_status') == ('unknown' if scenario == 'bound_live_unknown' else None)
        prior_financial_as_of = engine.lp_auto_state()['funds']['as_of']
        assert prior_financial_as_of
        balance_calls, result_packets = [], []
        def failed_cold_balance(**kwargs):
            balance_calls.append(deepcopy(kwargs))
            raise TimeoutError('cold current balance transport failure')
        account.get_balance_allowance = failed_cold_balance
        result_reader = adapter.lp_order_result_snapshot
        def observed_result_reader(request):
            packet = result_reader(request)
            result_packets.append(deepcopy(packet))
            return packet
        adapter.lp_order_result_snapshot = observed_result_reader
    fields = ('state', 'stop_requested', 'stop_loss_latched', 'entry_cancel_requested',
        'queue_protection', 'order_history', 'owned_order_ids', 'reservation_coverage',
        'buy_filled_quantity', 'buy_cost', 'sold_quantity', 'sold_revenue', 'residual_quantity')
    runtime.clock[0] = datetime.fromisoformat(intervening['plan_wait']['deadline']) + timedelta(seconds=1)
    recovered = engine.lp_auto_run_once(round_id='current-recovery')
    current = store.lp_session(sid)
    assert recovered['active_plan'] is None and recovered['desired_running'] is False
    assert recovered['last_round']['round_id'] == 'current-recovery'
    assert account.posts == []
    assert store.lp_actions(sid) == immutable_audits
    if scenario == 'post_unknown':
        assert current['submit_status'] == 'unknown' and not current.get('entry_order_id')
        assert recovered['slots']['occupied'] == 1
        assert Decimal(recovered['funds']['buy_reserved_usd']) + Decimal(recovered['funds']['isolated_reserved_usd']) == 8
    else:
        assert balance_calls, 'The fresh cold SDK actually attempts the failed balance read'
        assert len(result_packets) == 1, 'Original exact results remain independent of balance transport'
        packet = result_packets[0]
        assert packet['authenticated'] is True and packet['wallet_address'] == before['account_id']
        assert all(packet[key] is True for key in ('open_orders_complete', 'pagination_complete',
            'trades_complete', 'positions_complete'))
        assert packet['read_started_at'] <= packet['read_ended_at']
        if scenario == 'bound_live_unknown':
            exact_rows = [row for row in packet['orders'] if row['order_id'] == entry_audit['order_id']]
            assert len(exact_rows) == 1
            row = exact_rows[0]
            assert row['status'] == 'LIVE' and row['side'] == 'BUY' and row['token_id'] == before['token_id']
            assert Decimal(row['price']) == Decimal('.40') and Decimal(row['original_size']) == 20
            assert row['fill_quantity_known'] is True and Decimal(row['size_matched']) == 0
            assert entry_audit['order_id'] not in packet['order_read_errors']
        else:
            assert packet['orders'] == packet['open_orders'] == []
            assert packet['order_read_errors'][entry_audit['order_id']] == 'order_read_failed'
            assert before['state'] == 'complete' and before['orders_terminal'] and before['position_reconciled']
            assert before['fee_status'] == 'known'
            assert all(Decimal(before[key]) == 0 for key in ('buy_filled_quantity', 'buy_cost',
                'sold_quantity', 'sold_revenue', 'residual_quantity', 'buy_fees', 'sell_fees', 'fees'))
        assert recovered['funds']['as_of'] == prior_financial_as_of
        assert 'account_order_sync_unknown' in recovered['admission_block_reasons']
        assert recovered['last_round']['actions'] == [] and recovered['last_round']['completed_at']
        assert recovered['last_round']['reason'] == 'account_order_sync_unknown'
        assert recovered['plan_wait']['kind'] == 'round'
        assert datetime.fromisoformat(recovered['plan_wait']['deadline']) == datetime.fromisoformat(
            recovered['last_round']['completed_at']) + timedelta(seconds=30)
        assert account.cancels == [] and len(store.lp_sessions()) == 1
        assert current['submit_status'] == 'accepted' and current['submit_stage'] == 'receipt_received'
        assert current['submit_receipt_at'] == entry_audit['submit_receipt_at']
        assert current['submit_finished_at'] == entry_audit['submit_finished_at']
        assert {k: current.get(k) for k in fields} == {k: before.get(k) for k in fields}


@pytest.mark.parametrize('management', ['partial_fill', 'protection', 'monitoring'])
def test_partial_fill_and_active_protection_do_not_keep_yield_round_open(tmp_path, monkeypatch, management):
    engine, exchange, lp, store = plan_setup(tmp_path, monkeypatch,
        tokens=('A', 'B', 'C', 'D', 'E', 'F'))
    exchange.rewards.update(D=Decimal(0), E=Decimal(0), F=Decimal(0))
    install_order_result_reader(exchange, monkeypatch)
    venue = exchange.lp_post_order
    external_writer = __import__('threading').Lock()
    def identified_post(signed):
        with external_writer:
            result = venue(signed)
            result['order_id'] = ('original-' if signed['token_id'] in ('A', 'B', 'C') else 'new-') + signed['token_id']
            exchange.orders[-1]['order_id'] = result['order_id']
            return result
    exchange.lp_post_order = identified_post
    initial = engine.lp_auto_run_once(round_id='managed-initial-buys')
    assert Counter(p['token_id'] for p in exchange.posts) == Counter({'A': 1, 'B': 1, 'C': 1}), initial['last_round']
    exchange.rewards.update(D=Decimal(60), E=Decimal(50), F=Decimal(40))
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    original_a = next(s for s in store.lp_sessions() if s['token_id'] == 'A')
    if management == 'partial_fill':
        next(o for o in exchange.orders if o['order_id'] == 'original-A')['size_matched'] = '8'
        exchange.positions = [dict(token_id='A', condition_id='A', size='8', average_price='.39')]
        exchange.trades = [dict(trade_id='original-A-fill', order_id='original-A', condition_id='A',
            token_id='A', side='BUY', size='8', price='.39', fee='0', status='CONFIRMED',
            timestamp=pool.NOW, trader_side='MAKER', taker_order_id='external-taker',
            maker_orders=[dict(order_id='original-A', token_id='A', side='BUY', price='.39',
                matched_amount='8', fee='0', maker_address='test-wallet', owner='credential-owner-uuid')])]
        monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=1))
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        assert lp.reconcile_facts(original_a['session_id'])[3] is None
    else:
        if management == 'protection':
            direction = exchange.direction
            def depleted(token):
                row = direction(token)
                if token == 'A':
                    row['book']['bids'][1]['size'] = '3000'
                return row
            exchange.direction = depleted
            exchange.cancel_terminal = False
        engine.lp_tick()
        session = store.lp_session(original_a['session_id'])
        statuses = {level['state'] for level in session['queue_protection']['levels'].values()}
        assert statuses & ({'triggered', 'canceling', 'cancel_unknown'} if management == 'protection'
            else {'registered', 'monitoring'}), session
    existing_cancels = list(exchange.cancels)
    monkeypatch.setattr(pool, 'NOW', pool.NOW + timedelta(seconds=31))
    lp.refresh_candidate_recommendations()
    state = engine.lp_auto_run_once(round_id='managed-yield')
    assert state['active_plan'] is None and state['last_round']['completed_at']
    assert Counter(p['token_id'] for p in exchange.posts) == Counter({'A': 1, 'B': 1, 'C': 1, 'D': 1, 'E': 1}), state['last_round']
    assert state['slots']['occupied'] == 5
    assert not any(p['side'] == 'SELL' for p in exchange.posts)
    if management != 'monitoring':
        assert exchange.cancels == existing_cancels
        assert live_orders(exchange)['A'] == 'original-A'
        retained = next(r for r in state['last_round']['targets'] if r['token_id'] == 'A')
        assert retained['retained_constraint'] is True
    else:
        assert exchange.cancels == ['original-A']
    if management == 'partial_fill':
        assert Decimal(state['funds']['inventory_cost_usd']) == Decimal('3.12')
        assert Decimal(state['funds']['buy_reserved_usd']) == Decimal('35.88')
        order = next(o for o in exchange.orders if o['order_id'] == 'original-A')
        assert Decimal(order['original_size']) - Decimal(order['size_matched']) == 12


@pytest.mark.parametrize('interval', [30, 90], ids=['default30', 'custom90'])
def test_common_interval_starts_after_attempts_and_cycles_do_not_overlap(runtime, interval):
    store, adapter, account, lp, engine, _ = prepare(runtime, count=1, target=1)
    if interval == 90:
        engine.lp_auto_configure(dict(round_interval_seconds=90))
    assert engine.lp_auto_state()['round_interval_seconds'] == interval
    _, post = _entry_sdk(account, runtime.clock)
    started, release, finished = Event(), Event(), Event()

    def slow_post(signed):
        started.set()
        try:
            assert release.wait(5), 'Independent POST watchdog'
            runtime.clock[0] += timedelta(seconds=7)
            return post(signed)
        finally:
            finished.set()

    account.post_order = slow_post
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    began = runtime.clock[0]
    with ThreadPoolExecutor(1) as workers:
        running = workers.submit(scheduler.run_due)
        try:
            assert started.wait(5)
            runtime.clock[0] += timedelta(seconds=91)
            scheduler.request_check()
            assert scheduler.snapshot()['check_in_progress'] is True
            assert not scheduler.run_due()
            assert not running.done() and not finished.is_set()
            assert account.posts == []
        finally:
            release.set()
            assert running.result(timeout=5)
        assert finished.wait(5)
    first = engine.lp_auto_state()
    ended = datetime.fromisoformat(first['last_round']['completed_at'])
    assert ended >= began + timedelta(seconds=98)
    assert first['active_plan'] is None and len(account.posts) == 1
    assert datetime.fromisoformat(first['plan_wait']['started_at']) == ended
    assert datetime.fromisoformat(first['plan_wait']['deadline']) == ended + timedelta(seconds=interval)
    assert scheduler.snapshot()['next_check_at'] == first['plan_wait']['deadline']
    reads = (account.balance_reads, account.order_reads, account.position_reads)
    runtime.clock[0] = ended + timedelta(seconds=interval - 1)
    scheduler.request_check()
    assert not scheduler.run_due()
    assert reads == (account.balance_reads, account.order_reads, account.position_reads)
    assert len(account.posts) == 1

    balance = account.get_balance_allowance
    failed_reads = []
    def unavailable(**kwargs):
        failed_reads.append(runtime.clock[0])
        runtime.clock[0] += timedelta(seconds=11)
        raise TimeoutError('simulated required read after completed attempt')
    account.get_balance_allowance = unavailable
    runtime.clock[0] = ended + timedelta(seconds=interval)
    assert scheduler.run_due()
    failed = engine.lp_auto_state()
    assert failed_reads and failed['last_round']['round_id'] != first['last_round']['round_id']
    assert failed['active_plan'] is None and failed['last_round']['actions'] == []
    failed_end = datetime.fromisoformat(failed['last_round']['completed_at'])
    assert failed_end == runtime.clock[0]
    assert datetime.fromisoformat(failed['plan_wait']['deadline']) == failed_end + timedelta(seconds=interval)
    runtime.clock[0] = failed_end + timedelta(seconds=interval - 1)
    scheduler.request_check()
    assert not scheduler.run_due()
    assert len(failed_reads) == 1
    account.get_balance_allowance = balance
    engine.lp_auto_set_desired_running(False)
    runtime.clock[0] = failed_end + timedelta(seconds=interval)
    scheduler.request_check()
    assert scheduler.run_due()
    stopped = engine.lp_auto_state()
    assert stopped['desired_running'] is False and stopped['active_plan'] is None
    assert stopped['last_round']['round_id'] != failed['last_round']['round_id']
    assert stopped['last_round']['actions'] == []
    assert len(account.posts) == 1 and account.cancels == []
@pytest.mark.parametrize('limited', ['balance', 'allowance'])
def test_same_round_buys_use_wallet_net_free_capital(runtime, tmp_path, limited):
    import uuid

    public = RefillPublic(runtime.clock)
    public.sizes = {i: '50' for i in range(1, 7)}
    public.rates = dict(enumerate(('100', '90', '80', '1000', '900', '800'), 1))
    store, adapter, account, lp, engine, _ = prepare(runtime, count=6, public=public)
    original_orders = tuple(_open_order(f'original-{i}', 'BUY', price='.40', original='50',
        token_id=_refill_identity(i)[2], outcome='YES').model_copy(update={
            'market': _refill_identity(i)[1], 'condition_id': _refill_identity(i)[1]})
        for i in range(1, 4))
    account.orders = original_orders
    balance = account.get_balance_allowance
    finance_calls, signed_tokens = [], []

    def limited_wallet(**kwargs):
        native = balance(**kwargs)
        finance_calls.append(dict(active=engine.lp_auto_state()['active_plan'] is not None,
            original_balance=native.balance))
        return SimpleNamespace(balance='60000000' if limited == 'balance' else '100000000',
            allowances={account.environment.standard_exchange:
                '60000000' if limited == 'allowance' else '100000000'})

    account.get_balance_allowance = limited_wallet
    sign = account.create_limit_order

    def observed_sign(**kwargs):
        signed_tokens.append(kwargs['token_id'])
        return sign(**kwargs)

    account.create_limit_order = observed_sign
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    initial = engine.lp_auto_state()
    assert initial['slots']['occupied'] == 3
    assert Decimal(initial['funds']['buy_reserved_usd']) == 60
    assert Decimal(initial['funds']['spendable_usd']) == 40
    original_sessions = {s['session_id']: deepcopy(s) for s in store.lp_sessions()
        if set(s.get('owned_order_ids', ())) & {o.id for o in original_orders}}
    assert len(original_sessions) == 3
    original_audits = {sid: deepcopy(store.lp_actions(sid)) for sid in original_sessions}
    entered, release, finished = Event(), Event(), Event()
    pending_audits, errors = [], []
    cancel = account.cancel_orders

    def blocked_cancel(*, order_ids):
        try:
            current = engine.lp_auto_state()['active_plan']
            assert current is not None
            victims = {a['order_id']: a for a in current['actions'] if a['kind'] == 'cancel'}
            assert set(order_ids) <= set(victims) <= {o.id for o in original_orders}
            for oid in order_ids:
                action = victims[oid]
                session = store.lp_session(action['session_id'])
                assert session['account_id'].casefold() == WALLET.casefold()
                assert oid in session['owned_order_ids']
                index = int(oid.removeprefix('original-'))
                assert session['token_id'] == _refill_identity(index)[2]
                audits = [a for a in store.lp_actions(session['session_id'])
                    if a.get('order_id') == oid and a.get('rotation_round_id') == current['round_id']]
                assert audits and all(a['state'] == 'pending' for a in audits)
                for audit in audits:
                    assert uuid.UUID(audit['action_id'])
                    assert audit['session_id'] == session['session_id']
                    assert audit['action_key'].startswith(session['session_id'] + ':reconciliation-cancel:')
                    pending_audits.append(deepcopy(audit))
            entered.set()
            assert release.wait(5), 'Actual cancel I/O watchdog'
            return cancel(order_ids=order_ids)
        except BaseException as exc:
            errors.append(repr(exc))
            entered.set()
            raise
        finally:
            finished.set()

    account.cancel_orders = blocked_cancel
    with ThreadPoolExecutor(1) as workers:
        running = workers.submit(engine.lp_auto_run_once, round_id=f'wallet-net-{limited}')
        try:
            assert entered.wait(5), 'Real registered cancel reaches SDK'
            assert not errors, errors
            deadline = monotonic() + 5
            while True:
                held = engine.lp_auto_state()
                buys = [a for a in held['last_round']['actions'] if a['kind'] == 'buy']
                if buys and all(a['state'] != 'pending' for a in buys):
                    break
                assert monotonic() < deadline, 'Fixed local BUY dispositions watchdog'
                Event().wait(.01)
            (tmp_path / 'R1-held-public-proof.json').write_text(json.dumps(dict(
                state=held, signed_tokens=signed_tokens, posts=[p.token_id for p in account.posts],
                pending_audits=pending_audits, finance_calls=finance_calls), default=str, indent=2))
            assert not finished.is_set()
            assert signed_tokens == [] and account.posts == []
            assert all(a['state'] == 'not_sent' for a in buys)
            assert held['slots']['occupied'] == 3
            assert Decimal(held['funds']['buy_reserved_usd']) == 60
            assert account.orders == original_orders
        finally:
            release.set()
            state = running.result(timeout=5)
            (tmp_path / 'R1-completed-public-proof.json').write_text(json.dumps(dict(
                state=state, signed_tokens=signed_tokens, posts=[p.token_id for p in account.posts],
                cancels=account.cancels, errors=errors, pending_audits=pending_audits,
                sessions=store.lp_sessions(), audits={sid: store.lp_actions(sid) for sid in original_sessions},
                finance_calls=finance_calls), default=str, indent=2))
    assert not errors, errors
    assert finished.is_set() and pending_audits
    assert signed_tokens == [] and account.posts == account.market_orders == []
    assert account.orders == original_orders
    assert state['slots']['occupied'] == 3
    assert Decimal(state['funds']['buy_reserved_usd']) == 60
    assert state['active_plan'] is None and state['last_round']['completed_at']
    cancel_actions = [a for a in state['last_round']['actions'] if a['kind'] == 'cancel']
    assert Counter(oid for batch in account.cancels for oid in batch) == Counter(a['order_id'] for a in cancel_actions)
    assert all(a['state'] == 'canceling' for a in cancel_actions)
    buy_actions = [a for a in state['last_round']['actions'] if a['kind'] == 'buy']
    assert buy_actions and all(a['state'] == 'not_sent' for a in buy_actions)
    for action in buy_actions:
        target = state['last_round']['targets'][action['index']]
        assert target['token_id'] == action['token_id']
        assert Decimal(target['price']) == Decimal('.40') and Decimal(target['quantity']) == 50
    for sid, before in original_audits.items():
        current = store.lp_actions(sid)
        assert all(a in current for a in before)
    completed_audits = {sid: deepcopy(store.lp_actions(sid)) for sid in original_sessions}
    assert all(any(a['action_id'] == pending['action_id'] and a['action_key'] == pending['action_key']
        and a['order_id'] == pending['order_id'] and a['state'] == 'accepted'
        for a in completed_audits[pending['session_id']]) for pending in pending_audits)
    completed = datetime.fromisoformat(state['last_round']['completed_at'])
    assert state['plan_wait']['kind'] == 'round'
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=30)
    reads = (account.balance_reads, account.order_reads, account.trade_reads, account.position_reads)
    for elapsed in (1, 29):
        runtime.clock[0] = completed + timedelta(seconds=elapsed)
        quiet = engine.lp_auto_run_once()
        assert quiet['last_round'] == state['last_round']
        assert signed_tokens == [] and account.posts == []
        assert (account.balance_reads, account.order_reads, account.trade_reads, account.position_reads) == reads
        assert {sid: store.lp_actions(sid) for sid in original_sessions} == completed_audits
    assert finance_calls and not any(call['active'] for call in finance_calls)


@pytest.mark.parametrize('schedule', ['generation_only', 'filled_owner_publication'])
def test_invalidated_observation_cannot_authorize_new_round(runtime, tmp_path, monkeypatch, schedule):
    import sqlite3
    from pathlib import Path
    from tests.test_lp_account_reservation_reconciliation import _position
    from tests.test_lp_order_registration_contract import _maker_order, _trade

    public = RefillPublic(runtime.clock)
    public.rates = {1: '48', 2: '24'}
    store, adapter, account, lp, engine, _ = prepare(runtime, count=2, target=2, budget='16', public=public)
    _entry_sdk(account, runtime.clock)
    original_order = _open_order('original-monitor', 'BUY', price='.40', original='20',
        token_id=_refill_identity(1)[2], outcome='YES').model_copy(update={
            'market': _refill_identity(1)[1], 'condition_id': _refill_identity(1)[1]})
    account.orders = (original_order,)
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    original, = [s for s in store.lp_sessions() if 'original-monitor' in s.get('owned_order_ids', ())]
    sid = original['session_id']
    assert original['account_id'].casefold() == WALLET.casefold()
    assert original['token_id'] == _refill_identity(1)[2]
    assert original['order_history']['original-monitor']['status'] == 'LIVE'
    assert Decimal(original['order_history']['original-monitor']['size_matched']) == 0
    assert original.get('queue_protection'), 'Public original protection baseline exists'
    original_audits = deepcopy(store.lp_actions(sid))
    initial_generation = store.lp_trade_generation()
    reached, release = Event(), Event()
    automatic_thread, rows_observed, generations_committed, errors = [], [], [], []
    armed = [True]
    wire_reads, lookups, publication = [], [], {}
    balance, positions, orders = account.get_balance_allowance, account.list_positions, account.list_open_orders

    def observed_balance(**kwargs):
        started = runtime.clock[0]
        result = balance(**kwargs)
        wire_reads.append(dict(stage='balance', started_at=started, ended_at=runtime.clock[0],
            balance=result.balance, allowance=dict(result.allowances)))
        return result

    def observed_positions(**kwargs):
        result = positions(**kwargs)
        wire_reads.append(dict(stage='positions', ended_at=runtime.clock[0], quantity=[str(p.size) for p in result]))
        return result

    def observed_orders(**kwargs):
        result = orders(**kwargs)
        wire_reads.append(dict(stage='orders', at=runtime.clock[0], ids=[o.id for o in result],
            matched=[str(o.size_matched) for o in result]))
        return result

    account.get_balance_allowance = observed_balance
    account.list_positions = observed_positions
    account.list_open_orders = observed_orders
    native_connect = sqlite3.connect
    revision_sql = ("SELECT session_id,json_extract(payload,'$._lp_trade_revision') AS revision "
        "FROM lp_sessions WHERE lower(trim(coalesce(json_extract(payload,'$.account_id'),''))) IN ('',?)")

    class ObservedCursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            self.sql, self.parameters = sql, parameters
            return super().execute(sql, parameters)

        def fetchall(self):
            rows = super().fetchall()
            if (self.sql == revision_sql and self.parameters == (WALLET.casefold(),)
                    and automatic_thread == [get_ident()] and armed[0]):
                armed[0] = False
                apply_free = lp._first_seen_apply_lock.acquire(blocking=False)
                if apply_free:
                    lp._first_seen_apply_lock.release()
                record = dict(thread=get_ident(), sql=self.sql, account=self.parameters[0],
                    in_transaction=self.connection.in_transaction, apply_free=apply_free,
                    native_rows=[dict(row) for row in rows], native_rows_identity=id(rows),
                    generation=store.lp_trade_generation(), wire_reads=deepcopy(wire_reads))
                rows_observed.append(record)
                reached.set()
                try:
                    assert not record['in_transaction'] and record['apply_free']
                    assert sid in {row['session_id'] for row in rows}
                    assert release.wait(5), 'Native outside-writer fetchall delivery watchdog'
                except BaseException as exc:
                    errors.append(repr(exc))
                    raise
                finally:
                    record['returned_rows_identity'] = id(rows)
            return rows

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = self.cursor(factory=ObservedCursor).execute(sql, parameters)
            if sql.startswith('UPDATE lp_trade_generation SET generation=generation+1') and result.rowcount == 1:
                self.generation_advance = dict(sql=sql, parameters=list(parameters), thread=get_ident())
            if sql == 'COMMIT' and getattr(self, 'generation_advance', None):
                generations_committed.append({**self.generation_advance, 'successful_commit': True,
                    'in_transaction_after': self.in_transaction})
                self.generation_advance = None
            return result

    def observed_connect(database, *args, **kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs['factory'] = ObservedConnection
        return native_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', observed_connect)

    def run_original_observation():
        automatic_thread.append(get_ident())
        return engine.lp_auto_run_once(round_id=f'invalidated-{schedule}')

    with ThreadPoolExecutor(1) as workers:
        running = workers.submit(run_original_observation)
        try:
            assert reached.wait(5), 'Native first original automatic revision SELECT'
            assert not errors and len(rows_observed) == 1, (errors, rows_observed)
            frozen = rows_observed[0]
            assert frozen['generation'] == initial_generation
            old_map = {row['session_id']: int(row['revision'] or 0) for row in frozen['native_rows']}
            assert any(r['stage'] == 'orders' and r['ids'] == ['original-monitor']
                and r['matched'] == ['0'] for r in frozen['wire_reads'])
            assert any(r['stage'] == 'positions' and r['quantity'] == [] for r in frozen['wire_reads'])
            assert account.trades == () and account.positions == ()
            runtime.clock[0] += timedelta(microseconds=1)
            account.orders = ()
            receipt = original_order.model_copy(update={'status': 'CANCELED', 'size_matched': Decimal('8')})
            if schedule == 'filled_owner_publication':
                trade = _trade('owned-eight', _maker_order('original-monitor', 'BUY', '8', '.40'), size='8')
                trade = trade.model_copy(update={'match_time': runtime.clock[0], 'last_update': runtime.clock[0]})
                account.trades = (trade,)
                account.positions = (_position('8'),)
                receipt = receipt.model_copy(update={'associate_trades': ('owned-eight',)})

            def exact_original_result(*, order_id):
                assert order_id == 'original-monitor'
                lookups.append(dict(order_id=order_id, matched=str(receipt.size_matched),
                    original=str(receipt.original_size), price=str(receipt.price), status=receipt.status))
                return receipt

            account.get_order = exact_original_result
            newer = adapter.lp_account_snapshot_shared(max_age_seconds=-1,
                trade_generation_provider=store.lp_trade_generation)
            publication['newer_packet'] = deepcopy(newer)
            assert tuple(newer['open_orders']) == () and newer['trade_generation'] == initial_generation
            if schedule == 'generation_only':
                assert tuple(newer['positions']) == () and tuple(newer['raw_trades']) == ()
                publication['tick'] = engine.lp_tick()
                current_map = {s['session_id']: store.lp_session_revision(s['session_id'], trading=True)
                    for s in store.lp_sessions()}
                publication['current_trade_revision_map'] = current_map
                publication['current_generation'] = store.lp_trade_generation()
                assert lookups and all(r['matched'] == '8' and r['original'] == '20' for r in lookups)
                assert any(g['successful_commit'] and g['parameters'] == [initial_generation]
                    and 'AND generation=?' in g['sql'] for g in generations_committed)
                assert publication['current_generation'] > initial_generation
                assert current_map == old_map, 'Required native generation-only premise; metadata changes may differ'
            else:
                publication['dashboard'] = engine.refresh_lp_dashboard_snapshot()
                assert publication['dashboard']['state'] == 'ready', publication
                publication['tick'] = engine.lp_tick()
                current = store.lp_session(sid)
                publication['current_owned_session'] = deepcopy(current)
                publication['current_trade_revision'] = store.lp_session_revision(sid, trading=True)
                assert publication['current_trade_revision'] != old_map[sid], 'Required changed-owner premise'
                history = current['order_history']['original-monitor']
                assert history['status'] in ('UNKNOWN', 'CANCELED', 'CANCELLED', 'FILLED', 'MATCHED')
                assert Decimal(history['size_matched']) == 8
                assert Decimal(current['buy_filled_quantity']) == Decimal(current['residual_quantity']) == 8
                assert Decimal(current['buy_cost']) == Decimal('3.20')
                assert current['position_reconciled'] is True
                assert Decimal(newer['positions'][0]['size']) == 8
                assert Decimal(newer['positions'][0]['average_price']) == Decimal('.40')
                assert Decimal(newer['positions'][0]['size']) * Decimal(newer['positions'][0]['average_price']) == Decimal('3.20')
            publication['before_release_session'] = deepcopy(store.lp_session(sid))
            publication['before_release_audits'] = deepcopy(store.lp_actions(sid))
            publication['before_release_public'] = deepcopy(engine.lp_auto_state())
        finally:
            release.set()
            try:
                state = running.result(timeout=5)
            finally:
                (tmp_path / 'R2-native-public-proof.json').write_text(json.dumps(dict(
                    rows=rows_observed, wire_reads=wire_reads, lookups=lookups,
                    generation_commits=generations_committed, publication=publication,
                    state=engine.lp_auto_state(), sessions=store.lp_sessions(),
                    audits={s['session_id']: store.lp_actions(s['session_id']) for s in store.lp_sessions()},
                    posts=[p.token_id for p in account.posts], cancels=account.cancels,
                    market_orders=account.market_orders, errors=errors), default=str, indent=2))
    assert not errors, errors
    assert rows_observed[0]['returned_rows_identity'] == rows_observed[0]['native_rows_identity']
    assert account.posts == [] and account.cancels == []
    assert store.lp_actions(sid) == publication['before_release_audits'] == original_audits
    assert state['active_plan'] is None
    assert state['last_round']['actions'] == []
    assert state['last_round']['completed_at'] and state['last_round']['reason'] == 'account_round_invalid'
    completed = datetime.fromisoformat(state['last_round']['completed_at'])
    assert state['plan_wait']['kind'] == 'round'
    assert datetime.fromisoformat(state['plan_wait']['deadline']) == completed + timedelta(seconds=30)
    after = store.lp_session(sid)
    assert after['account_id'] == original['account_id'] and after['owned_order_ids'] == original['owned_order_ids']
    if schedule == 'filled_owner_publication':
        assert after['order_history'] == publication['before_release_session']['order_history']
        assert Decimal(after['buy_filled_quantity']) == Decimal(after['residual_quantity']) == 8
        assert Decimal(after['buy_cost']) == Decimal('3.20') and after['position_reconciled'] is True
    assert state['funds']['as_of'] == publication['before_release_public']['funds']['as_of']
