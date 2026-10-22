"""D2 candidate production uses current account and actual process ownership."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import io
import json
import sqlite3

import pytest
from polymarket.models.clob.order_response import AcceptedOrder

from open_trader import polymarket_lp_market_reader, polymarket_trading
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
from open_trader.polymarket_lp_market_calculation import CalculationPolicy
from open_trader.prediction_arbitrage_execution import PredictionExecutionService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_lp_auto_refill_contract import RefillPublic
from tests.test_lp_d2_integration import seed_running
from tests.test_lp_order_registration_contract import _adapter, _Notifier, WALLET, CONDITION_ID, TOKEN_ID


@contextmanager
def current_runtime(root, monkeypatch, *, budget='40', target=1, attached=True, prime=True):
    clock = [datetime(2026, 10, 16, 1, tzinfo=UTC)]
    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)
    class BusinessTime(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz is not None else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(polymarket_trading, 'datetime', BusinessTime)
    monkeypatch.setattr(polymarket_lp_market_reader, 'datetime', BusinessTime)
    class Public(RefillPublic):
        def list_market_rewards(self, *, condition_id, sponsored):
            reward = super().list_market_rewards(condition_id=condition_id, sponsored=sponsored)[0]
            config = reward.rewards_config[0].model_copy(update=dict(start_date=clock[0]-timedelta(days=1), end_date=clock[0]+timedelta(days=1)))
            return (reward.model_copy(update=dict(rewards_config=(config,))),)
        def list_markets(self, **kwargs):
            return [self.get_market(id='market-1')]
        def list_current_rewards(self, *, sponsored):
            return () if sponsored else self.list_market_rewards(condition_id=CONDITION_ID, sponsored=False)
    public = Public(clock)
    adapter, sdk = _adapter(root, public_client=public)
    def transport(request, **kwargs):
        if request.data:
            body = json.loads(request.data)
            value = dict(history={t: [dict(t=body['start_ts'], p='.400'), dict(t=body['end_ts'], p='.405' if t == TOKEN_ID else '.425')]
                                  for t in body['markets']})
        else:
            assert 'rewards' in request.full_url
            value = dict(data=[dict(condition_id=CONDITION_ID, market_competitiveness='1.2')])
        return io.BytesIO(json.dumps(value).encode())
    adapter._urlopen_fn = transport
    sdk.create_limit_order = lambda **kw: dict(kw, order_type='GTD')
    def post(signed):
        sdk.posts.append(signed)
        return AcceptedOrder(order_id='current-new-buy', status='live', making_amount=Decimal('0'),
                             taking_amount=Decimal('0'), trade_ids=(), transactions_hashes=())
    sdk.post_order = post
    store = PredictionArbitrageStore(root)
    store.lp_save_current_settings(hashlib.sha256(WALLET.casefold().encode()).hexdigest(),
        expected_config_version=0, changes=dict(budget_usd=budget, target_buy_count=target, buy_price_level=1))
    seed_running(store)
    producer = PolymarketLPService(store, adapter, clock=lambda: clock[0])
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='candidate-current', policy=CalculationPolicy(None), utc_now=lambda: clock[0])
    live.load_settings()
    live.initialize(ValidatedStartAdmission('candidate-current', 'offline-permit'))
    execution = PredictionExecutionService(store=store, monitor=object(), trading=adapter, notifier=_Notifier(),
        lock_path=root/'execution.lock', lp=producer, lp_d2=live if attached else None)
    execution._breaker_open = False  # existing startup-clear fixture boundary
    try:
        if attached and prime:
            execution.lp_auto_scheduled_check()  # initial current owner activation, with no candidates
        yield store, producer, live, execution, sdk, clock
    finally:
        assert live.close(timeout=3)
        adapter.close()


def produce(producer):
    assert producer.refresh_price_history()['state'] == 'known'
    assert producer.refresh_competition_cache()['state'] == 'known'
    return producer.refresh_candidates(force=True)


def expected_candidate(producer):
    capture = producer.capture_qualified_pool()
    assert tuple(capture.rows) == (CONDITION_ID,), json.dumps(producer.candidate_snapshot(), default=str)
    assert capture.rows[CONDITION_ID].complete and capture.rows[CONDITION_ID].error is None
    row = producer.candidate_snapshot()['candidates'][0]
    assert row['state'] == 'eligible'
    selected = row['selected_direction']
    assert selected['token_id'] == TOKEN_ID
    assert Decimal(selected['price']) == Decimal('.40')
    assert Decimal(selected['quantity']) == 20
    return tuple(capture.rows)


def add_history(store, kind):
    if kind == 'retired_nleg':
        with sqlite3.connect(store.path) as connection:
            connection.execute("INSERT INTO n_leg_controls(singleton,mode,breaker_open,active_batch_id,total_unsettled_capital_units,updated_at) VALUES(1,'MANUAL',0,'retired-absent',9000000,'2026-10-01T00:00:00Z') ON CONFLICT(singleton) DO UPDATE SET active_batch_id='retired-absent',total_unsettled_capital_units=9000000")
        assert store.n_leg_control()['active_batch_id'] == 'retired-absent'
        return store.n_leg_control()
    store.lp_create_session('obsolete', 'obsolete-request', state='entry_open', payload=dict(
        market_id='old-market', condition_id='old-condition', token_id='old-token', outcome='YES',
        entry_order_id='old-absent', price=None, quantity='20'))
    if kind == 'unknown_action':
        store.lp_upsert_action('obsolete', 'historical-unresolved-buy', state='unknown',
            payload=dict(role='augment', side='BUY', token_id='old-token', quantity='20', post_started=True))
    return dict(session=store.lp_session('obsolete'), actions=store.lp_actions('obsolete'))


@pytest.mark.parametrize('history', ['missing_price', 'unknown_action', 'retired_nleg'])
def test_d2_producer_ignores_legacy_reservations(tmp_path, monkeypatch, history):
    with current_runtime(tmp_path/'baseline', monkeypatch, prime=False) as (_, producer, _, execution, sdk, _):
        produce(producer)
        baseline = expected_candidate(producer)
        execution.lp_auto_scheduled_check()  # actual activation/cleanup round
        execution.lp_auto_scheduled_check()
        assert len(sdk.posts) == 1
    with current_runtime(tmp_path/'history', monkeypatch, prime=False) as (store, producer, live, execution, sdk, clock):
        original = add_history(store, history)
        def prohibited(*args, **kwargs):
            pytest.fail('D2 candidate money must not consult historical reservations')
        assert producer.refresh_price_history()['state'] == 'known'
        assert producer.refresh_competition_cache()['state'] == 'known'
        with monkeypatch.context() as history_patch:
            for name in ('lp_active_sessions', 'n_leg_control', 'n_leg_batch'):
                history_patch.setattr(store, name, prohibited)
            producer.refresh_candidates(force=True)
        assert expected_candidate(producer) == baseline
        clock[0] += timedelta(seconds=31)
        producer.refresh_candidate_recommendations()
        assert expected_candidate(producer) == baseline
        assert producer.capture_qualified_pool().rows[CONDITION_ID].checked_at == clock[0]
        execution.lp_auto_scheduled_check()  # actual activation/cleanup round
        result = execution.lp_auto_scheduled_check()
        assert len(sdk.posts) == 1, result
        assert sdk.posts[0]['token_id'] == TOKEN_ID
        assert (sdk.posts[0]['price'], sdk.posts[0]['size']) == (Decimal('.40'), Decimal('20'))
        assert sdk.cancels == sdk.market_orders == []
        if history == 'retired_nleg':
            assert store.n_leg_control() == original
        else:
            assert dict(session=store.lp_session('obsolete'), actions=store.lp_actions('obsolete')) == original


@pytest.mark.parametrize('invalid', ['incomplete', 'stale', 'identity'])
def test_d2_producer_rejects_invalid_current_account(tmp_path, monkeypatch, invalid):
    from tests.test_lp_order_registration_contract import _open_order
    with current_runtime(tmp_path, monkeypatch) as (_, producer, live, execution, sdk, clock):
        produce(producer)
        expected_candidate(producer)
        adapter = live.trading
        read = adapter.lp_account_snapshot_shared
        def invalid_account(*args, **kwargs):
            value = dict(read(*args, **kwargs))
            if invalid == 'incomplete':
                value['open_orders_complete'] = False
            elif invalid == 'stale':
                value['checked_at'] = clock[0] - timedelta(seconds=61)
            else:
                value['wallet_address'] = value['account_id'] = '0x'+'b'*40
            return value
        monkeypatch.setattr(adapter, 'lp_account_snapshot_shared', invalid_account)
        if invalid == 'incomplete':
            sdk.orders = None
        elif invalid == 'identity':
            sdk.orders = (_open_order('foreign-buy', 'BUY', price='.40', original='20').model_copy(
                update=dict(maker_address='0x'+'b'*40)),)
        else:
            class StaleTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return clock[0]-timedelta(seconds=61)
            monkeypatch.setattr(polymarket_lp_market_reader, 'datetime', StaleTime)
        clock[0] += timedelta(seconds=31)
        producer.refresh_candidate_recommendations()
        execution.lp_auto_scheduled_check()
        assert live.state.snapshot().new_capacity.state != 'known'
        assert sdk.posts == sdk.cancels == sdk.market_orders == []
        snapshot = producer.candidate_snapshot()
        for row in snapshot['candidates']:
            assert row.get('refresh_failed') or row.get('state') != 'eligible' or snapshot['stale']


def test_d2_candidate_gate_preserves_current_send_ownership(tmp_path, monkeypatch):
    from threading import Event, Thread
    from tests.test_lp_d2_integration import settle_control
    with current_runtime(tmp_path, monkeypatch) as (_, producer, live, execution, sdk, clock):
        produce(producer)
        expected_candidate(producer)
        entered, release = Event(), Event()
        results, errors = [], []
        def uncertain_post(signed):
            sdk.posts.append(signed)
            entered.set()
            assert release.wait(6), 'independent actual SDK POST watchdog'
            raise TimeoutError('actual held SDK BUY outcome unknown')
        sdk.post_order = uncertain_post
        def scheduled():
            try:
                results.append(execution.lp_auto_scheduled_check())
            except BaseException as exc:
                errors.append(exc)
        worker = Thread(target=scheduled)
        try:
            worker.start()
            assert entered.wait(3), 'actual SDK POST did not start'
            assert len(sdk.posts) == 1 and sdk.posts[0]['token_id'] == TOKEN_ID
            assert sdk.orders == ()
            clock[0] += timedelta(seconds=31)
            producer.refresh_candidate_recommendations()
            row, = producer.candidate_snapshot()['candidates']
            assert row['refresh_failed'] is True
            with pytest.raises(RuntimeError, match='d2_run_in_progress'):
                execution.lp_auto_scheduled_check()
            assert len(sdk.posts) == 1
            release.set()
            worker.join(3)
            assert not worker.is_alive() and errors == []
            result, = results
            outcome, = result.outcomes
            uncertainty = live.status()['last_buy_uncertainty']
            assert uncertainty.request_id == outcome.identity.request_id
            assert uncertainty.token_id == TOKEN_ID and uncertainty.reason == 'unknown_timeout'
            assert live.state.snapshot().new_capacity.state == 'unknown'
            execution.lp_auto_set_desired_running(False)
            settle_control(live)
            # Same-time cached/old source cannot supersede the actual send floor.
            execution.lp_auto_scheduled_check()
            assert live.state.snapshot().new_capacity.state == 'unknown'
            producer.refresh_candidates(force=True)
            assert len(sdk.posts) == 1
            clock[0] += timedelta(seconds=1)
            execution.lp_auto_scheduled_check()
            assert live.state.snapshot().new_capacity.state == 'known'
            assert live.state.snapshot().buy_reserved.value == 0
            assert len(sdk.posts) == 1
            assert sdk.cancels == sdk.market_orders == []
        finally:
            release.set()
            worker.join(3)
            assert not worker.is_alive()


@pytest.mark.parametrize('budget,capacity,posts', [('14', '8', 1), ('7', '1', 0)], ids=['funded', 'budget_limited'])
def test_d2_current_buy_remaining_counts_once(tmp_path, monkeypatch, budget, capacity, posts):
    from types import SimpleNamespace
    from tests.test_lp_account_reservation_reconciliation import _refill_identity
    from tests.test_lp_order_registration_contract import _open_order
    with current_runtime(tmp_path, monkeypatch, budget=budget, target=2) as (store, producer, live, execution, sdk, clock):
        _, incumbent_condition, incumbent_token = _refill_identity(2)
        _, held_condition, held_token = _refill_identity(3)
        incumbent = _open_order('current-incumbent', 'BUY', price='.40', original='20', matched='5',
            token_id=incumbent_token).model_copy(update=dict(market=incumbent_condition, condition_id=incumbent_condition))
        sell = _open_order('current-sell', 'SELL', price='.60', original='5', token_id=held_token).model_copy(
            update=dict(market=held_condition, condition_id=held_condition))
        sdk.orders = (incumbent, sell)
        sdk.positions = (dict(user=WALLET, condition_id=held_condition, token_id=held_token,
                             size='5', initial_value='2', redeemable=False),)
        sdk.get_balance_allowance = lambda **kw: SimpleNamespace(balance='100000000',
            allowances={sdk.environment.standard_exchange: '100000000'})
        store.lp_create_session('matching-history', 'matching-request', state='entry_open', payload=dict(
            market_id='market-2', condition_id=incumbent_condition, token_id=incumbent_token, outcome='YES',
            entry_order_id='current-incumbent', price='.40', quantity='20', buy_filled_quantity='5'))
        obsolete = add_history(store, 'missing_price')
        matching = store.lp_session('matching-history')
        produce(producer)
        expected_candidate(producer)
        # 100 wallet minus exactly 6 current BUY; SELL and history add no hold.
        assert Decimal(producer.candidate_snapshot()['funnel']['budget']['available_capital']) == 94
        before_sign = []
        sign = sdk.create_limit_order
        def observed_sign(**kwargs):
            before_sign.append(execution.lp_auto_state())
            return sign(**kwargs)
        sdk.create_limit_order = observed_sign
        result = execution.lp_auto_scheduled_check()
        calculation = result.acceptance.observation.calculation
        assert calculation.buy_reserved == 6
        assert calculation.inventory_cost == 2
        assert calculation.new_capacity == Decimal(capacity)
        assert len(sdk.posts) == posts
        if posts:
            state, = before_sign
            assert (Decimal(state['buy_reserved_usd']), Decimal(state['inventory_cost_usd']),
                    Decimal(state['new_capacity_usd'])) == (6, 2, 8)
            assert (sdk.posts[0]['token_id'], sdk.posts[0]['price'], sdk.posts[0]['size']) == (
                TOKEN_ID, Decimal('.40'), Decimal('20'))
        else:
            state = execution.lp_auto_state()
            assert before_sign == [] and Decimal(state['new_capacity_usd']) == 1
        assert sdk.orders == (incumbent, sell)
        assert sdk.cancels == sdk.market_orders == []
        assert store.lp_session('matching-history') == matching
        assert dict(session=store.lp_session('obsolete'), actions=store.lp_actions('obsolete')) == obsolete


def test_non_d2_producer_preserves_legacy_unknown_guard(tmp_path, monkeypatch):
    with current_runtime(tmp_path/'baseline', monkeypatch, attached=False) as (_, producer, _, _, sdk, _):
        produce(producer)
        expected_candidate(producer)
        assert sdk.posts == sdk.cancels == []
    with current_runtime(tmp_path/'history', monkeypatch, attached=False) as (store, producer, _, _, sdk, _):
        original = add_history(store, 'missing_price')
        snapshot = produce(producer)
        assert not producer.capture_qualified_pool().rows
        assert snapshot['candidate_valid_count'] == 0
        assert any(row['code'] == 'account_facts_unknown' for row in snapshot['funnel']['reasons']['trial'])
        assert sdk.posts == sdk.cancels == sdk.market_orders == []
        assert dict(session=store.lp_session('obsolete'), actions=store.lp_actions('obsolete')) == original
