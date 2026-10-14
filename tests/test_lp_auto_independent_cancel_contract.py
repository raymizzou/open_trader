"""BUY plans and exact-ID cancellation have independent public lifetimes."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Event

from polymarket.models.clob.cancel import CancelOrdersResponse

from tests.test_lp_account_reservation_reconciliation import runtime, _refill_identity
from tests.test_lp_auto_refill_contract import RefillPublic, prepare
from tests.test_lp_order_registration_contract import _open_order


def _five_resting(runtime, *, f_price=".40"):
    public = RefillPublic(runtime.clock)
    public.prices[6] = f_price
    public.rates = {1: '1000', 2: '900', 3: '800', 4: '700', 5: '1', 6: '100'}
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='40', count=6, public=public)
    account.orders = tuple(_open_order(f'original-{letter}', 'BUY', price='.40', original='20',
        token_id=_refill_identity(i)[2], outcome='YES').model_copy(update={
            'market': _refill_identity(i)[1], 'condition_id': _refill_identity(i)[1]})
        for i, letter in enumerate('ABCDE', 1))
    account.get_order = lambda *, order_id: next(o for o in account.orders if o.id == order_id)
    lp.refresh_candidate_recommendations()
    return store, adapter, account, lp, engine, public


def test_buy_plan_finishes_while_cancel_is_inflight(runtime):
    store, adapter, account, lp, engine, public = _five_resting(runtime)
    entered, release, finished = Event(), Event(), Event()

    def cancel(*, order_ids):
        account.cancels.append(order_ids)
        entered.set()
        try:
            assert release.wait(5), 'Independent external cancel watchdog'
            return CancelOrdersResponse(canceled=order_ids, not_canceled={})
        finally:
            finished.set()

    account.cancel_orders = cancel
    with ThreadPoolExecutor(max_workers=1) as workers:
        running = workers.submit(engine.lp_auto_run_once, round_id='independent-one')
        try:
            assert entered.wait(5), 'Actual registered E cancel reaches external SDK'
            try:
                state = running.result(timeout=1)
            except FutureTimeout:
                raise AssertionError('BUY plan waits for independent external cancel response') from None
            assert not finished.is_set()
            assert state['active_plan'] is None and state['last_round']['completed_at']
            assert all(a['kind'] == 'buy' for a in state['last_round']['actions'])
            assert account.posts == []
            assert account.cancels == [('original-E',)]
            assert {o.id for o in account.orders} == {f'original-{k}' for k in 'ABCDE'}
            assert state['slots']['occupied'] == 5
            assert Decimal(state['funds']['buy_reserved_usd']) == 40
            completed = datetime.fromisoformat(state['last_round']['completed_at'])
            assert state['plan_wait'] is None
            audits = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])
                      if a.get('order_id') == 'original-E' and a.get('role') == 'reconciliation_cancel']
            assert len(audits) == 1 and audits[0]['state'] == 'pending' and audits[0]['post_started']
        finally:
            release.set()
            assert finished.wait(5), 'External sender completes before test teardown'
            running.result(timeout=5)

import pytest
from copy import deepcopy
from time import monotonic


def _wait_audits(store, predicate):
    deadline = monotonic() + 5
    while True:
        audits = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])
                  if a.get('role') == 'reconciliation_cancel']
        if predicate(audits):
            return audits
        assert monotonic() < deadline, 'Independent durable cancel receipt watchdog'
        Event().wait(.01)


def _wait_round_cancels(store, state, *, expected_states=None):
    round_id = state['last_round']['round_id']
    registration = state['last_round'].get('cancel_registration')
    if not registration or not registration.get('completed'):
        assert not expected_states, 'Explicit receipt expectations require completed registration'
        return []
    expected = set(registration['sessions'])
    expected_states = dict(expected_states or {})
    assert set(expected_states) <= expected, 'Typed expectations must identify registered orders'
    assert all(value in ('accepted', 'rejected', 'unknown') for value in expected_states.values())
    rows = _wait_audits(store, lambda rows: {a['order_id'] for a in rows
        if a.get('rotation_round_id') == round_id} == expected
        and len([a for a in rows if a.get('rotation_round_id') == round_id]) == len(expected)
        and all(a['session_id'] == registration['sessions'][a['order_id']]
            and a['action_id'] and a['action_key']
            and a['state'] in ((expected_states[a['order_id']],) if a['order_id'] in expected_states
                else ('accepted', 'rejected'))
            for a in rows if a.get('rotation_round_id') == round_id))
    return [a for a in rows if a.get('rotation_round_id') == round_id]


@pytest.mark.parametrize('scenario', ['still_outside', 'back_in_top_five', 'late_cancel'])
def test_each_cycle_selects_current_orders_without_cancel_memory(runtime, scenario):
    store, adapter, account, lp, engine, public = _five_resting(runtime)
    entered, release, finished, second_entered = Event(), Event(), Event(), Event()
    original_e = next(o for o in account.orders if o.id == 'original-E')
    exact = account.get_order
    account.get_order = lambda *, order_id: (original_e.model_copy(update={'status': 'CANCELED'})
        if order_id == 'original-E' and not any(o.id == order_id for o in account.orders)
        else exact(order_id=order_id))

    def cancel(*, order_ids):
        account.cancels.append(order_ids)
        first = len(account.cancels) == 1
        if not first:second_entered.set()
        if first:
            entered.set()
        try:
            assert release.wait(5), 'Independent original cancel watchdog'
            if scenario == 'late_cancel':
                account.orders = tuple(o for o in account.orders if o.id not in order_ids)
                return CancelOrdersResponse(canceled=order_ids, not_canceled={})
            raise TimeoutError('Independent cancel receipt remains UNKNOWN')
        finally:
            if first:
                finished.set()

    account.cancel_orders = cancel
    try:
        first = engine.lp_auto_run_once(round_id='current-one')
        assert entered.wait(5)
        assert first['active_plan'] is None and not finished.is_set()
        first_audits = _wait_audits(store, lambda rows: len(rows) == 1)
        original_key = first_audits[0]['action_key']
        if scenario == 'back_in_top_five':
            public.rates[5], public.rates[6] = '2000', '.5'
        if scenario == 'late_cancel':
            release.set()
            assert finished.wait(5)
            _wait_audits(store, lambda rows: all(a['state'] == 'accepted' for a in rows))
        runtime.clock[0] = datetime.fromisoformat(first['last_round']['completed_at']) + timedelta(seconds=1)
        lp.refresh_candidate_recommendations()
        second = engine.lp_auto_run_once(round_id='current-two')
        assert second['last_round']['round_id'] == 'current-two'
        assert second['active_plan'] is None
        assert all(a['kind'] == 'buy' for a in second['last_round']['actions'])
        if scenario == 'still_outside':
            assert second_entered.wait(5), 'Distinct current-cycle SDK cancel actually entered'
            audits = _wait_audits(store, lambda rows: len(rows) == 2 and all(a.get('post_started') for a in rows))
            assert len({a['action_key'] for a in audits}) == 2
            assert {a['rotation_round_id'] for a in audits} == {'current-one', 'current-two'}
            assert account.cancels == [('original-E',), ('original-E',)]
            assert account.posts == []
        elif scenario == 'back_in_top_five':
            assert account.cancels == [('original-E',)]
            assert len(_wait_audits(store, lambda rows: len(rows) >= 1)) == 1
            assert account.posts == []
            assert any(o.id == 'original-E' for o in account.orders)
        else:
            assert account.cancels == [('original-E',)]
            assert [p.token_id for p in account.posts] == [_refill_identity(6)[2]]
            new_f, = [o for o in account.orders if o.token_id == _refill_identity(6)[2]]
            assert new_f.id != 'original-E'
            assert second['slots']['occupied'] == 5
        assert original_key in {a['action_key'] for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])}
        assert Decimal(second['funds']['buy_reserved_usd']) == 40
    finally:
        release.set()
        assert finished.wait(5)
        _wait_audits(store, lambda rows: all(a['state'] != 'pending' for a in rows))

        sender_deadline=monotonic()+5
        while True:
            try:
                lp.drain_submissions(timeout=.05)
                break
            except RuntimeError as exc:
                assert monotonic()<sender_deadline,str(exc)
                Event().wait(.01)

from polymarket.models.data.portfolio import Position
from tests.test_lp_order_registration_contract import WALLET


def test_partially_filled_buy_can_rotate_without_selling_inventory(runtime):
    store, adapter, account, lp, engine, public = _five_resting(runtime, f_price='.20')
    original_e = next(o for o in account.orders if o.id == 'original-E').model_copy(
        update={'size_matched': Decimal('8')})
    account.orders = tuple(original_e if o.id == original_e.id else o for o in account.orders)
    account.positions = (Position(conditionId=_refill_identity(5)[1], proxyWallet=WALLET,
        asset=_refill_identity(5)[2], size=Decimal('8'), avgPrice=Decimal('.40'), outcome='YES'),)
    entered, release, finished = Event(), Event(), Event()
    exact = account.get_order
    account.get_order = lambda *, order_id: (original_e.model_copy(update={'status': 'CANCELED'})
        if order_id == 'original-E' and not any(o.id == order_id for o in account.orders)
        else exact(order_id=order_id))

    def cancel(*, order_ids):
        account.cancels.append(order_ids)
        entered.set()
        try:
            assert release.wait(5), 'Independent partially filled cancel watchdog'
            account.orders = tuple(o for o in account.orders if o.id not in order_ids)
            return CancelOrdersResponse(canceled=order_ids, not_canceled={})
        finally:
            finished.set()

    account.cancel_orders = cancel
    lp.refresh_candidate_recommendations()
    try:
        first = engine.lp_auto_run_once(round_id='partial-one')
        assert entered.wait(5), 'Partially filled E remainder must be cancelable'
        assert account.cancels == [('original-E',)] and account.posts == account.market_orders == []
        assert first['slots']['occupied'] == 5
        assert Decimal(first['funds']['buy_reserved_usd']) == Decimal('36.8')
        assert Decimal(first['funds']['inventory_cost_usd']) == Decimal('3.2')
        release.set()
        assert finished.wait(5)
        _wait_audits(store, lambda rows: all(a['state'] == 'accepted' for a in rows))
        runtime.clock[0] = datetime.fromisoformat(first['last_round']['completed_at']) + timedelta(seconds=1)
        lp.refresh_candidate_recommendations()
        second = engine.lp_auto_run_once(round_id='partial-two')
        assert [p.token_id for p in account.posts] == [_refill_identity(6)[2]], second['last_round']
        assert account.market_orders == [] and all(p.side == 'BUY' for p in account.posts)
        assert account.positions[0].size == 8 and account.positions[0].avg_price == Decimal('.40')
        assert second['slots']['occupied'] == 5
        assert Decimal(second['funds']['buy_reserved_usd']) == 36
        assert Decimal(second['funds']['inventory_cost_usd']) == Decimal('3.2')
        assert Decimal(second['funds']['buy_reserved_usd']) + Decimal(second['funds']['inventory_cost_usd']) == Decimal('39.2')
    finally:
        release.set()
        if entered.is_set():
            assert finished.wait(5)
            _wait_audits(store, lambda rows: all(a['state'] != 'pending' for a in rows))

from polymarket.models.clob.order_book import OrderBookLevel


def _protected_f(runtime):
    public = RefillPublic(runtime.clock)
    public.rates = {i: ('2000' if i == 6 else '1') for i in range(1, 7)}
    book = public.get_order_book
    depth = [Decimal('1000')]
    def current_book(*, token_id):
        result = book(token_id=token_id)
        if token_id == _refill_identity(6)[2]:
            return result.model_copy(update={'bids': (
                OrderBookLevel(price=Decimal('.40'), size=depth[0]),
                OrderBookLevel(price=Decimal('.39'), size=Decimal('1000')))})
        return result
    public.get_order_book = current_book
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='8', target=1, count=6, public=public)
    first = engine.lp_auto_run_once(round_id='initial-f')
    assert [p.token_id for p in account.posts] == [_refill_identity(6)[2]]
    original, = account.orders
    closed = {}
    account.get_order = lambda *, order_id: next((o for o in account.orders if o.id == order_id), closed.get(order_id))
    def cancel(*, order_ids):
        account.cancels.append(order_ids)
        for order in account.orders:
            if order.id in order_ids:
                closed[order.id] = order.model_copy(update={'status': 'CANCELED'})
        account.orders = tuple(o for o in account.orders if o.id not in order_ids)
        return CancelOrdersResponse(canceled=order_ids, not_canceled={})
    account.cancel_orders = cancel
    return store, account, lp, engine, public, depth, original, first


def test_protected_candidate_can_reenter_with_new_order_baseline(runtime):
    store, account, lp, engine, public, depth, original, first = _protected_f(runtime)
    try:
        depth[0] = Decimal('20')
        runtime.clock[0] += timedelta(seconds=1)
        deadline = monotonic() + 5
        while not account.cancels:
            protection = engine.lp_tick()
            assert monotonic() < deadline, protection
        assert account.cancels == [(original.id,)], protection
        assert account.orders == ()
        # A venue callback can return before its native receipt is committed.
        lp.drain_submissions(timeout=5)
        old_session, = store.lp_sessions()
        old_audits = deepcopy(store.lp_actions(old_session['session_id']))
        assert any(a.get('role') == 'entry-protection-cancel' for a in old_audits), old_audits
        depth[0] = Decimal('1500')
        # Follow the completed protection event, never the historical BUY clock.
        runtime.clock[0] += timedelta(seconds=1)
        # The ordinary planning reader does not own terminal/fill proof. Publish
        # the real exact-result/full-account recovery before asking for a new BUY.
        lp.reconcile_facts(old_session['session_id'])
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        recovered = engine.lp_auto_state()
        assert recovered['slots']['occupied'] == 0 and recovered['funds']['status'] == 'known'
        assert Decimal(recovered['funds']['buy_reserved_usd']) == 0
        assert Decimal(recovered['funds']['inventory_cost_usd']) == 0
        # External venue allocates a new ID to the next logical BUY.
        post = account.post_order
        def new_post(signed):
            result = post(signed)
            oid = 'f-reentry-new'
            account.orders = tuple(o.model_copy(update={'id': oid}) if o.id == result['order_id'] else o for o in account.orders)
            return {**result, 'order_id': oid}
        account.post_order = new_post
        lp.refresh_candidate_recommendations()
        second = engine.lp_auto_run_once(round_id='reentry-f')
        assert len(account.posts) == 2, second['last_round']
        new_order, = account.orders
        assert new_order.id == 'f-reentry-new' and new_order.id != original.id
        assert new_order.token_id == original.token_id
        new_session = next(s for s in store.lp_sessions() if s.get('entry_order_id') == new_order.id)
        assert new_session['session_id'] != old_session['session_id']
        baseline = new_session['queue_protection']
        assert Decimal(baseline['baseline_front']) == 1500, baseline
        assert baseline['levels']['0.40']['order_id'] == new_order.id
        assert store.lp_actions(old_session['session_id']) == old_audits
        assert account.cancels == [(original.id,)] and account.market_orders == []
    finally:
        lp.drain_submissions(timeout=5)


def test_protection_and_rotation_keep_independent_cancel_receipts(runtime):
    store, account, lp, engine, public, depth, original, first = _protected_f(runtime)
    entered, release, finished = Event(), Event(), Event()
    def cancel(*, order_ids):
        account.cancels.append(order_ids)
        if len(account.cancels) == 1:
            entered.set()
            try:
                assert release.wait(5), 'Independent original protection watchdog'
                return CancelOrdersResponse(canceled=order_ids, not_canceled={})
            finally:
                finished.set()
        return CancelOrdersResponse(canceled=(), not_canceled={original.id: 'already canceling'})
    account.cancel_orders = cancel
    depth[0] = Decimal('20')
    runtime.clock[0] += timedelta(seconds=1)
    def monitor():
        deadline = monotonic() + 5
        while not entered.is_set():
            result = engine.lp_tick()
            if not entered.is_set():
                assert monotonic() < deadline, result
        return result
    with ThreadPoolExecutor(max_workers=1) as workers:
        protection = workers.submit(monitor)
        try:
            assert entered.wait(5)
            before = deepcopy(store.lp_sessions()[0]['queue_protection'])
            public.rates[1], public.rates[6] = '3000', '1'
            depth[0] = Decimal('1000')
            # Maintenance refreshes source facts at its real 30-second lead.
            # Advance from the pending protection event, not the earlier BUY.
            runtime.clock[0] += timedelta(seconds=31)
            lp.refresh_candidate_recommendations()
            rotation = engine.lp_auto_run_once(round_id='independent-rotation')
            assert rotation['active_plan'] is None and not finished.is_set()
            audits = _wait_audits(store, lambda rows: any(a.get('rotation_round_id') == 'independent-rotation'
                and a['state'] == 'rejected' for a in rows))
            rotated, = [a for a in audits if a.get('rotation_round_id') == 'independent-rotation']
            assert rotated['order_id'] == original.id and rotated['reason'] == 'already canceling'
            assert account.cancels == [(original.id,), (original.id,)]
            current = store.lp_sessions()[0]
            # Account observation may add a price-key representation. The
            # original protection's exact bucket and group controls survive.
            for key, value in before.items():
                if key == 'levels':
                    for level, bucket in value.items():
                        assert current['queue_protection']['levels'][level] == bucket
                else:
                    assert current['queue_protection'][key] == value
            assert not current.get('yield_rotation_cancel_owners')
            assert account.posts and len(account.posts) == 1
            release.set()
            assert finished.wait(5)
            protection.result(timeout=5)
            lp.drain_submissions(timeout=5)
            final_audits = store.lp_actions(current['session_id'])
            own_protection, = [a for a in final_audits if a.get('role') == 'entry-protection-cancel']
            assert own_protection['state'] == 'accepted' and own_protection['canceled'] == [original.id]
            assert next(a for a in final_audits if a['action_key'] == rotated['action_key'])['state'] == 'rejected'
            assert engine.lp_auto_state()['slots']['occupied'] == 1
        finally:
            release.set()
            try:
                assert finished.wait(5)
                protection.result(timeout=5)
            finally:
                lp.drain_submissions(timeout=5)

import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import zlib
from polymarket.models.clob.account import OpenOrder
from tests.test_lp_account_reservation_reconciliation import _CandidateSDKPublic
from tests.test_lp_auto_current_facts_cycle import ColdPublic, _prepare_public_inputs


@pytest.mark.parametrize('scenario', ['legacy_fixture', 'interrupted_request'])
def test_restart_and_legacy_rotation_history_never_authorize_old_cancel(runtime, tmp_path, monkeypatch, scenario):
    if scenario == 'legacy_fixture':
        fixture_path = Path(__file__).parent / 'fixtures/lp_auto_terminal_cancel_owners_0906.json'
        fixture_bytes = fixture_path.read_bytes()
        assert hashlib.sha256(fixture_bytes).hexdigest() == '16b4b00c9833bc8129abef4d1a9a3567c4490acc70ba45439a7e6aec5db34260'
        fixture = json.loads(fixture_bytes)
        raw = zlib.decompress(base64.b64decode(fixture['database']))
        assert hashlib.sha256(raw).hexdigest() == fixture['database_sha256']
        database = tmp_path / 'state.sqlite/prediction_arbitrage/prediction_arbitrage.sqlite3'
        database.parent.mkdir(parents=True)
        database.write_bytes(raw)
        runtime.clock[0] = datetime.fromisoformat(fixture['business_time']) + timedelta(seconds=61)
        originals = tuple(OpenOrder.model_validate(o) for o in fixture['external_orders'])
        terminal = {o['id']: OpenOrder.model_validate(o) for o in fixture['exact_terminal_orders']}
        public = _CandidateSDKPublic(runtime.clock[0])
        store, adapter, account, lp, engine = runtime(public_client=public, orders=originals)
        account.get_order = lambda *, order_id: next((o for o in originals if o.id == order_id), terminal.get(order_id))
        original_audit = deepcopy(fixture['original_audit'])
        result = engine.lp_auto_run_once(round_id='fresh-after-legacy')
        _wait_audits(store, lambda rows: all(a['state'] != 'pending' for a in rows))
        assert all('rank-c' not in ids for ids in account.cancels), 'Old terminal request never sends again'
        current_audit = next(a for a in store.lp_actions(original_audit['session_id'])
            if a['action_key'] == original_audit['action_key'])
        assert current_audit == original_audit
        assert result['active_plan'] is None
        assert all(a['kind'] == 'buy' for a in result['last_round']['actions'])
        new_audits = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])
            if a.get('rotation_round_id') == 'fresh-after-legacy']
        assert all(a['order_id'] in {o.id for o in originals} for a in new_audits)
        assert fixture_path.read_bytes() == fixture_bytes
    else:
        store, adapter, account, lp, engine, public = _five_resting(runtime)
        native_connect = sqlite3.connect
        committed = Event()
        class InterruptedConnection(sqlite3.Connection):
            registered = False
            def execute(self, sql, parameters=()):
                result = super().execute(sql, parameters)
                if sql.startswith('INSERT INTO lp_actions') and any(
                        isinstance(value, str) and 'reconciliation_cancel' in value
                        and 'before-exit' in value for value in parameters):
                    self.registered = True
                if sql == 'COMMIT' and self.registered:
                    committed.set()
                    raise KeyboardInterrupt('Process ends after native independent request registration COMMIT')
                return result
        def connect(database, *args, **kwargs):
            if Path(database).resolve() == store.path.resolve():
                kwargs['factory'] = InterruptedConnection
            return native_connect(database, *args, **kwargs)
        with monkeypatch.context() as observer:
            observer.setattr(sqlite3, 'connect', connect)
            with pytest.raises(KeyboardInterrupt):
                engine.lp_auto_run_once(round_id='before-exit')
        assert committed.is_set() and account.cancels == account.posts == []
        originals = account.orders
        original_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
        pending = [a for rows in original_audits.values() for a in rows if a.get('role') == 'reconciliation_cancel']
        assert len(pending) == 1 and pending[0]['state'] == 'pending' and not pending[0].get('post_started')
        adapter.close()
        cold = ColdPublic(runtime.clock)
        cold.rates = {**public.rates, 5: '2000', 6: '.5'}
        store, adapter, account, lp, engine = runtime(public_client=cold, orders=originals)
        account.get_order = lambda *, order_id: next(o for o in account.orders if o.id == order_id)
        _prepare_public_inputs(lp, adapter)
        runtime.clock[0] += timedelta(seconds=61)
        result = engine.lp_auto_run_once(round_id='fresh-after-exit')
        assert account.cancels == account.posts == [], result['last_round']
        assert result['active_plan'] is None and result['last_round']['round_id'] == 'fresh-after-exit'
        for sid, audits in original_audits.items():
            assert store.lp_actions(sid) == audits
        assert result['slots']['occupied'] == 5 and Decimal(result['funds']['buy_reserved_usd']) == 40

from open_trader.prediction_runtime import PredictionRuntime, PredictionRuntimeOwnershipError
from open_trader.polymarket_trading import PolymarketTradingClient
from tests.test_lp_order_registration_contract import _adapter


def test_independent_cancel_preserves_native_inflight_lifetime(runtime, tmp_path, monkeypatch):
    # Startup's account-age clock shares the fixture's controlled business
    # time. Native thread/Event watchdogs retain real monotonic time.
    from open_trader import prediction_arbitrage_execution as execution_module
    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)
    class BusinessDateTime(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return runtime.clock[0] if tz is not None else runtime.clock[0].replace(tzinfo=None)
    monkeypatch.setattr(execution_module, 'datetime', BusinessDateTime)
    public = ColdPublic(runtime.clock)
    public.rates = {1: '1000', 2: '900', 3: '800', 4: '700', 5: '1', 6: '100'}
    originals = tuple(_open_order(f'original-{letter}', 'BUY', price='.40', original='20',
        token_id=_refill_identity(i)[2]).model_copy(update={
            'market': _refill_identity(i)[1], 'condition_id': _refill_identity(i)[1]})
        for i, letter in enumerate('ABCDE', 1))
    adapter, account = _adapter(tmp_path, orders=originals, public_client=public)
    entered, release, finished, closed = Event(), Event(), Event(), Event()
    def cancel(*, order_ids):
        account.cancels.append(order_ids)
        entered.set()
        try:
            assert release.wait(5), 'Independent runtime-owned cancel watchdog'
            return CancelOrdersResponse(canceled=order_ids, not_canceled={})
        finally:
            finished.set()
    account.cancel_orders = cancel
    account.get_order = lambda *, order_id: next(o for o in account.orders if o.id == order_id)
    native_close = adapter.close
    def observed_close():
        closed.set()
        return native_close()
    adapter.close = observed_close
    account.is_gasless_ready = lambda: True
    monkeypatch.setattr(PolymarketTradingClient, 'from_keychain', classmethod(lambda cls, config: adapter))
    config = tmp_path / 'prediction.json'
    config.write_text(json.dumps({'signer_address': WALLET, 'wallet_address': WALLET}))
    # All HTTP lanes use the established offline external carrier, including
    # runtime background competition/history reads.
    from tests.test_lp_auto_current_facts_cycle import _prepare_public_inputs
    from contextlib import contextmanager
    from types import SimpleNamespace
    @contextmanager
    def http(request, **kwargs):
        if request.data:
            body = json.loads(request.data)
            payload = {'history': {t: [dict(t=body['start_ts'], p='.40'), dict(t=body['end_ts'], p='.401')]
                for t in body['markets']}}
        else:
            payload = {'data': [dict(condition_id=_refill_identity(i)[1], market_competitiveness='2.5')
                for i in range(1, 7)], 'next_cursor': 'LTE='}
        yield SimpleNamespace(read=lambda: json.dumps(payload).encode())
    adapter._urlopen_fn = http
    markets = public.list_markets
    class Pages:
        def __init__(self, rows): self.rows = rows
        def iter_items(self): yield from self.rows
        def first_page(self): return SimpleNamespace(items=self.rows, has_more=False)
    public.list_markets = lambda **kwargs: Pages(markets(**kwargs))
    host = PredictionRuntime(data_dir=tmp_path / 'host', prediction_config_path=config,
        dashboard_url='http://127.0.0.1:8766/', n_leg_paused=True, history_clock=lambda: runtime.clock[0],
        notifier=SimpleNamespace(_notifiers=(SimpleNamespace(channel='macos'), SimpleNamespace(channel='feishu'))))
    try:
        host.start()
        assert host.state == 'RUNNING'
        engine, lp, store = host.execution, host.lp, host.store
        engine.lp_auto_configure({'budget_usd': '40', 'target_buy_count': 5})
        deadline = monotonic() + 5
        while lp.preparation_snapshot().get('state') != 'ready':
            assert monotonic() < deadline, lp.preparation_snapshot()
            Event().wait(.01)
        lp.refresh_competition_cache()
        lp.refresh_candidates(force=True)
        engine.lp_auto_set_desired_running(True)
        waiting = engine.lp_auto_state()['plan_wait']
        if waiting:
            runtime.clock[0] = datetime.fromisoformat(waiting['deadline']) + timedelta(microseconds=1)
        engine.lp_auto_run_once(round_id='runtime-current')
        assert entered.wait(5)
        deadline = monotonic() + 5
        while engine.lp_auto_state()['active_plan'] is not None:
            assert monotonic() < deadline, 'BUY plan completion watchdog'
            Event().wait(.01)
        assert not finished.is_set()
        engine.lp_auto_set_desired_running(False)
        assert not finished.is_set() and not closed.is_set()
        with pytest.raises(RuntimeError, match='sender did not stop'):
            host.stop()
        assert host.state == 'STOPPING' and not closed.is_set()
        assert host.store is store and host.lp is lp
        contender = PredictionRuntime(data_dir=tmp_path / 'host', prediction_config_path=config,
            dashboard_url='http://127.0.0.1:8766/', n_leg_paused=True)
        with pytest.raises(PredictionRuntimeOwnershipError):
            contender.start()
        assert account.cancels == [('original-E',)]
        release.set()
        assert finished.wait(5)
        _wait_audits(store, lambda rows: all(a['state'] != 'pending' for a in rows))
        deadline = monotonic() + 5
        while host.state != 'STOPPED':
            try:
                host.stop()
            except RuntimeError:
                assert monotonic() < deadline, 'Actual sender drain and runtime shutdown watchdog'
        assert closed.is_set() and account.cancels == [('original-E',)]
    finally:
        release.set()
        host.stop()
        adapter.close()
