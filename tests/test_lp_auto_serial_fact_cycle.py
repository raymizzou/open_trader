"""Current serial facts through the native SDK adapter and SQLite Store."""

from datetime import datetime, timedelta
from decimal import Decimal
import pytest
from types import SimpleNamespace
import json
import sqlite3
import logging
from pathlib import Path
from threading import Event, Thread, Lock
import fcntl
from urllib.error import HTTPError
from concurrent.futures import Future
from timing_support import run_test_in_subprocess

from polymarket.models.clob.order_book import OrderBookLevel

from tests.test_lp_account_reservation_reconciliation import runtime as account_runtime, _refill_identity, _position
from tests.test_lp_auto_refill_contract import RefillPublic, prepare
from tests.test_lp_order_registration_contract import _open_order
from tests.test_lp_auto_current_facts_cycle import ColdPublic, _prepare_public_inputs


@pytest.fixture
def runtime(account_runtime):
    """Drain LP senders before the shared adapter fixture closes its clients."""
    instances = []

    def build(**kwargs):
        result = account_runtime(**kwargs)
        instances.append(result[3])
        return result

    build.clock = account_runtime.clock
    yield build
    errors = []
    for lp in reversed(instances):
        try:
            lp.drain_submissions()
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


class SerialPublic(RefillPublic):
    """The approved symmetric single-level external reward/book producer."""

    def get_order_book(self, *, token_id):
        book = super().get_order_book(token_id=token_id)
        return book.model_copy(update={
            'bids': (OrderBookLevel(price=Decimal('.40'), size=Decimal('1000')),),
            'asks': (OrderBookLevel(price=Decimal('.42'), size=Decimal('1000')),),
        })

    def list_market_rewards(self, *, condition_id, sponsored):
        return tuple(r.model_copy(update={'rewards_max_spread': 5}) for r in
            super().list_market_rewards(condition_id=condition_id, sponsored=sponsored))


def _resting(index, oid=None, *, matched='0'):
    _, condition, token = _refill_identity(index)
    return _open_order(oid or f'resting-{index}', 'BUY', price='.40', original='20',
        matched=matched, token_id=token, outcome='YES').model_copy(update={
            'market': condition, 'condition_id': condition})


def _yield(store, oid):
    session, = [s for s in store.lp_sessions() if oid in s.get('owned_order_ids', ())]
    return session.get('buy_yield_observations', {}).get(oid)


def test_round_publishes_current_yields_even_when_values_unchanged(runtime):
    public = SerialPublic(runtime.clock)
    orders = (_resting(1), _resting(2))
    store, adapter, account, lp, engine = runtime(public_client=public, orders=orders)
    engine.lp_auto_configure({'budget_usd': '16', 'target_buy_count': 2})
    engine.lp_auto_set_desired_running(True)
    previous = None
    # Literals are hand-worked: share 1/149, capital 8, daily reward 24/48.
    for index, (daily, expected) in enumerate([
            ('24', '0.083893'), ('48', '0.167785'), ('48', '0.167785')]):
        public.rates = {1: daily, 2: daily}
        runtime.clock[0] += timedelta(seconds=1)
        state = engine.lp_auto_run_once(round_id=f'unchanged-{index}')
        observed = _yield(store, orders[0].id)
        assert observed is not None, 'Every full round must persist current resting BUY yield'
        assert observed['state'] == 'known'
        assert Decimal(observed['yield_pct_per_hour']).quantize(Decimal('.000001')) == Decimal(expected)
        assert datetime.fromisoformat(observed['checked_at'].replace('Z', '+00:00')) == runtime.clock[0]
        assert observed['order_id'] == orders[0].id
        assert Decimal(observed['quantity']) == 20
        assert observed['checked_at'] != previous
        previous = observed['checked_at']
        assert _yield(store, orders[1].id)['state'] == 'known'
        assert state['last_round']['round_id'] == f'unchanged-{index}'
        assert state['last_round']['actions'] == []
        assert account.posts == [] and account.market_orders == [] and account.cancels == []
        assert state['slots']['occupied'] == 2
        assert Decimal(state['funds']['buy_reserved_usd']) == 16


def test_one_unknown_yield_does_not_block_other_actions(runtime):
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(
        runtime, budget='40', target=5, count=5, public=public)
    account.orders = (_resting(1), _resting(2))
    rewards = public.list_market_rewards

    def reward(**kwargs):
        if kwargs['condition_id'] == _refill_identity(1)[1]:
            raise TimeoutError('offline A reward unavailable')
        return rewards(**kwargs)

    public.list_market_rewards = reward
    unknown_since = None
    for index in range(2):
        runtime.clock[0] += timedelta(seconds=1)
        state = engine.lp_auto_run_once(round_id=f'one-unknown-{index}')
        lp.drain_submissions()
        unknown = _yield(store, 'resting-1')
        assert unknown['state'] == 'unknown' and unknown['error']
        assert _yield(store, 'resting-2')['state'] == 'known'
        session, = [s for s in store.lp_sessions() if 'resting-1' in s.get('owned_order_ids', ())]
        since = session['buy_yield_unknown']['resting-1']['unknown_since']
        assert unknown_since is None or since == unknown_since
        unknown_since = since
        assert {p.token_id for p in account.posts} == {_refill_identity(i)[2] for i in (3, 4, 5)}
        assert len(account.posts) == 3, 'No original request is replayed on the next current round'
        assert state['slots']['occupied'] == 5
        assert Decimal(state['funds']['buy_reserved_usd']) == 40
        assert account.cancels == [] and account.market_orders == []
        assert state['last_round']['round_id'] == f'one-unknown-{index}'


def test_all_unknown_resting_yields_do_not_hide_known_free_capacity(runtime):
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(
        runtime, budget='40', target=5, count=5, public=public)
    account.orders = (_resting(1), _resting(2))
    reward = public.list_market_rewards

    def unavailable(**kwargs):
        if kwargs['condition_id'] in {_refill_identity(i)[1] for i in (1, 2)}:
            raise TimeoutError('offline resting rewards unavailable')
        return reward(**kwargs)

    public.list_market_rewards = unavailable
    state = engine.lp_auto_run_once(round_id='all-resting-unknown')
    lp.drain_submissions()
    assert all(_yield(store, f'resting-{i}')['state'] == 'unknown' for i in (1, 2))
    assert {p.token_id for p in account.posts} == {_refill_identity(i)[2] for i in (3, 4, 5)}
    assert len(account.posts) == 3
    assert all(p.maker_amount == 8000000 and p.taker_amount == 20000000 for p in account.posts)
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert account.cancels == [] and account.market_orders == []
    runtime.clock[0] += timedelta(seconds=1)
    current = engine.lp_auto_run_once(round_id='all-resting-unknown-next')
    assert current['last_round']['round_id'] == 'all-resting-unknown-next'
    assert len(account.posts) == 3 and len(account.orders) == 5


def test_invalid_candidate_is_excluded_before_plan_freezes(runtime):
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(
        runtime, budget='40', target=5, count=6, public=public)
    account.orders = (_resting(1), _resting(2))
    reward = public.list_market_rewards

    def unavailable(**kwargs):
        if kwargs['condition_id'] == _refill_identity(3)[1]:
            raise TimeoutError('offline C qualification unavailable')
        return reward(**kwargs)

    public.list_market_rewards = unavailable
    state = engine.lp_auto_run_once(round_id='invalid-before-freeze')
    lp.drain_submissions()
    assert {p.token_id for p in account.posts} == {_refill_identity(i)[2] for i in (4, 5, 6)}
    assert len(account.posts) == 3
    assert {a['token_id'] for a in state['last_round']['actions']} == {_refill_identity(i)[2] for i in (4, 5, 6)}
    assert all(a['state'] == 'success' for a in state['last_round']['actions'])
    assert state['slots']['occupied'] == 5
    assert Decimal(state['funds']['buy_reserved_usd']) == 40
    assert any(b['condition_id'] == _refill_identity(3)[1] for b in state['last_round']['blocked'])
    assert account.cancels == [] and account.market_orders == []


@pytest.mark.parametrize('candidate_result', ['empty', 'unavailable'])
def test_empty_or_unavailable_candidates_still_publish_current_yields(runtime, candidate_result):
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='40', target=5,
        count=0 if candidate_result == 'empty' else 4, public=public)
    account.orders = (_resting(1), _resting(2))
    if candidate_result == 'empty':
        public.list_current_rewards = lambda **kwargs: ()
        assert lp.refresh_price_history()['state'] == 'known'
        candidate_state = lp.refresh_candidates()
        assert candidate_state['state'] == 'ready', candidate_state
    else:
        reward = public.list_market_rewards

        def unavailable(**kwargs):
            if kwargs['condition_id'] in {_refill_identity(i)[1] for i in (3, 4)}:
                raise TimeoutError('offline candidate qualification unavailable')
            return reward(**kwargs)

        public.list_market_rewards = unavailable
    previous = None
    for index in range(2):
        public.rates = {1: str(24 * (index + 1)), 2: '48'}
        runtime.clock[0] += timedelta(seconds=1)
        state = engine.lp_auto_run_once(round_id=f'{candidate_result}-{index}')
        observation = _yield(store, 'resting-1')
        assert observation['state'] == 'known'
        assert observation['checked_at'] != previous
        previous = observation['checked_at']
        assert state['last_round']['round_id'] == f'{candidate_result}-{index}'
        assert state['last_round']['actions'] == []
        assert account.posts == [] and account.market_orders == []
        assert state['slots']['occupied'] == 2
        assert Decimal(state['funds']['buy_reserved_usd']) == 16
        if candidate_result == 'empty':
            assert state['last_round']['candidate_filter']['counts']['pool_total'] == 0
        else:
            assert len([b for b in state['last_round']['blocked'] if b['condition_id'] in
                {_refill_identity(i)[1] for i in (3, 4)}]) == 2


@pytest.mark.parametrize('failure', ['balance', 'allowance'])
def test_unusable_balance_blocks_only_unfunded_actions(runtime, failure, tmp_path):
    public = ColdPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='24', target=3,
        count=3, public=public)
    account.orders = (_resting(1), _resting(2))
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    balance = account.get_balance_allowance

    def unavailable(**kwargs):
        if failure == 'balance':
            raise TimeoutError('offline balance unavailable')
        return SimpleNamespace(balance='100000000', allowances={
            account.environment.standard_exchange: 'unavailable'})

    account.get_balance_allowance = unavailable
    runtime.clock[0] += timedelta(seconds=61)
    public.rates = {1: '48', 2: '48'}
    state = engine.lp_auto_run_once(round_id=f'no-{failure}')
    assert account.posts == [] and account.market_orders == []
    assert all(_yield(store, f'resting-{i}')['state'] == 'known' for i in (1, 2))
    assert state['last_round']['round_id'] == f'no-{failure}'
    assert state['funds']['status'] == 'unknown'
    assert state['funds']['spendable_usd'] is None
    component = state['account_observation']['components']['balance']
    assert component['state'] == 'failed' and component['error'] and component['retry_at']
    account.get_balance_allowance = balance
    runtime.clock[0] += timedelta(seconds=1)
    waiting = (state.get('account_observation') or {}).get('components', {}).get('balance', {}).get('retry_at')
    if waiting:
        runtime.clock[0] = max(runtime.clock[0], datetime.fromisoformat(waiting.replace('Z', '+00:00')))
    adapter.lp_account_snapshot_shared(max_age_seconds=-1,
        trade_generation_provider=store.lp_trade_generation)
    # Renew genuine catalog/history/metadata inputs after expiring finances.
    # The older prepare seed has no public catalog preparation lifecycle.
    prepared = _prepare_public_inputs(lp, adapter)
    maintained = lp.refresh_candidate_recommendations()
    state = engine.lp_auto_run_once(round_id=f'{failure}-recovered')
    (tmp_path / 'R06-recovery.json').write_text(json.dumps(
        dict(prepared=prepared, maintained=maintained, state=state), default=str, indent=2))
    assert state['last_round']['round_id'] == f'{failure}-recovered'
    assert len(account.posts) == 1 and account.posts[0].token_id == _refill_identity(3)[2]
    assert state['slots']['occupied'] == 3
    assert Decimal(state['funds']['buy_reserved_usd']) == 24


@pytest.mark.parametrize('position_result', ['known_inventory_no_history', 'missing_positions'])
def test_missing_inventory_evidence_is_not_zero_exposure(runtime, position_result):
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='40', target=5,
        count=5, public=public)
    account.orders = (_resting(1), _resting(2))
    account.positions = (_position('40').model_copy(update={
        'token_id': _refill_identity(6)[2], 'condition_id': _refill_identity(6)[1]}),)
    history_calls = []
    history = account.list_account_trades

    def counted_history(**kwargs):
        history_calls.append(kwargs)
        return history(**kwargs)

    account.list_account_trades = counted_history
    if position_result == 'missing_positions':
        # Establish current order ownership honestly before the source fails.
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        history_calls.clear()

        def missing_positions(**kwargs):
            raise TimeoutError('offline current positions unavailable')

        account.list_positions = missing_positions
    for index in range(2):
        runtime.clock[0] += timedelta(seconds=1)
        state = engine.lp_auto_run_once(round_id=f'{position_result}-{index}')
        assert history_calls == [], 'Ordinary planning must not read full trade history'
        assert state['last_round']['round_id'] == f'{position_result}-{index}'
        assert all(_yield(store, f'resting-{i}')['state'] == 'known' for i in (1, 2))
        assert account.market_orders == [] and account.cancels == []
        if position_result == 'missing_positions':
            assert account.posts == [], 'Missing inventory evidence is not zero inventory'
            assert state['funds']['status'] == 'unknown'
            assert state['funds']['spendable_usd'] is None
        else:
            assert len(account.posts) == 1
            assert account.posts[0].maker_amount == 8000000
            assert account.posts[0].taker_amount == 20000000
            assert Decimal(state['funds']['inventory_cost_usd']) == 16
            assert Decimal(state['funds']['buy_reserved_usd']) == 24
            assert state['slots']['occupied'] == 3
            assert all(s.get('fee_status') != 'known' for s in store.lp_sessions()
                if s['token_id'] == _refill_identity(6)[2]), 'Unread history must not become complete fees'


def test_incomplete_order_list_cannot_create_phantom_capacity(runtime):
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='24', target=3,
        count=3, public=public)
    account.orders = (_resting(1), _resting(2))
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'

    class IncompletePages:
        def iter_items(self):
            yield account.orders[1]
            raise TimeoutError('offline second orders page unavailable; A omitted')

    account.list_open_orders = lambda **kwargs: IncompletePages()
    for index in range(2):
        runtime.clock[0] += timedelta(seconds=1)
        state = engine.lp_auto_run_once(round_id=f'incomplete-orders-{index}')
        assert account.posts == [] and account.market_orders == [] and account.cancels == []
        assert state['slots']['occupied'] == 2
        assert Decimal(state['funds']['buy_reserved_usd']) == 16
        assert state['funds']['spendable_usd'] is None
        assert state['last_round']['round_id'] == f'incomplete-orders-{index}'
        assert _yield(store, 'resting-1')['state'] == 'known'
        assert state.get('account_observation', {}).get('components', {}).get('open_orders', {}).get('state') == 'failed'
        assert state['account_observation']['components']['positions']['state'] == 'known'


@pytest.mark.parametrize('invalid', ['foreign_account', 'original_quantity'])
def test_invalid_identity_cannot_poison_unrelated_current_facts(runtime, invalid):
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='24', target=3,
        count=3, public=public)
    originals = (_resting(1), _resting(2))
    account.orders = originals
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    owner, = [s for s in store.lp_sessions() if 'resting-1' in s.get('owned_order_ids', ())]
    saved = owner['order_history']['resting-1']
    account.orders = (originals[0].model_copy(update=(
        {'maker_address': '0x' + '9' * 40} if invalid == 'foreign_account' else
        {'original_size': Decimal('1')})), originals[1])
    public.rates[2] = '48'
    runtime.clock[0] += timedelta(seconds=1)
    state = engine.lp_auto_run_once(round_id=f'invalid-{invalid}')
    assert account.posts == [] and account.market_orders == []
    assert store.lp_session(owner['session_id'])['order_history']['resting-1'] == saved
    assert _yield(store, 'resting-2')['state'] == 'known'
    assert datetime.fromisoformat(_yield(store, 'resting-2')['checked_at'].replace('Z', '+00:00')) == runtime.clock[0]
    assert state['last_round']['round_id'] == f'invalid-{invalid}'
    assert state['funds']['spendable_usd'] is None


@pytest.mark.parametrize('published', ['still_valid', 'expired', 'changed_by_trade'])
def test_read_failure_reuses_only_still_valid_published_facts(runtime, published):
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='24', target=3,
        count=3, public=public)
    account.orders = (_resting(1), _resting(2))
    engine.lp_auto_set_desired_running(False)
    baseline = engine.lp_auto_run_once(round_id='published-before-failure')
    assert account.posts == []
    original_time = baseline['funds']['as_of']
    if published == 'changed_by_trade':
        owner, = [s for s in store.lp_sessions() if 'resting-1' in s.get('owned_order_ids', ())]
        lp.stop(owner['session_id'])
    engine.lp_auto_set_desired_running(True)

    def failed_balance(**kwargs):
        raise TimeoutError('offline failed current balance')

    account.get_balance_allowance = failed_balance
    runtime.clock[0] += timedelta(seconds=61 if published == 'expired' else 1)
    state = engine.lp_auto_run_once(round_id=f'reuse-{published}')
    assert len(account.posts) == (1 if published == 'still_valid' else 0)
    assert state['funds']['as_of'] == original_time, 'A failed read cannot renew original source time'
    assert state['account_observation']['components']['balance']['state'] == 'failed'
    assert _yield(store, 'resting-2')['state'] == 'known'
    assert state['last_round']['round_id'] == f'reuse-{published}'
    assert account.market_orders == []
    if published == 'still_valid':
        assert account.posts[0].token_id == _refill_identity(3)[2]
        assert state['slots']['occupied'] == 3
        assert Decimal(state['funds']['buy_reserved_usd']) == 24


def test_all_reads_failed_can_recover_without_replaying_plan(runtime):
    public = ColdPublic(runtime.clock)
    public.rates[1] = '120'
    book = public.get_order_book

    def yes_liquidity(**kwargs):
        result = book(**kwargs)
        return result.model_copy(update={'bids': ()}) if kwargs['token_id'] in {
            f'0x{i + 300:064x}' for i in range(1, 7)} else result

    public.get_order_book = yes_liquidity
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='8', target=1,
        count=1, public=public)
    original = {key: getattr(account, key) for key in
        ('get_balance_allowance', 'list_open_orders', 'list_positions')}

    def unavailable(**kwargs):
        raise TimeoutError('offline all account dependencies unavailable')

    for key in original:
        setattr(account, key, unavailable)
    for index in range(2):
        runtime.clock[0] += timedelta(seconds=1)
        state = engine.lp_auto_run_once(round_id=f'all-failed-{index}')
        assert state['last_round']['round_id'] == f'all-failed-{index}'
        assert account.posts == [] and account.market_orders == [] and store.lp_sessions() == []
        assert all(c['state'] == 'failed' for c in state['account_observation']['components'].values())
    for key, reader in original.items():
        setattr(account, key, reader)
    runtime.clock[0] = max(datetime.fromisoformat(c['retry_at'].replace('Z', '+00:00'))
        for c in state['account_observation']['components'].values())
    adapter.lp_account_snapshot_shared(max_age_seconds=-1,
        trade_generation_provider=store.lp_trade_generation)
    _prepare_public_inputs(lp, adapter)
    state = engine.lp_auto_run_once(round_id='all-failed-recovered')
    assert len(account.posts) == 1 and account.posts[0].token_id == _refill_identity(1)[2]
    sid = state['last_round']['actions'][0]['session_id']
    audit = store.lp_actions(sid)
    runtime.clock[0] += timedelta(seconds=1)
    second = engine.lp_auto_run_once(round_id='all-failed-after-recovery')
    assert second['last_round']['round_id'] == 'all-failed-after-recovery'
    assert len(account.posts) == 1 and store.lp_actions(sid) == audit
    assert second['slots']['occupied'] == 1 and Decimal(second['funds']['buy_reserved_usd']) == 8


def _round_thread(engine, round_id):
    future = Future()

    def run():
        try:
            future.set_result(engine.lp_auto_run_once(round_id=round_id))
        except BaseException as exc:
            future.set_exception(exc)

    thread = Thread(target=run, daemon=True)
    thread.start()
    return future, thread


def _observe_native_connections(store, monkeypatch, connection_type):
    native = sqlite3.connect

    def connect(database, *args, **kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs['factory'] = connection_type
        return native(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    return native


def test_planning_consumes_only_committed_partial_observation(request, runtime, monkeypatch):
    if run_test_in_subprocess(request):
        return
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='24', target=3,
        count=3, public=public)
    account.orders = (_resting(1), _resting(2))
    reward = public.list_market_rewards

    def partial(**kwargs):
        if kwargs['condition_id'] == _refill_identity(1)[1]:
            raise TimeoutError('offline A yield unknown, current account remains usable')
        return reward(**kwargs)

    public.list_market_rewards = partial
    ready, release = Event(), Event()
    claimed = []

    class BeforeCommit(sqlite3.Connection):
        armed = False

        def execute(self, sql, parameters=()):
            if sql.startswith('INSERT INTO lp_auto_pool') and parameters and not claimed:
                payload = json.loads(parameters[0])
                if payload.get('account_observation'):
                    claimed.append(True)
                    self.armed = True
            if sql == 'COMMIT' and self.armed:
                ready.set()
                assert release.wait(5), 'Independent native COMMIT watchdog'
                self.armed = False
            return super().execute(sql, parameters)

    native = _observe_native_connections(store, monkeypatch, BeforeCommit)
    future, thread = _round_thread(engine, 'partial-commit')
    try:
        assert ready.wait(5), 'Round must reach its actual COMMIT'
        with native(store.path) as independent:
            document = json.loads(independent.execute('SELECT payload FROM lp_auto_pool').fetchone()[0])
        assert not document.get('account_observation')
        assert account.posts == [] and not future.done()
        release.set()
        state = future.result(timeout=5)
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        lp.drain_submissions()
    assert _yield(store, 'resting-1')['state'] == 'unknown'
    assert _yield(store, 'resting-2')['state'] == 'known'
    assert len(account.posts) == 1 and account.posts[0].token_id == _refill_identity(3)[2]
    assert state['slots']['occupied'] == 3 and Decimal(state['funds']['buy_reserved_usd']) == 24


def test_one_failed_publication_does_not_block_independent_committed_facts(runtime):
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='24', target=3,
        count=3, public=public)
    account.orders = (_resting(1), _resting(2))
    with sqlite3.connect(store.path) as connection:
        connection.execute("""CREATE TRIGGER fail_only_a_yield BEFORE UPDATE ON lp_sessions
            WHEN json_extract(NEW.payload, '$.buy_yield_observations."resting-1"') IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'offline only A yield publication failed'); END""")
    state = engine.lp_auto_run_once(round_id='independent-publication')
    assert _yield(store, 'resting-1') is None
    assert _yield(store, 'resting-2')['state'] == 'known'
    assert len(account.posts) == 1 and account.posts[0].token_id == _refill_identity(3)[2]
    retained, = [r for r in state['last_round']['targets'] if r.get('order_id') == 'resting-1']
    assert retained['minimum_order_estimate']['state'] == 'unknown', 'Uncommitted estimates cannot authorize planning'
    assert state['slots']['occupied'] == 3 and Decimal(state['funds']['buy_reserved_usd']) == 24
    assert account.cancels == [] and account.market_orders == []


@pytest.mark.parametrize('mode', ['natural_empty', 'manual_pause'])
def test_empty_and_paused_rounds_continue_fact_publication(runtime, mode):
    public = SerialPublic(runtime.clock)
    account_orders = (_resting(1), _resting(2))
    store, adapter, account, lp, engine = runtime(public_client=public, orders=account_orders)
    engine.lp_auto_configure({'budget_usd': '16', 'target_buy_count': 2})
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_set_desired_running(mode == 'natural_empty')
    for index, expected in enumerate(('0.083893', '0.167785')):
        public.rates = {1: '24' if index == 0 else '48', 2: '24'}
        runtime.clock[0] += timedelta(seconds=1)
        state = engine.lp_auto_run_once(round_id=f'{mode}-{index}')
        observation = _yield(store, 'resting-1')
        assert Decimal(observation['yield_pct_per_hour']).quantize(Decimal('.000001')) == Decimal(expected)
        assert datetime.fromisoformat(observation['checked_at'].replace('Z', '+00:00')) == runtime.clock[0]
        assert state['last_round']['round_id'] == f'{mode}-{index}'
        assert state['last_round']['reason'] == ('target_filled' if mode == 'natural_empty' else 'manually_paused')
        assert state['desired_running'] == (mode == 'natural_empty')
        assert state['plan_wait'] is None, 'Empty/paused rounds do not begin a normal fixed interval'
        assert account.posts == [] and account.cancels == [] and account.market_orders == []


def test_native_rollback_never_exposes_half_a_fact_batch(request, runtime):
    if run_test_in_subprocess(request):
        return
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='24', target=3,
        count=3, public=public)
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    with sqlite3.connect(store.path) as connection:
        previous = json.loads(connection.execute('SELECT payload FROM lp_auto_pool').fetchone()[0])['account_financial_facts']
        connection.execute(f"""CREATE TRIGGER abort_second_registered_order BEFORE INSERT ON lp_sessions
            WHEN json_extract(NEW.payload, '$.token_id') = '{_refill_identity(2)[2]}'
            BEGIN SELECT RAISE(ABORT, 'offline native second registration failure'); END""")
    account.orders = (_resting(1), _resting(2))
    state = engine.lp_auto_run_once(round_id='rollback-account-registration')
    with sqlite3.connect(store.path) as independent:
        assert independent.execute('SELECT count(*) FROM lp_sessions').fetchone()[0] == 0
        document = json.loads(independent.execute('SELECT payload FROM lp_auto_pool').fetchone()[0])
    assert document['account_financial_facts'] == previous
    assert not document.get('account_planning_facts')
    assert account.posts == [] and account.market_orders == []
    assert state['last_round']['round_id'] == 'rollback-account-registration'
    with sqlite3.connect(store.path) as connection:
        connection.execute('DROP TRIGGER abort_second_registered_order')
    runtime.clock[0] += timedelta(seconds=1)
    recovered = engine.lp_auto_run_once(round_id='rollback-next-current-round')
    assert len(account.posts) == 1 and account.posts[0].token_id == _refill_identity(3)[2]
    assert recovered['slots']['occupied'] == 3
    assert Decimal(recovered['funds']['buy_reserved_usd']) == 24


@pytest.mark.parametrize('interruption', ['before_commit', 'after_commit', 'accepted_projection_failure'])
def test_uncertain_commit_is_verified_without_duplicate_trading(request, runtime, monkeypatch, interruption):
    if run_test_in_subprocess(request):
        return
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='24', target=3,
        count=3, public=public)
    account.orders = (_resting(1), _resting(2))
    if interruption == 'accepted_projection_failure':
        with sqlite3.connect(store.path) as connection:
            connection.execute(f"""CREATE TRIGGER fail_accepted_projection BEFORE UPDATE ON lp_sessions
                WHEN json_extract(NEW.payload, '$.token_id') = '{_refill_identity(3)[2]}'
                AND json_extract(NEW.payload, '$.submit_status') = 'accepted'
                BEGIN SELECT RAISE(ABORT, 'offline derived accepted projection failed'); END""")
        state = engine.lp_auto_run_once(round_id='accepted-projection-failure')
        assert len(account.posts) == 1
        session, = [s for s in store.lp_sessions() if s['token_id'] == _refill_identity(3)[2]]
        original, = store.lp_actions(session['session_id'])
        assert original['state'] == 'accepted' and original['order_id'] == 'refill-3'
        assert original['post_started'] is True
        assert state['last_round']['actions'][0].get('reason'), 'The derived publication fault remains visible'
        with sqlite3.connect(store.path) as connection:
            connection.execute('DROP TRIGGER fail_accepted_projection')
        runtime.clock[0] += timedelta(seconds=1)
        engine.lp_auto_run_once(round_id='accepted-projection-recovery')
        assert len(account.posts) == 1
        assert store.lp_actions(session['session_id']) == [original]
        return
    faulted = []

    class InterruptedCommit(sqlite3.Connection):
        armed = False

        def execute(self, sql, parameters=()):
            if sql.startswith('UPDATE lp_auto_pool SET payload=json_set') and '$.account_planning_facts' in sql and not faulted:
                self.armed = True
            if sql == 'COMMIT' and self.armed and not faulted:
                faulted.append(interruption)
                if interruption == 'after_commit':
                    super().execute(sql, parameters)
                # A real SQL exception occurs on the selected side of COMMIT.
                return super().execute('SELECT * FROM offline_missing_commit_return')
            return super().execute(sql, parameters)

    native = _observe_native_connections(store, monkeypatch, InterruptedCommit)
    state = engine.lp_auto_run_once(round_id='uncertain-publication')
    assert faulted == [interruption]
    assert account.posts == [] and account.market_orders == []
    with native(store.path) as independent:
        count = independent.execute('SELECT count(*) FROM lp_sessions').fetchone()[0]
        document = json.loads(independent.execute('SELECT payload FROM lp_auto_pool').fetchone()[0])
    assert count == (2 if interruption == 'after_commit' else 0)
    assert bool(document.get('account_planning_facts')) == (interruption == 'after_commit')
    assert state['last_round']['round_id'] == 'uncertain-publication'
    runtime.clock[0] += timedelta(seconds=1)
    recovered = engine.lp_auto_run_once(round_id='uncertain-publication-recovered')
    assert len(account.posts) == 1 and account.posts[0].token_id == _refill_identity(3)[2]
    action, = recovered['last_round']['actions']
    audit = store.lp_actions(action['session_id'])
    runtime.clock[0] += timedelta(seconds=1)
    engine.lp_auto_run_once(round_id='uncertain-publication-no-replay')
    assert len(account.posts) == 1
    assert store.lp_actions(action['session_id']) == audit
    assert len([s for s in store.lp_sessions() if s['token_id'] == _refill_identity(3)[2]]) == 1


def test_optional_diagnostic_failure_does_not_gate_a_registered_trade(runtime):
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='24', target=3,
        count=3, public=public)
    account.orders = (_resting(1), _resting(2))
    failed = Event()
    errors = []

    class FailedSink(logging.Handler):
        def emit(self, record):
            if str(record.msg).startswith('lp_rotation_guard'):
                errors.append('offline optional diagnostic sink unavailable')
                failed.set()
                raise OSError(errors[-1])

    target = logging.getLogger('open_trader.polymarket_lp_auto')
    sink, old_level = FailedSink(), target.level
    target.addHandler(sink)
    target.setLevel(logging.INFO)
    try:
        state = engine.lp_auto_run_once(round_id='optional-diagnostic-failure')
        assert failed.wait(5), 'Observe actual failure at the optional logging sink'
    finally:
        target.removeHandler(sink)
        target.setLevel(old_level)
        sink.close()
    assert errors
    assert len(account.posts) == 1 and account.posts[0].token_id == _refill_identity(3)[2]
    action, = state['last_round']['actions']
    audit, = store.lp_actions(action['session_id'])
    assert audit['state'] == 'accepted' and audit['post_started'] is True
    assert audit['order_id'] == 'refill-3'
    runtime.clock[0] += timedelta(seconds=1)
    engine.lp_auto_run_once(round_id='diagnostic-failure-no-replay')
    assert len(account.posts) == 1
    assert store.lp_actions(action['session_id']) == [audit]


def test_store_outage_defers_unregistrable_requests_but_recovers(request, runtime, monkeypatch):
    if run_test_in_subprocess(request):
        return
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='8', target=1,
        count=1, public=public)
    native = sqlite3.connect
    writes_blocked = []

    class ReadOnly(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError as exc:
                if 'readonly' in str(exc):
                    writes_blocked.append(sql)
                raise

    def read_only(database, *args, **kwargs):
        if Path(database).resolve() == store.path.resolve():
            kwargs.update(uri=True, factory=ReadOnly)
            database = 'file:' + str(store.path) + '?mode=ro'
        return native(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', read_only)
    before_reads = account.position_reads
    state = engine.lp_auto_run_once(round_id='store-outage')
    assert writes_blocked, 'The real SQLite connection must reject authoritative writes'
    assert account.position_reads > before_reads
    assert account.posts == [] and store.lp_sessions() == []
    assert state['funds']['spendable_usd'] is None
    monkeypatch.setattr(sqlite3, 'connect', native)
    runtime.clock[0] += timedelta(seconds=1)
    state = engine.lp_auto_run_once(round_id='store-recovered')
    assert len(account.posts) == 1 and account.posts[0].token_id == _refill_identity(1)[2]
    sid = state['last_round']['actions'][0]['session_id']
    audit = store.lp_actions(sid)
    runtime.clock[0] += timedelta(seconds=1)
    engine.lp_auto_run_once(round_id='store-recovered-next')
    assert len(account.posts) == 1 and store.lp_actions(sid) == audit


def test_old_observation_cannot_overwrite_newer_order_or_control(request, runtime):
    if run_test_in_subprocess(request):
        return
    public = SerialPublic(runtime.clock)
    orders = (_resting(1), _resting(2))
    store, adapter, account, lp, engine = runtime(public_client=public, orders=orders)
    engine.lp_auto_configure({'budget_usd': '16', 'target_buy_count': 2})
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once(round_id='before-late-market-read')
    saved = _yield(store, 'resting-1')
    ready, release, finished = Event(), Event(), Event()
    book = public.get_order_book
    claimed = []

    def delayed(**kwargs):
        result = book(**kwargs)
        if kwargs['token_id'] == orders[0].token_id and not claimed:
            claimed.append(True)
            ready.set()
            try:
                assert release.wait(5), 'Independent old market-read watchdog'
            finally:
                finished.set()
        return result

    public.get_order_book = delayed
    runtime.clock[0] += timedelta(seconds=1)
    future, thread = _round_thread(engine, 'late-market-read')
    try:
        assert ready.wait(5)
        runtime.clock[0] += timedelta(seconds=1)
        account.orders = (orders[0].model_copy(update={'size_matched': Decimal('5')}), orders[1])
        account.positions = (_position('5'),)
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        engine.lp_auto_set_desired_running(False)
        current_owner, = [s for s in store.lp_sessions() if orders[0].id in s.get('owned_order_ids', ())]
        current_order = current_owner['order_history'][orders[0].id]
        release.set()
        future.result(timeout=5)
        assert finished.wait(5)
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        lp.drain_submissions()
    assert engine.lp_auto_state()['desired_running'] is False
    assert store.lp_session(current_owner['session_id'])['order_history'][orders[0].id] == current_order
    assert Decimal(current_order['size_matched']) == 5
    assert _yield(store, orders[0].id) == saved, 'The late old quantity cannot publish over the new order'
    assert Decimal(engine.lp_auto_state()['funds']['inventory_cost_usd']) == 2
    assert account.posts == [] and account.market_orders == []


def test_mixed_failures_leave_a_nonempty_executable_subset(runtime):
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='40', target=5,
        count=6, public=public)
    account.orders = (_resting(1), _resting(2))
    reward = public.list_market_rewards

    def mixed(**kwargs):
        if kwargs['condition_id'] in {_refill_identity(i)[1] for i in (1, 3)}:
            raise TimeoutError('offline A yield / C candidate unavailable')
        return reward(**kwargs)

    public.list_market_rewards = mixed
    with sqlite3.connect(store.path) as connection:
        connection.execute("""CREATE TRIGGER fail_only_b_yield BEFORE UPDATE ON lp_sessions
            WHEN json_extract(NEW.payload, '$.buy_yield_observations."resting-2"') IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'offline only B yield writer failed'); END""")
    state = engine.lp_auto_run_once(round_id='mixed-independent-failures')
    assert _yield(store, 'resting-1')['state'] == 'unknown'
    assert _yield(store, 'resting-2') is None
    assert len(account.posts) == 3
    assert {p.token_id for p in account.posts} == {_refill_identity(i)[2] for i in (4, 5, 6)}
    assert any(b['condition_id'] == _refill_identity(3)[1] for b in state['last_round']['blocked'])
    assert all(r['minimum_order_estimate']['state'] == 'unknown'
        for r in state['last_round']['targets'] if r.get('order_id') in {'resting-1', 'resting-2'})
    assert state['slots']['occupied'] == 5 and Decimal(state['funds']['buy_reserved_usd']) == 40
    assert account.cancels == [] and account.market_orders == []


@pytest.mark.parametrize('execution_lock', ['free', 'unrelated_native_lock'])
def test_fixed_cde_finishes_once_with_mixed_sync_and_unknown_results(request, runtime, tmp_path, execution_lock):
    if run_test_in_subprocess(request):
        return
    public = RefillPublic(runtime.clock)
    public.rates = {1: '120', 2: '120', 3: '96', 4: '72', 5: '48', 6: '24'}
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='40', target=5,
        count=6, public=public)
    account.orders = (_resting(1), _resting(2))
    tokens = {_refill_identity(i)[2]: i for i in (3, 4, 5)}
    sign, post = account.create_limit_order, account.post_order
    held, signed_tokens, ready, locked, guard = [], set(), Event(), Event(), Lock()
    queries = []

    def signing(**kwargs):
        signed = sign(**kwargs)
        if execution_lock == 'unrelated_native_lock':
            with guard:
                signed_tokens.add(kwargs['token_id'])
                if len(signed_tokens) == 3:
                    ready.set()
            assert ready.wait(5), 'All native signers must finish preparation'
            if kwargs['token_id'] == _refill_identity(3)[2]:
                handle = (tmp_path / 'execution.lock').open('a+')
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    handle.close()
                    raise
                held.append(handle)
                locked.set()
            assert locked.wait(5), 'Observe the real unrelated execution flock'
        return signed

    def venue(signed):
        if tokens[signed.token_id] == 3:
            return post(signed)
        account.posts.append(signed)
        if tokens[signed.token_id] == 4:
            return {'accepted': False, 'status': 'REJECTED', 'code': 'venue_denied'}
        raise TimeoutError('offline E POST response is genuinely unclear')

    def query(**kwargs):
        queries.append(kwargs)
        raise AssertionError('Clear synchronous outcomes need no universal second query')

    account.create_limit_order, account.post_order, account.get_order = signing, venue, query
    try:
        state = engine.lp_auto_run_once(round_id=f'fixed-cde-{execution_lock}')
    finally:
        ready.set()
        locked.set()
        for handle in held:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        lp.drain_submissions()
    assert len(account.posts) == 3
    assert {p.token_id for p in account.posts} == set(tokens)
    assert queries == []
    actions = {a['token_id']: a for a in state['last_round']['actions']}
    assert {token: a['state'] for token, a in actions.items()} == {
        _refill_identity(3)[2]: 'success', _refill_identity(4)[2]: 'rejected', _refill_identity(5)[2]: 'unknown'}
    audits = {token: store.lp_actions(a['session_id']) for token, a in actions.items()}
    assert {token: rows[0]['state'] for token, rows in audits.items()} == {
        _refill_identity(3)[2]: 'accepted', _refill_identity(4)[2]: 'rejected', _refill_identity(5)[2]: 'unknown'}
    assert state['active_plan'] is None and state['last_round']['completed_at']
    assert state['slots']['occupied'] == 4
    assert Decimal(state['funds']['buy_reserved_usd']) + Decimal(state['funds']['isolated_reserved_usd']) == 32
    # A regular current observation may cover an ended UNKNOWN hold; absence
    # never rewrites that original request as rejected or authorizes its replay.
    public.rates.update({4: '0', 5: '0', 6: '0'})
    runtime.clock[0] += timedelta(seconds=1)
    engine.lp_auto_run_once(round_id='fixed-cde-current-recovery')
    assert all(store.lp_actions(actions[token]['session_id']) == rows for token, rows in audits.items())
    assert len(account.posts) == 3


@pytest.mark.parametrize('dependency', ['native_timeout', 'retry_after'])
def test_slow_or_limited_dependency_does_not_own_the_cycle(request, runtime, dependency):
    if run_test_in_subprocess(request):
        return
    public = RefillPublic(runtime.clock)
    store, adapter, account, lp, engine, _ = prepare(runtime, budget='24', target=3,
        count=3, public=public)
    account.orders = (_resting(1), _resting(2))
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    balance = account.get_balance_allowance
    ready, release, finished = Event(), Event(), Event()
    calls = []

    def slow(**kwargs):
        calls.append(kwargs)
        ready.set()
        if dependency == 'retry_after':
            raise HTTPError('https://offline.invalid/account', 429, 'limited', {'Retry-After': '120'}, None)
        try:
            assert release.wait(5), 'Independent SDK worker watchdog'
            return balance(**kwargs)
        finally:
            finished.set()

    account.get_balance_allowance = slow
    future, thread = _round_thread(engine, f'bounded-{dependency}')
    try:
        assert ready.wait(5)
        first = future.result(timeout=5)
        initial = _yield(store, 'resting-2')
        assert initial['state'] == 'known'
        assert len(calls) == 1 and account.posts == []
        runtime.clock[0] += timedelta(seconds=1)
        public.rates[2] = '48'
        second = engine.lp_auto_run_once(round_id=f'bounded-next-{dependency}')
        assert second['last_round']['round_id'] == f'bounded-next-{dependency}'
        assert _yield(store, 'resting-2')['checked_at'] != initial['checked_at']
        assert len(calls) == 1 and account.posts == []
        if dependency == 'native_timeout':
            assert not finished.is_set(), 'The original underlying SDK worker still owns capacity'
            assert first['account_observation']['components']['positions']['state'] == 'known'
            with pytest.raises(RuntimeError, match='account reader'):
                lp.drain_submissions(timeout=.05)
        else:
            assert 'account_read_cooling_down' in second['block_reasons']
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        if dependency == 'native_timeout':
            assert finished.wait(5)
        lp.drain_submissions()


@pytest.mark.parametrize('scenario', ['local_failure', 'no_work', 'serial_scheduler', 'retired_config'])
def test_repeated_local_failure_paces_only_its_dependency(request, runtime, scenario):
    if run_test_in_subprocess(request):
        return
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    public = SerialPublic(runtime.clock)
    orders = () if scenario == 'no_work' else (_resting(1), _resting(2))
    store, adapter, account, lp, engine = runtime(public_client=public, orders=orders)
    engine.lp_auto_configure({'budget_usd': '16', 'target_buy_count': 2})
    engine.lp_auto_set_desired_running(True)
    if scenario == 'retired_config':
        before = engine.lp_auto_state()
        with pytest.raises(ValueError, match='round_interval_seconds_retired_in_continuous_mode'):
            engine.lp_auto_configure({'budget_usd': '40', 'target_buy_count': 5,
                'round_interval_seconds': 7, 'api_retry_interval_seconds': 3})
        after = engine.lp_auto_state()
        assert {k: after[k] for k in ('budget_usd', 'target_buy_count', 'config_version', 'api_retry_interval_seconds')} == {
            k: before[k] for k in ('budget_usd', 'target_buy_count', 'config_version', 'api_retry_interval_seconds')}
        assert after['cycle_mode'] == 'continuous' and after['effective_round_interval_seconds'] is None
        return
    if scenario == 'local_failure':
        initial = engine.lp_auto_run_once(round_id='local-success')
        last_success = initial['account_observation']['components']['balance']['last_success_at']
        calls = []
        def fail(**kwargs):
            calls.append(kwargs)
            raise TimeoutError('offline balance transport')
        account.get_balance_allowance = fail
        deadline, previous = None, None
        for index, rate in enumerate(('24', '48', '72')):
            runtime.clock[0] += timedelta(seconds=1)
            public.rates = {1: rate, 2: rate}
            state = engine.lp_auto_run_once(round_id=f'local-failure-{index}')
            component = state['account_observation']['components']['balance']
            assert component['state'] == 'failed' and component['error']
            assert component['last_success_at'] == last_success
            assert deadline is None or component['retry_at'] == deadline
            deadline = component['retry_at']
            current = _yield(store, 'resting-2')
            assert current['state'] == 'known' and current['checked_at'] != previous
            previous = current['checked_at']
            assert len(calls) == 1 and state['plan_wait'] is None
            assert state['last_round']['round_id'] == f'local-failure-{index}'
            assert account.posts == [] and state['slots']['occupied'] == 2
        return
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    if scenario == 'no_work':
        calls = []
        def fail(*args, **kwargs):
            calls.append(1)
            raise TimeoutError('offline all current sources')
        account.get_balance_allowance = account.list_open_orders = account.list_positions = fail
        try:
            assert scheduler.run_due()
            state = engine.lp_auto_state()
            deadlines = [c['retry_at'] for c in state['account_observation']['components'].values()]
            assert state['plan_wait']['kind'] == 'dependencies'
            assert state['plan_wait']['deadline'] == min(deadlines)
            assert scheduler.snapshot()['next_check_at'] == min(deadlines)
            assert not scheduler.run_due() and len(calls) == 3
            scheduler.request_check()
            assert scheduler.run_due(), 'An existing wake event may re-evaluate available work'
            assert len(calls) == 3, 'Wake does not bypass actual dependency retry deadlines'
            assert not scheduler.run_due() and account.posts == []
        finally:
            scheduler.stop()
        return
    ready, release = Event(), Event()
    balance = account.get_balance_allowance
    calls = []
    def blocking(**kwargs):
        calls.append(1)
        ready.set()
        assert release.wait(5), 'Independent native scheduling watchdog'
        return balance(**kwargs)
    account.get_balance_allowance = blocking
    result = Future()
    def run():
        try:
            result.set_result(scheduler.run_due())
        except BaseException as exc:
            result.set_exception(exc)
    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        assert ready.wait(5)
        assert not scheduler.run_due(), 'A second cycle cannot overlap the current native read'
        release.set()
        assert result.result(timeout=5)
        first_round = engine.lp_auto_state()['last_round']['round_id']
        assert scheduler.run_due(), 'A healthy cycle has no normal interval to wait'
        assert engine.lp_auto_state()['last_round']['round_id'] != first_round
        assert len(calls) == 2 and account.posts == []
        assert scheduler.snapshot()['check_interval_seconds'] is None
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        scheduler.stop()
        lp.drain_submissions()


@pytest.mark.parametrize('scenario', ['accepted', 'unknown', 'unsent', 'legacy_interval', 'limited_restart', 'local_retry_restart'])
def test_cold_restart_uses_current_facts_without_old_send_authority(request, runtime, tmp_path, scenario):
    if run_test_in_subprocess(request):
        return
    from copy import deepcopy
    import io
    import subprocess
    import sys
    import tarfile
    if scenario in ('limited_restart', 'local_retry_restart'):
        public = SerialPublic(runtime.clock)
        orders = (_resting(1), _resting(2))
        store, adapter, account, lp, engine = runtime(public_client=public, orders=orders)
        engine.lp_auto_configure({'budget_usd': '16', 'target_buy_count': 2})
        engine.lp_auto_set_desired_running(True)
        assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
        def limited(**kwargs):
            if scenario == 'local_retry_restart':
                raise TimeoutError('offline local balance source')
            raise HTTPError('https://offline.invalid/account', 429, 'limited', {'Retry-After': '120'}, None)
        account.get_balance_allowance = limited
        first = engine.lp_auto_run_once(round_id='shared-limit-before-restart')
        if scenario == 'limited_restart':
            assert 'account_read_cooling_down' in first['block_reasons']
        else:
            deadline = first['account_observation']['components']['balance']['retry_at']
        previous = _yield(store, 'resting-2')
        lp.drain_submissions()
        adapter.close()
        store, adapter, account, lp, engine = runtime(public_client=public, orders=orders)
        balance, calls = account.get_balance_allowance, []
        def observed(**kwargs):
            calls.append(kwargs)
            return balance(**kwargs)
        account.get_balance_allowance = observed
        runtime.clock[0] += timedelta(seconds=1)
        public.rates = {1: '48', 2: '48'}
        second = engine.lp_auto_run_once(round_id='shared-limit-cold-current')
        assert calls == [], 'Cold restart preserves the genuine saved dependency deadline'
        if scenario == 'limited_restart':
            assert 'account_read_cooling_down' in second['block_reasons']
        else:
            assert second['account_observation']['components']['balance']['retry_at'] == deadline
            assert second['account_observation']['components']['positions']['state'] == 'known'
        assert _yield(store, 'resting-2')['checked_at'] != previous['checked_at']
        assert second['last_round']['round_id'] == 'shared-limit-cold-current'
        assert account.posts == [] and second['slots']['occupied'] == 2
        return
    if scenario == 'legacy_interval':
        # Authenticate immutable old-version inputs without runtime Git history.
        import hashlib
        fixture = Path(__file__).parent / 'fixtures' / 'lp_legacy_de24'
        manifest = json.loads((fixture / 'manifest.json').read_text())
        sha = 'de24fd97c611e98658f9f215c760c1b2e593da15'
        assert manifest['commit'] == sha
        archive = (fixture / manifest['archive']).read_bytes()
        assert hashlib.sha256(archive).hexdigest() == manifest['archive_sha256']
        source = tmp_path / 'baseline-source'
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as bundle:
            actual = {m.name: hashlib.sha256(bundle.extractfile(m).read()).hexdigest()
                for m in bundle.getmembers() if m.isfile()}
            assert actual == manifest['members_sha256']
            assert all(m.isdir() or m.isfile() for m in bundle.getmembers())
            bundle.extractall(source, filter='data')
        producer = r'''
import json,sys,hashlib
from pathlib import Path
import pytest
from tests.test_lp_account_reservation_reconciliation import runtime
from tests.test_lp_auto_refill_contract import RefillPublic
import open_trader.polymarket_lp_auto as auto
patch=pytest.MonkeyPatch()
generator=runtime.__wrapped__(Path(sys.argv[1]),patch)
build=next(generator)
lp=None
try:
    store,adapter,account,lp,engine=build(public_client=RefillPublic(build.clock))
    engine.lp_auto_configure({'budget_usd':'16','target_buy_count':2,'round_interval_seconds':900})
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_set_desired_running(False)
    state=engine.lp_auto_run_once(round_id='real-legacy-normal-wait')
    assert state['plan_wait']['kind']=='round' and state['round_interval_seconds']==900
    assert account.posts==[]
    Path(sys.argv[1],'baseline-producer.json').write_text(json.dumps({'sha':sys.argv[2],
        'module':auto.__file__,'module_sha256':hashlib.sha256(Path(auto.__file__).read_bytes()).hexdigest(),
        'origins':{name:{'path':sys.modules[name].__file__,
            'sha256':hashlib.sha256(Path(sys.modules[name].__file__).read_bytes()).hexdigest()}
            for name in ('open_trader.polymarket_lp_auto','open_trader.polymarket_trading',
                'open_trader.polymarket_lp','open_trader.prediction_arbitrage_store',
                'tests.test_lp_account_reservation_reconciliation','tests.test_lp_auto_refill_contract',
                'tests.test_lp_order_registration_contract')},'state':state},default=str))
finally:
    if lp is not None: lp.drain_submissions()
    try: next(generator)
    except StopIteration: pass
    patch.undo()
'''
        import os
        result = subprocess.run([sys.executable, '-B', '-c', producer, str(tmp_path), sha],
            cwd=source, env={**os.environ, 'PYTHONPATH': os.pathsep.join((str(source), str(source / 'src')))},
            text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        evidence = json.loads((tmp_path / 'baseline-producer.json').read_text())
        assert Path(evidence['module']).is_relative_to(source)
        assert evidence['module_sha256'] == manifest['members_sha256']['src/open_trader/polymarket_lp_auto.py']
        for origin in evidence['origins'].values():
            relative = Path(origin['path']).relative_to(source).as_posix()
            assert origin['sha256'] == manifest['members_sha256'][relative]

        legacy_wait = evidence['state']['plan_wait']
        assert datetime.fromisoformat(legacy_wait['deadline']) > runtime.clock[0]
        original_audits, original_posts = {}, []
        venue_orders = (_resting(1), _resting(2))
    else:
        public = ColdPublic(runtime.clock)
        store, adapter, account, lp, engine, _ = prepare(runtime, budget='24', target=3,
            count=3, public=public)
        account.orders = (_resting(1), _resting(2))
        if scenario == 'unknown':
            def lost(signed):
                account.posts.append(signed)
                raise TimeoutError('offline C POST response lost')
            account.post_order = lost
        if scenario == 'unsent':
            def interrupted(**kwargs):
                raise KeyboardInterrupt('offline interruption before C POST')
            account.create_limit_order = interrupted
            with pytest.raises(KeyboardInterrupt):
                engine.lp_auto_run_once(round_id='old-unsent-authority')
            assert engine.lp_auto_state()['active_plan']['round_id'] == 'old-unsent-authority'
            assert account.posts == []
        else:
            first = engine.lp_auto_run_once(round_id=f'old-{scenario}')
            assert len(account.posts) == 1
            assert first['last_round']['actions'][0]['state'] == ('success' if scenario == 'accepted' else 'unknown')
        lp.drain_submissions()
        original_posts = list(account.posts)
        original_audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id']))
            for s in store.lp_sessions() if store.lp_actions(s['session_id'])}
        venue_orders = account.orders
        adapter.close()
    public = ColdPublic(runtime.clock)
    store, adapter, account, lp, engine = runtime(public_client=public, orders=venue_orders)
    _prepare_public_inputs(lp, adapter)
    reads = []
    for name in ('get_balance_allowance', 'list_open_orders', 'list_positions'):
        native = getattr(account, name)
        def observed(*args, _name=name, _native=native, **kwargs):
            reads.append(_name)
            return _native(*args, **kwargs)
        setattr(account, name, observed)
    if scenario == 'unknown':
        def history_unavailable(**kwargs):
            raise TimeoutError('Original UNKNOWN remains unresolved; absence is not rejection')
        account.list_account_trades = history_unavailable
    state = engine.lp_auto_run_once(round_id=f'cold-current-{scenario}')
    assert state['last_round']['round_id'] == f'cold-current-{scenario}'
    assert set(reads) == {'get_balance_allowance', 'list_open_orders', 'list_positions'}
    assert state['active_plan'] is None and state['plan_wait'] is None
    assert state['slots']['occupied'] >= 2
    assert account.market_orders == []
    if scenario == 'legacy_interval':
        assert state['round_interval_seconds'] == 900
        assert state['cycle_mode'] == 'continuous' and account.posts == []
        assert state['reason'] == 'manually_paused'
    elif scenario == 'unsent':
        # New current permission may choose C, but never the old action UUID.
        assert len(account.posts) <= 1
        assert state['last_round']['round_id'] != 'old-unsent-authority'
        if account.posts:
            audit = next(store.lp_actions(s['session_id'])[0] for s in store.lp_sessions()
                if store.lp_actions(s['session_id']) and store.lp_actions(s['session_id'])[0].get('post_started'))
            assert audit['action_key'] not in {a['action_key'] for rows in original_audits.values() for a in rows}
    else:
        assert len(original_posts) == 1 and account.posts == []
        assert all(store.lp_actions(sid) == rows for sid, rows in original_audits.items())


@pytest.mark.parametrize('status, late', [(503, False), (503, True), (401, False),
    (401, True), (429, False), (429, True), (403, False)])
def test_planning_dependency_deadlines_survive_errors_and_restart(request, runtime, tmp_path, status, late):
    if run_test_in_subprocess(request):
        return
    from threading import current_thread
    public = SerialPublic(runtime.clock)
    orders = (_resting(1), _resting(2))
    store, adapter, account, lp, engine = runtime(public_client=public, orders=orders)
    engine.lp_auto_configure({'budget_usd': '16', 'target_buy_count': 2,
        'api_retry_interval_seconds': 7})
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    assert not (engine.lp_auto_state().get('account_observation') or {}).get('components')
    engine.lp_auto_set_desired_running(True)
    entered, release, ended = Event(), Event(), Event()
    calls, native_threads, raised_at = [], [], []
    def fail(**kwargs):
        calls.append(runtime.clock[0])
        native_threads.append(current_thread())
        entered.set()
        try:
            if late:
                assert release.wait(5), 'Independent original SDK error watchdog'
            raised_at.append(runtime.clock[0])
            raise HTTPError('https://offline.invalid/balance-allowance', status, 'offline dependency',
                {'Retry-After': '120'} if status == 503 else {}, None)
        finally:
            ended.set()
    account.get_balance_allowance = fail
    records = []
    future, thread = _round_thread(engine, 'review-deadline-original')
    try:
        assert entered.wait(5)
        first = future.result(timeout=5)
        records.append(first)
        if late:
            assert not ended.is_set(), 'Original native SDK call outlives bounded round return'
            runtime.clock[0] += timedelta(seconds=1)
            release.set()
            assert ended.wait(5)
            native_threads[0].join(5)
            assert not native_threads[0].is_alive()
            runtime.clock[0] += timedelta(seconds=2)
            first = engine.lp_auto_run_once(round_id='review-deadline-late-collection')
            records.append(first)
        expected = raised_at[0] + timedelta(seconds=120 if status == 503 else 7)
        component = ((first.get('account_observation') or {}).get('components') or {}).get('balance')
        wait = first.get('account_reader_wait') or {}
        actual = wait.get('retry_at') or (component or {}).get('retry_at')
        assert actual and datetime.fromisoformat(actual.replace('Z', '+00:00')) == expected, records
        if component:
            assert component['state'] == 'failed' and component['error']
            assert component['checked_at'] is None and component.get('last_success_at') is None
        assert len(calls) == 1 and account.posts == account.market_orders == account.cancels == []
        previous = _yield(store, orders[1].id)
        runtime.clock[0] += timedelta(seconds=1)
        public.rates = {1: '48', 2: '48'}
        current = engine.lp_auto_run_once(round_id='review-deadline-current')
        records.append(current)
        assert len(calls) == 1 and _yield(store, orders[1].id)['checked_at'] != previous['checked_at']
        assert account.posts == account.market_orders == account.cancels == []
        lp.drain_submissions()
        adapter.close()
        store, adapter, account, lp, engine = runtime(public_client=public, orders=orders)
        balance, cold_calls = account.get_balance_allowance, []
        def observed(**kwargs):
            cold_calls.append(runtime.clock[0])
            return balance(**kwargs)
        account.get_balance_allowance = observed
        runtime.clock[0] = expected - timedelta(seconds=1)
        cold = engine.lp_auto_run_once(round_id='review-deadline-cold-before')
        records.append(cold)
        assert cold_calls == [] and account.posts == account.market_orders == account.cancels == []
        wait = cold.get('account_reader_wait') or {}
        component = ((cold.get('account_observation') or {}).get('components') or {}).get('balance') or {}
        assert datetime.fromisoformat((wait.get('retry_at') or component['retry_at']).replace('Z', '+00:00')) == expected
        runtime.clock[0] = expected
        due = engine.lp_auto_run_once(round_id='review-deadline-cold-due')
        records.append(due)
        assert cold_calls == [expected], 'The real dependency becomes readable exactly at its saved deadline'
        assert account.posts == account.market_orders == account.cancels == []
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        for native in native_threads:
            native.join(5)
            assert not native.is_alive()
        lp.drain_submissions()
        (tmp_path / 'review-F1-public-evidence.json').write_text(json.dumps(
            dict(status=status, late=late, calls=calls, raised_at=raised_at, records=records), default=str, indent=2))


@pytest.mark.parametrize('failure', ['local_components', 'shared_retry_after'])
def test_sell_only_sessions_wait_for_account_dependencies(request, runtime, tmp_path, failure):
    if run_test_in_subprocess(request):
        return
    from time import monotonic
    from copy import deepcopy
    from polymarket.models.clob import SignedOrder
    from polymarket.models.data.portfolio import Position
    from tests.test_lp_buy_yield_unknown_guard import _managed_buy
    from tests.test_lp_order_registration_contract import WALLET, _maker_order, _trade
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    store, account, lp, engine, public, original = _managed_buy(runtime, 'auto')
    engine.lp_auto_configure({'api_retry_interval_seconds': 7})
    maker = _maker_order(original.id, 'BUY', '20', '.40').model_copy(update={'token_id': original.token_id})
    fill = _trade('review-real-buy-fill', maker, size='20').model_copy(update={
        'market': _refill_identity(6)[1], 'condition_id': _refill_identity(6)[1], 'token_id': original.token_id,
        'matched_at': runtime.clock[0], 'updated_at': runtime.clock[0]})
    account.trades = (fill,)
    account.positions = (Position(conditionId=_refill_identity(6)[1], proxyWallet=WALLET,
        asset=original.token_id, size=Decimal('20'), avgPrice=Decimal('.40'), outcome='YES'),)
    account.orders = ()
    terminal = original.model_copy(update={'status': 'FILLED', 'size_matched': Decimal('20'),
        'associate_trades': ('review-real-buy-fill',)})
    account.get_order = lambda *, order_id: terminal if order_id == original.id else next(o for o in account.orders if o.id == order_id)
    def sign(**kwargs):
        assert kwargs['side'] == 'SELL'
        return SignedOrder(builder='0x1', expiration=kwargs['expiration'], maker=WALLET,
            maker_amount=int(kwargs['size'] * 1000000), metadata='0x3',
            order_type='GTC' if kwargs['expiration'] is None else 'GTD', salt=1, side='SELL', signature='0x4',
            signature_type=0, signer=WALLET, taker_amount=int(kwargs['price'] * kwargs['size'] * 1000000),
            timestamp=1, token_id=str(kwargs['token_id']), post_only=True)
    def post(signed):
        account.posts.append(signed)
        sell = _open_order('review-real-passive-sell', 'SELL',
            price=str(Decimal(signed.taker_amount) / signed.maker_amount), original='20',
            token_id=signed.token_id).model_copy(update={'market': _refill_identity(6)[1],
                'condition_id': _refill_identity(6)[1]})
        account.orders += (sell,)
        return {'order_id': sell.id, 'status': 'LIVE', 'accepted': True, 'size_matched': '0'}
    account.create_limit_order, account.post_order = sign, post
    scheduler = None
    records = []
    try:
        runtime.clock[0] += timedelta(seconds=1)
        registered = lp.register_account_snapshot(lp.exchange.lp_account_snapshot_shared(max_age_seconds=-1,
            trade_generation_provider=store.lp_trade_generation))
        assert registered['state'] == 'registered'
        watchdog = monotonic() + 5
        while not any(p.side == 'SELL' for p in account.posts):
            observed = engine.lp_tick()
            assert monotonic() < watchdog, observed
        lp.drain_submissions()
        runtime.clock[0] += timedelta(seconds=1)
        sid = next(s['session_id'] for s in store.lp_sessions() if s.get('entry_order_id') == original.id)
        lp.reconcile_facts(sid)
        fresh = lp.exchange.lp_account_snapshot_shared(max_age_seconds=-1,
            trade_generation_provider=store.lp_trade_generation)
        assert lp.register_account_snapshot(fresh)['state'] == 'registered'
        dashboard = engine.refresh_lp_dashboard_snapshot()
        assert dashboard['state'] == 'ready', dashboard
        active, = store.lp_active_sessions()
        sid = active['session_id']
        assert active['order_history'][original.id]['status'] == 'FILLED'
        assert active['order_history']['review-real-passive-sell']['side'] == 'SELL'
        assert active['order_history']['review-real-passive-sell']['status'] == 'LIVE'
        assert not any(o['side'] == 'BUY' and o['status'] == 'LIVE' for o in active['order_history'].values())
        assert not lp.entry_send_inflight(sid)
        audits = deepcopy(store.lp_actions(sid))
        assert all(a['state'] == 'accepted' for a in audits), audits
        initial = engine.lp_auto_state()
        assert Decimal(initial['funds']['inventory_cost_usd']) == 8
        assert account.positions[0].size == 20 and account.positions[0].avg_price == Decimal('.40')
        posts, orders = deepcopy(account.posts), tuple(account.orders)
        originals = {name: getattr(account, name) for name in
            ('get_balance_allowance', 'list_open_orders', 'list_positions')}
        calls = {name: [] for name in originals}
        for name, read in originals.items():
            def fail(*args, _name=name, **kwargs):
                calls[_name].append(runtime.clock[0])
                if failure == 'shared_retry_after' and _name == 'get_balance_allowance':
                    raise HTTPError('https://offline.invalid/balance-allowance', 429, 'shared limit', {'Retry-After': '120'}, None)
                raise TimeoutError('Actual SDK dependency unavailable: ' + _name)
            setattr(account, name, fail)
        runtime.clock[0] += timedelta(seconds=61)
        failed_at = runtime.clock[0]
        scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        records.append(state)
        expected = failed_at + timedelta(seconds=120 if failure == 'shared_retry_after' else 7)
        assert state['plan_wait'] and state['plan_wait']['kind'] == 'dependencies', state
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == expected
        assert datetime.fromisoformat(scheduler.snapshot()['next_check_at']) == expected
        prior_calls, prior_round = deepcopy(calls), deepcopy(state['last_round'])
        assert not scheduler.run_due()
        assert calls == prior_calls and engine.lp_auto_state()['last_round'] == prior_round
        scheduler.request_check()
        assert scheduler.run_due()
        assert calls == prior_calls, 'Wake cannot bypass genuine SDK cooldown'
        assert not scheduler.run_due()
        runtime.clock[0] = expected - timedelta(seconds=1)
        assert not scheduler.run_due() and calls == prior_calls
        for name, read in originals.items():
            def observed(*args, _name=name, _read=read, **kwargs):
                calls[_name].append(runtime.clock[0])
                return _read(*args, **kwargs)
            setattr(account, name, observed)
        runtime.clock[0] = expected
        assert scheduler.run_due()
        records.append(engine.lp_auto_state())
        assert calls['get_balance_allowance'][-1] == expected and len(calls['get_balance_allowance']) == 2
        assert all(calls[name][-1] == expected for name in originals)
        assert account.posts == posts and tuple(account.orders) == orders
        assert account.cancels == account.market_orders == []
        assert store.lp_actions(sid) == audits
        assert account.positions[0].size == 20
    finally:
        if scheduler:
            scheduler.stop()
        lp.drain_submissions()
        (tmp_path / 'review-F3-public-sell.json').write_text(json.dumps(
            dict(failure=failure, records=records), default=str, indent=2))


@pytest.mark.parametrize('failure', ['balance_failed', 'positions_failed'])
def test_partial_account_failure_uses_current_owned_buy_quantity(request, runtime, tmp_path, failure):
    if run_test_in_subprocess(request):
        return
    from copy import deepcopy
    public = SerialPublic(runtime.clock)
    original = _open_order('owned-current-partial', 'BUY', price='.40', original='40',
        token_id=_refill_identity(1)[2], outcome='YES').model_copy(update={
            'market': _refill_identity(1)[1], 'condition_id': _refill_identity(1)[1]})
    store, adapter, account, lp, engine = runtime(public_client=public, orders=(original,))
    engine.lp_auto_configure({'budget_usd': '16', 'target_buy_count': 1})
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    engine.lp_auto_set_desired_running(True)
    initial = engine.lp_auto_run_once(round_id='review-partial-original40')
    first = _yield(store, original.id)
    assert first['state'] == 'known' and Decimal(first['quantity']) == 40 and Decimal(first['capital_usd']) == 16
    owner, = store.lp_sessions()
    sid = owner['session_id']
    audits = deepcopy(store.lp_actions(sid))
    financial_as_of = initial['funds']['as_of']
    history = deepcopy(owner['order_history'])
    controls = {k: deepcopy(owner.get(k)) for k in ('idempotency_key', 'entry_order_id',
        'owned_order_ids', 'stop_requested', 'loss_requested', 'entry_cancel_requested', 'queue_protection')}
    account.orders = (original.model_copy(update={'size_matched': Decimal('8')}),)
    method = 'get_balance_allowance' if failure == 'balance_failed' else 'list_positions'
    calls = []
    def unavailable(**kwargs):
        calls.append(runtime.clock[0])
        raise TimeoutError('Actual SDK source unavailable after partial match: ' + method)
    setattr(account, method, unavailable)
    runtime.clock[0] += timedelta(seconds=61)
    current = engine.lp_auto_run_once(round_id='review-partial-current32')
    observed = _yield(store, original.id)
    packet = current['account_observation']
    failed = packet['components']['balance' if failure == 'balance_failed' else 'positions']
    assert failed['state'] == 'failed' and failed['error'] and calls
    assert packet['authenticated'] is True and packet['wallet_address'] == packet['account_id'] == owner['account_id']
    assert packet['open_orders_complete'] is True and packet['components']['open_orders']['state'] == 'known'
    row, = packet['open_orders']
    assert row['order_id'] == original.id and row['token_id'] == original.token_id and row['side'] == 'BUY'
    assert Decimal(row['original_size']) == 40 and Decimal(row['size_matched']) == 8 and Decimal(row['price']) == Decimal('.40')
    assert observed['state'] == 'known' and observed['order_id'] == original.id
    assert Decimal(observed['quantity']) == 32, 'Fresh current owned terms replace stale remaining40'
    assert Decimal(observed['capital_usd']) == Decimal('12.80')
    assert observed['order_checked_at'] == packet['components']['open_orders']['checked_at']
    assert observed['order_read_started_at'] == packet['components']['open_orders']['read_started_at']
    assert observed['order_trade_generation'] == packet['trade_generation']
    # Symmetric single1000 levels: remaining32 gives share32/2968,
    # daily24 => hourly percent250/2968, independently rounded here as literal.
    assert Decimal(observed['yield_pct_per_hour']).quantize(Decimal('.000001')) == Decimal('.084232')
    assert datetime.fromisoformat(observed['checked_at'].replace('Z', '+00:00')) == runtime.clock[0]
    assert datetime.fromisoformat(observed['reward_checked_at'].replace('Z', '+00:00')) == runtime.clock[0]
    assert datetime.fromisoformat(str(packet['components']['open_orders']['checked_at']).replace('Z', '+00:00')) == runtime.clock[0]
    assert current['funds']['status'] == 'unknown' and current['funds']['spendable_usd'] is None
    assert current['funds']['as_of'] == financial_as_of
    unchanged = store.lp_session(sid)
    assert unchanged['order_history'] == history, 'Current-order yield evidence does not fabricate a full fill/account publication'
    assert {k: unchanged.get(k) for k in controls} == controls
    assert store.lp_actions(sid) == audits and Decimal(unchanged['buy_filled_quantity']) == 0
    assert account.trades == () and account.positions == ()
    assert account.posts == account.market_orders == account.cancels == []
    tick = engine.lp_tick()
    monitored = _yield(store, original.id)
    assert monitored['state'] == 'known'
    assert Decimal(monitored['quantity']) == 32, 'Ordinary monitoring must retain proved current remaining32'
    assert Decimal(monitored['capital_usd']) == Decimal('12.80')
    assert Decimal(monitored['yield_pct_per_hour']).quantize(Decimal('.000001')) == Decimal('.084232')
    for key in ('order_checked_at', 'order_read_started_at', 'order_trade_generation'):
        assert monitored[key] == observed[key], 'Monitoring must retain the actual current-order source provenance'
    after_tick = store.lp_session(sid)
    expected_controls = deepcopy(controls)
    expected_controls['queue_protection']['data_failures'] += 1
    assert {k: after_tick.get(k) for k in controls} == expected_controls
    assert after_tick['order_history'] == history and store.lp_actions(sid) == audits
    assert Decimal(after_tick['buy_filled_quantity']) == 0
    assert engine.lp_auto_state()['funds']['as_of'] == financial_as_of
    assert account.posts == account.market_orders == account.cancels == []
    # A newer complete current list contradicts the retained LIVE terms while
    # the same financial source remains unavailable. Absence grants no release.
    account.orders = ()
    runtime.clock[0] += timedelta(seconds=1)
    omitted = engine.lp_auto_run_once(round_id='review-partial-new-complete-omission')
    omission = omitted['account_observation']
    assert omission['authenticated'] is True
    assert omission['account_id'] == omission['wallet_address'] == owner['account_id']
    assert omission['open_orders_complete'] is True and omission['open_orders'] == []
    assert omission['components']['open_orders']['state'] == 'known'
    assert omission['components']['balance' if failure == 'balance_failed' else 'positions']['state'] == 'failed'
    assert datetime.fromisoformat(omission['components']['open_orders']['checked_at'].replace('Z', '+00:00')) > datetime.fromisoformat(observed['order_checked_at'].replace('Z', '+00:00'))
    invalidated = _yield(store, original.id)
    assert invalidated['state'] == 'unknown'
    unknown_since = store.lp_session(sid)['buy_yield_unknown'][original.id]['unknown_since']
    for key in ('order_checked_at', 'order_read_started_at', 'order_trade_generation', 'order_terms'):
        assert invalidated[key] == monitored[key], 'Contradiction retains the old proof only as audit provenance'
    assert omitted['funds']['status'] == 'unknown' and omitted['funds']['spendable_usd'] is None
    assert omitted['funds']['as_of'] == financial_as_of
    assert omitted['slots']['occupied'] == 1 and Decimal(omitted['funds']['buy_reserved_usd']) == 16
    assert Decimal(omitted['funds']['inventory_cost_usd']) == 0
    assert store.lp_session(sid)['order_history'] == history and store.lp_actions(sid) == audits
    assert {k: store.lp_session(sid).get(k) for k in controls} == expected_controls
    runtime.clock[0] += timedelta(seconds=1)
    assert runtime.clock[0] - datetime.fromisoformat(observed['order_checked_at'].replace('Z', '+00:00')) < timedelta(seconds=60)
    invalid_tick = engine.lp_tick()
    still_unknown = _yield(store, original.id)
    assert still_unknown['state'] == 'unknown', 'Normal monitoring cannot revive a proof disproved by newer complete current facts'
    assert store.lp_session(sid)['buy_yield_unknown'][original.id]['unknown_since'] == unknown_since
    for key in ('order_checked_at', 'order_read_started_at', 'order_trade_generation', 'order_terms'):
        assert still_unknown[key] == invalidated[key]
    expected_controls['queue_protection']['data_failures'] += 1
    assert {k: store.lp_session(sid).get(k) for k in controls} == expected_controls
    assert store.lp_session(sid)['order_history'] == history and store.lp_actions(sid) == audits
    assert Decimal(store.lp_session(sid)['buy_filled_quantity']) == 0
    unknown_state = engine.lp_auto_state()
    assert unknown_state['funds']['status'] == 'unknown' and unknown_state['funds']['spendable_usd'] is None
    assert unknown_state['funds']['as_of'] == financial_as_of
    assert unknown_state['slots']['occupied'] == 1 and Decimal(unknown_state['funds']['buy_reserved_usd']) == 16
    assert account.posts == account.market_orders == account.cancels == []
    # Only a later actual exact owned current observation can restore authority.
    account.orders = (original.model_copy(update={'size_matched': Decimal('8')}),)
    runtime.clock[0] += timedelta(seconds=1)
    restored = engine.lp_auto_run_once(round_id='review-partial-later-current32-restoration')
    restoration = restored['account_observation']
    assert restoration['open_orders_complete'] is True
    assert restoration['components']['open_orders']['state'] == 'known'
    assert restoration['components']['balance' if failure == 'balance_failed' else 'positions']['state'] == 'failed'
    revalidated = _yield(store, original.id)
    assert revalidated['state'] == 'known' and revalidated['order_id'] == original.id
    assert Decimal(revalidated['quantity']) == 32 and Decimal(revalidated['capital_usd']) == Decimal('12.80')
    assert Decimal(revalidated['yield_pct_per_hour']).quantize(Decimal('.000001')) == Decimal('.084232')
    assert revalidated['order_checked_at'] == restoration['components']['open_orders']['checked_at']
    assert revalidated['order_read_started_at'] == restoration['components']['open_orders']['read_started_at']
    assert revalidated['order_trade_generation'] == restoration['trade_generation']
    assert datetime.fromisoformat(revalidated['order_checked_at'].replace('Z', '+00:00')) > datetime.fromisoformat(omission['components']['open_orders']['checked_at'].replace('Z', '+00:00'))
    assert original.id not in store.lp_session(sid).get('buy_yield_unknown', {})
    assert restored['funds']['status'] == 'unknown' and restored['funds']['spendable_usd'] is None
    assert restored['funds']['as_of'] == financial_as_of
    assert restored['slots']['occupied'] == 1 and Decimal(restored['funds']['buy_reserved_usd']) == 16
    assert Decimal(restored['funds']['inventory_cost_usd']) == 0
    assert store.lp_session(sid)['order_history'] == history and store.lp_actions(sid) == audits
    assert {k: store.lp_session(sid).get(k) for k in controls} == expected_controls
    assert Decimal(store.lp_session(sid)['buy_filled_quantity']) == 0
    assert account.trades == account.positions == ()
    assert account.posts == account.market_orders == account.cancels == []
    (tmp_path / 'review-F4-current-terms.json').write_text(json.dumps(
        dict(failure=failure, initial=initial, current=current, observed=observed,
            tick=tick, monitored=monitored, omitted=omitted, invalidated=invalidated,
            invalid_tick=invalid_tick, still_unknown=still_unknown, restored=restored,
            revalidated=revalidated), default=str, indent=2))


def test_cold_unknown_entry_waits_for_real_dependency_deadline(request, runtime, tmp_path):
    if run_test_in_subprocess(request):
        return
    from copy import deepcopy
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    store, adapter, account, lp, engine, public = prepare(runtime, budget='8', target=1, count=1)
    engine.lp_auto_configure({'api_retry_interval_seconds': 7})
    def lost(signed):
        account.posts.append(signed)
        raise TimeoutError('Actual original POST response lost without order ID')
    account.post_order = lost
    initial = engine.lp_auto_run_once(round_id='cold-unknown-original-post')
    lp.drain_submissions()
    assert len(account.posts) == 1 and account.orders == ()
    owner, = store.lp_sessions()
    sid = owner['session_id']
    original_audits = deepcopy(store.lp_actions(sid))
    audit, = original_audits
    assert audit['state'] == 'unknown' and audit['post_started'] is True and not audit.get('order_id')
    assert not lp.entry_send_inflight(sid)
    assert initial['slots']['occupied'] == 1 and Decimal(initial['funds']['buy_reserved_usd']) == 8
    identity = {k: deepcopy(owner.get(k)) for k in ('account_id', 'idempotency_key', 'entry_order_id',
        'owned_order_ids', 'price', 'quantity', 'stop_requested', 'loss_requested')}
    adapter.close()
    store, adapter, account, lp, engine = runtime(public_client=public)
    assert not lp.entry_send_inflight(sid) and account.orders == ()
    calls = {name: [] for name in ('get_balance_allowance', 'list_open_orders', 'list_positions')}
    native = {name: getattr(account, name) for name in calls}
    for name in calls:
        def failed(*args, _name=name, **kwargs):
            calls[_name].append(runtime.clock[0])
            raise TimeoutError('Actual cold dependency unavailable: ' + _name)
        setattr(account, name, failed)
    runtime.clock[0] += timedelta(seconds=61)
    deadline = runtime.clock[0] + timedelta(seconds=7)
    scheduler = LPAutoScheduler(engine, clock=lambda: runtime.clock[0])
    records = []
    try:
        assert scheduler.run_due()
        state = engine.lp_auto_state()
        records.append(state)
        assert all(c['state'] == 'failed' for c in state['account_observation']['components'].values())
        assert state['plan_wait'] and state['plan_wait']['kind'] == 'dependencies', state
        assert datetime.fromisoformat(state['plan_wait']['deadline']) == deadline
        assert datetime.fromisoformat(scheduler.snapshot()['next_check_at']) == deadline
        prior_calls, prior_round = deepcopy(calls), deepcopy(state['last_round'])
        assert not scheduler.run_due()
        assert calls == prior_calls and engine.lp_auto_state()['last_round'] == prior_round
        scheduler.request_check()
        assert scheduler.run_due() and calls == prior_calls
        assert not scheduler.run_due()
        runtime.clock[0] = deadline - timedelta(seconds=1)
        assert not scheduler.run_due() and calls == prior_calls
        # A due read remains failed; it cannot prove rejection or release holds.
        runtime.clock[0] = deadline
        assert scheduler.run_due()
        records.append(engine.lp_auto_state())
        assert all(len(at) == 2 and at[-1] == deadline for at in calls.values())
        current = engine.lp_auto_state()
        assert current['slots']['occupied'] == 1 and Decimal(current['funds']['buy_reserved_usd']) == 8
        assert store.lp_actions(sid) == original_audits
        assert {k: store.lp_session(sid).get(k) for k in identity} == identity
        assert account.posts == account.market_orders == account.cancels == []
        assert account.trades == account.positions == ()
    finally:
        scheduler.stop()
        lp.drain_submissions()
        (tmp_path / 'cold-unknown-dependency-evidence.json').write_text(json.dumps(
            dict(initial=initial, audit=original_audits, records=records, calls=calls), default=str, indent=2))


@pytest.mark.parametrize('status', [503, 429])
def test_late_account_retry_survives_shutdown_before_another_round(request, runtime, tmp_path, status):
    if run_test_in_subprocess(request):
        return
    from copy import deepcopy
    from threading import current_thread
    public = SerialPublic(runtime.clock)
    orders = (_resting(1), _resting(2))
    store, adapter, account, lp, engine = runtime(public_client=public, orders=orders)
    engine.lp_auto_configure({'budget_usd': '16', 'target_buy_count': 2, 'api_retry_interval_seconds': 7})
    assert engine.refresh_lp_dashboard_snapshot()['state'] == 'ready'
    engine.lp_auto_set_desired_running(True)
    before = deepcopy(engine.lp_auto_state())
    audits = {s['session_id']: deepcopy(store.lp_actions(s['session_id'])) for s in store.lp_sessions()}
    entered, release, ended, drain_started = Event(), Event(), Event(), Event()
    native_threads, raised_at, calls, records = [], [], [], []
    def delayed_failure(**kwargs):
        native_threads.append(current_thread())
        calls.append(runtime.clock[0])
        entered.set()
        try:
            assert release.wait(5), 'Independent native SDK watchdog'
            raised_at.append(runtime.clock[0])
            raise HTTPError('https://offline.invalid/balance-allowance', status, 'late shutdown failure',
                {'Retry-After': '120'} if status == 503 else {}, None)
        finally:
            ended.set()
    account.get_balance_allowance = delayed_failure
    first_future, round_thread = _round_thread(engine, 'shutdown-first-bounded-timeout')
    drain_result = Future()
    def shutdown():
        drain_started.set()
        try:
            lp.drain_submissions()
            adapter.close()
            drain_result.set_result(True)
        except BaseException as exc:
            drain_result.set_exception(exc)
    drain_thread = Thread(target=shutdown)
    try:
        assert entered.wait(5)
        first = first_future.result(timeout=5)
        records.append(first)
        assert not ended.is_set(), 'Original SDK Future still owns its resource after bounded timeout'
        assert first['funds']['as_of'] == before['funds']['as_of']
        drain_thread.start()
        assert drain_started.wait(5) and not drain_result.done()
        runtime.clock[0] += timedelta(seconds=1)
        release.set()
        assert ended.wait(5)
        for native in native_threads:
            native.join(5)
            assert not native.is_alive()
        assert drain_result.result(timeout=5)
        drain_thread.join(5)
        assert not drain_thread.is_alive()
        expected = raised_at[0] + timedelta(seconds=120 if status == 503 else 7)
        assert len(calls) == 1 and account.posts == account.market_orders == account.cancels == []
        assert all(store.lp_actions(sid) == rows for sid, rows in audits.items())
        # No automatic round collected the completed error before public close.
        store, adapter, account, lp, engine = runtime(public_client=public, orders=orders)
        balance, cold_calls = account.get_balance_allowance, []
        def observed(**kwargs):
            cold_calls.append(runtime.clock[0])
            return balance(**kwargs)
        account.get_balance_allowance = observed
        runtime.clock[0] = expected - timedelta(seconds=1)
        cold = engine.lp_auto_run_once(round_id='shutdown-cold-before-deadline')
        records.append(cold)
        assert cold_calls == [], 'Public shutdown must preserve actual completion-anchored provider/fallback deadline'
        wait = cold.get('account_reader_wait') or {}
        component = ((cold.get('account_observation') or {}).get('components') or {}).get('balance') or {}
        assert datetime.fromisoformat((wait.get('retry_at') or component['retry_at']).replace('Z', '+00:00')) == expected
        assert cold['funds']['as_of'] == before['funds']['as_of']
        assert cold['slots']['occupied'] == 2 and Decimal(cold['funds']['buy_reserved_usd']) == 16
        runtime.clock[0] = expected
        due = engine.lp_auto_run_once(round_id='shutdown-cold-at-deadline')
        records.append(due)
        assert cold_calls == [expected]
        assert account.posts == account.market_orders == account.cancels == []
        assert all(store.lp_actions(sid) == rows for sid, rows in audits.items())
    finally:
        release.set()
        round_thread.join(5)
        assert not round_thread.is_alive()
        if drain_thread.ident is not None:
            drain_thread.join(5)
            assert not drain_thread.is_alive()
        for native in native_threads:
            native.join(5)
            assert not native.is_alive()
        lp.drain_submissions()
        (tmp_path / 'shutdown-late-deadline.json').write_text(json.dumps(
            dict(status=status, raised_at=raised_at, calls=calls, records=records), default=str, indent=2))
