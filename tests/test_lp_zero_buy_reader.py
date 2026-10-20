"""LPZB-A: real Reader/adapter/D2, with offline SDK and literal oracles."""
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event, Lock, Thread
from types import SimpleNamespace
import time
from urllib.error import HTTPError

import pytest
from polymarket.models.gamma.market import Market
from open_trader.polymarket_lp_market_reader import MarketReader, ReadScope
from open_trader.polymarket_lp_market_calculation import CalculationPolicy, calculate_current
from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_lp_order_registration_contract import (
    _adapter, _SDKPublicClient, _open_order, WALLET, CONDITION_ID, TOKEN_ID, NO_TOKEN_ID,
)
from tests.test_lp_d2_integration import seed_running


MARKET_COMPONENT = f'market:{CONDITION_ID}:{TOKEN_ID}'


class ClosedSDK(_SDKPublicClient):
    def __init__(self, *, case='closed'):
        super().__init__(datetime.now(UTC))
        self.case = case
        self.book_calls = []
        self.reward_calls = []
        self.metadata_calls = []

    def list_markets(self, **kwargs):
        self.metadata_calls.append(kwargs)
        if self.case == 'missing_metadata':
            return ()
        return (Market.parse_response(dict(
            id='market-1', conditionId=CONDITION_ID,
            closed=self.case != 'open_transport', acceptingOrders=False,
            active=False, outcomes=['Yes', 'No'],
            clobTokenIds=[NO_TOKEN_ID, TOKEN_ID] if self.case == 'wrong_identity' else [TOKEN_ID, NO_TOKEN_ID],
        )),)

    def get_order_books(self, *, token_ids):
        self.book_calls.append(tuple(token_ids))
        if self.case == 'open_transport':
            from polymarket.errors import TransportError
            raise TransportError('offline transport failure')
        raise HTTPError('https://offline.invalid/book', 404,
                        'No orderbook exists for the requested token id', {}, None)

    def list_market_rewards(self, **kwargs):
        self.reward_calls.append(kwargs)
        return ()


def closed_adapter(tmp_path, *, case='closed', orders=()):
    public = ClosedSDK(case=case)
    # Wrong identity means the requested token is absent, not merely swapped sides.
    if case == 'wrong_identity':
        original = public.list_markets
        def wrong(**kwargs):
            original(**kwargs)
            return (Market.parse_response(dict(id='market-1', conditionId=CONDITION_ID,
                closed=True, acceptingOrders=False, outcomes=['Yes', 'No'], clobTokenIds=['foreign-yes', 'foreign-no'])),)
        public.list_markets = wrong
    adapter, sdk = _adapter(tmp_path, public_client=public, orders=orders,
        positions=(dict(condition_id=CONDITION_ID, asset=TOKEN_ID, size='200',
                        initial_value='115.6884', current_value='80', cur_price='.40'),))
    sdk.get_balance_allowance = lambda **kwargs: SimpleNamespace(
        balance='443205033', allowances={WALLET: '443205033'})
    return adapter, sdk, public


def test_closed_position_without_book_is_non_tradable_without_poisoning_finances(tmp_path):
    adapter, sdk, public = closed_adapter(tmp_path)
    reader = MarketReader(adapter.lp_reader_source_port())
    store = PredictionArbitrageStore(tmp_path / 'state')
    seed_running(store)
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='lpzb-closed', policy=CalculationPolicy(Decimal('100')), reader=reader)
    try:
        live.initialize(ValidatedStartAdmission('lpzb-closed', 'offline-permit'))
        first = live.run_once().acceptance.observation.source_frame
        assert first.orders_complete and first.positions_complete
        assert first.sources['balance'].value == (Decimal('443.205033'), Decimal('443.205033'))
        assert first.positions[0]['quantity'] == 200
        assert first.inventory_cost == Decimal('115.6884')
        source = first.sources[MARKET_COMPONENT]
        assert source.complete and source.error is None, 'proven closed/no-book must be a known resource state'
        assert source.value['state'] == 'non_tradable'
        assert source.value['book'] is None and source.value['rewards'] is None
        assert source.value['metadata']['closed'] is True
        assert source.value['metadata']['accepting_orders'] is False
        assert source.source_id and source.checked_at and source.value['book_absent'] is True
        calls = (len(public.book_calls), len(public.reward_calls), len(public.metadata_calls))
        second = live.run_once().acceptance.observation.source_frame
        assert (len(public.book_calls), len(public.reward_calls), len(public.metadata_calls)) == calls
        assert second.sources[MARKET_COMPONENT] == source  # original evidence/time, no refresh fiction
        assert second.sources['balance'].source_id != first.sources['balance'].source_id
        assert second.sources['positions'].source_id != first.sources['positions'].source_id
        prepared = prepare_current_inputs(second, None, live.policy, datetime.now(UTC))
        result = calculate_current(prepared, live.policy, datetime.now(UTC))
        assert result.inventory_cost == Decimal('115.6884')
        assert result.new_capacity == 0  # A preserves the baseline budget formula
        snapshot = live.state.snapshot()
        assert snapshot.inventory_cost.state == 'known' and snapshot.inventory_cost.value == Decimal('115.6884')
        assert sdk.posts == [] and sdk.cancels == [] and sdk.market_orders == []
        # Current identity removal invalidates the bounded position-only proof.
        sdk.positions = ()
        live.run_once()
        sdk.positions = (dict(condition_id=CONDITION_ID, asset=TOKEN_ID, size='200', initial_value='115.6884'),)
        reader.read_current(ReadScope(WALLET, related_markets=True), time.monotonic()+3)
        assert len(public.book_calls) == calls[0]+1
    finally:
        assert live.close(timeout=3)
        adapter.close()


@pytest.mark.parametrize('case', ['missing_metadata', 'wrong_identity', 'open_transport', 'active_buy', 'manual_sell'])
def test_unproven_closed_and_active_order_scopes_keep_original_guards(tmp_path, case):
    orders = (_open_order('original-'+case, 'BUY' if case == 'active_buy' else 'SELL', price='.40', original='20'),) if case in ('active_buy', 'manual_sell') else ()
    adapter, sdk, public = closed_adapter(tmp_path, case=case, orders=orders)
    reader = MarketReader(adapter.lp_reader_source_port())
    store = PredictionArbitrageStore(tmp_path / 'state')
    # Observation without automatic start authority must still retain current orders.
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='lpzb-guard', policy=CalculationPolicy(Decimal('100')), reader=reader)
    try:
        live.initialize(ValidatedStartAdmission('lpzb-guard', 'offline-permit'))
        first = live.run_once().acceptance.observation.source_frame
        assert first.inventory_cost == Decimal('115.6884') and first.positions[0]['quantity'] == 200
        market = first.sources[MARKET_COMPONENT]
        if case in ('missing_metadata', 'wrong_identity', 'open_transport'):
            assert not market.complete and market.value is None
            if case == 'open_transport':
                assert market.error == 'TransportError'
        assert not first.current_buys or calculate_current(prepare_current_inputs(first, None, live.policy, datetime.now(UTC)), live.policy, datetime.now(UTC)).yields['original-active_buy'] is None
        second = live.run_once().acceptance.observation.source_frame
        assert second.orders_complete and second.positions_complete
        assert [r['order_id'] for r in second.orders] == ['original-'+case] if orders else second.orders == ()
        if orders:
            assert second.orders[0]['side'] == ('BUY' if case == 'active_buy' else 'SELL')
            assert second.orders[0]['price'] == Decimal('.40') and second.orders[0]['remaining'] == 20
            # A closed resource observation cannot suppress required current-order reads.
            reader.reset_provider_wait()
            before = len(public.book_calls)
            reader.read_current(ReadScope(WALLET, related_markets=True), time.monotonic()+3)
            assert len(public.book_calls) == before+1
            sdk.orders = ()
            reader.reset_provider_wait()
            reader.read_current(ReadScope(WALLET, related_markets=True), time.monotonic()+3)
            sdk.orders = orders
            reader.reset_provider_wait()
            before = len(public.book_calls)
            current = reader.read_current(ReadScope(WALLET, related_markets=True), time.monotonic()+3)
            assert len(public.book_calls) == before+1
            assert not current.sources[MARKET_COMPONENT].complete
        assert sdk.posts == [] and sdk.cancels == [] and sdk.market_orders == []
    finally:
        assert live.close(timeout=3)
        adapter.close()


@pytest.mark.parametrize('kind', ['market', 'protection'])
def test_slow_related_reads_cannot_starve_current_financial_sources(tmp_path, kind):
    adapter, sdk = _adapter(tmp_path)
    external = adapter.lp_reader_source_port()
    slow_names = (('market:first:token-one', 'market:second:token-two') if kind == 'market'
                  else ('protection:["scoring","first"]', 'protection:["scoring","second"]'))
    release, entered, observation_done = Event(), Event(), Event()
    lock = Lock()
    calls, active = [], {}
    peaks = {'total': 0, 'nonfinancial': 0}
    outputs = []
    failures = []

    class Sources:
        def read(self, component, scope):
            with lock:
                calls.append(component)
                active[component] = active.get(component, 0)+1
                peaks['total'] = max(peaks['total'], sum(active.values()))
                peaks['nonfinancial'] = max(peaks['nonfinancial'], sum(v for k,v in active.items() if k in slow_names))
            try:
                if component in slow_names:
                    entered.set()
                    assert release.wait(5), 'independent native I/O release watchdog'
                    return {'finished_component': component}
                return external.read(component, scope)
            finally:
                with lock:
                    active[component] -= 1

    reader = MarketReader(Sources())
    def related_observation():
        try:
            outputs.append(reader.read_current(ReadScope(WALLET, slow_names), time.monotonic()+.15))
        except BaseException as exc:
            failures.append(exc)
        finally:
            observation_done.set()
    observer = Thread(target=related_observation)
    store = PredictionArbitrageStore(tmp_path / 'state')
    seed_running(store)
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='lpzb-slots', policy=CalculationPolicy(Decimal('100')), reader=reader)
    try:
        live.initialize(ValidatedStartAdmission('lpzb-slots', 'offline-permit'))
        observer.start()
        assert entered.wait(2)
        assert observation_done.wait(2), 'observation deadline watchdog'
        observer.join(timeout=2)
        assert not observer.is_alive() and not failures
        historical = outputs[0]
        assert not reader.drain(timeout=0)
        same = reader.read_current(ReadScope(WALLET, (slow_names[0],)), time.monotonic()+1)
        assert same.sources[slow_names[0]].error == 'in_progress'
        started_at = datetime.now(UTC)
        observation = live.run_once(timeout=2).acceptance.observation
        current = observation.source_frame
        assert all(current.sources[n].complete for n in ('balance', 'open_orders', 'positions')), 'related reads starved current financial sources'
        assert all(current.sources[n].source_id and current.sources[n].checked_at >= started_at
                   for n in ('balance', 'open_orders', 'positions'))
        assert current.sources['balance'].value == (Decimal('100'), Decimal('100'))
        assert current.orders_complete and current.positions_complete
        assert live.state.snapshot().inventory_cost.state == 'known'
        assert peaks['total'] <= 2 and peaks['nonfinancial'] == 1
        assert calls.count(slow_names[0]) == 1 and slow_names[1] not in calls
        assert historical.sources[slow_names[0]].error == 'timeout'
        # The observation deadline expired while this component was never admitted.
        assert historical.sources[slow_names[1]].error == 'timeout'
        assert not historical.sources[slow_names[1]].complete
        assert historical.sources[slow_names[1]].source_id == ''
        assert historical.sources[slow_names[1]].value is None
        assert reader.close(timeout=0) is False
        assert reader.drain(timeout=0) is False
        release.set()
        assert reader.drain(timeout=2) and reader.close(timeout=2)
        with lock:
            assert sum(active.values()) == 0
        # Historical timeout snapshots do not mutate after true native completion.
        assert historical.sources[slow_names[0]].error == 'timeout'
        assert sdk.posts == [] and sdk.cancels == []
    finally:
        release.set()
        if observer.ident is not None:
            observer.join(timeout=2)
            assert not observer.is_alive()
        assert live.close(timeout=3)
        adapter.close()

    # Two genuine financial I/Os can consume both slots: preserve UNKNOWN/ownership.
    both = {n: Event() for n in ('balance', 'open_orders')}
    financial_release, financial_done = Event(), Event()
    financial_calls, financial_outputs, financial_errors = [], [], []
    class SlowFinancial:
        def read(self, component, scope):
            financial_calls.append(component)
            both[component].set()
            assert financial_release.wait(5), 'financial release watchdog'
            return ('100', '100') if component == 'balance' else ()
    financial = MarketReader(SlowFinancial())
    def observe_finances():
        try:
            financial_outputs.append(financial.read_current(ReadScope(WALLET, ('balance', 'open_orders')), time.monotonic()+.15))
        except BaseException as exc:
            financial_errors.append(exc)
        finally:
            financial_done.set()
    financial_observer = Thread(target=observe_finances)
    try:
        financial_observer.start()
        assert all(e.wait(2) for e in both.values())
        assert financial_done.wait(2)
        financial_observer.join(timeout=2)
        assert not financial_observer.is_alive() and not financial_errors
        blocked = financial.read_current(ReadScope(WALLET), time.monotonic()+1)
        assert blocked.sources['balance'].error == 'in_progress'
        assert blocked.sources['open_orders'].error == 'in_progress'
        assert blocked.sources['positions'].error == 'capacity'
        assert blocked.buy_reserved is None and blocked.inventory_cost is None
        assert sorted(financial_calls) == ['balance', 'open_orders']
        assert financial.close(timeout=0) is False
        financial_release.set()
        assert financial.drain(timeout=2) and financial.close(timeout=2)
    finally:
        financial_release.set()
        financial_observer.join(timeout=2)
        assert not financial_observer.is_alive()
        assert financial.close(timeout=2)

    if kind == 'market':
        # A single market component has two reward providers. Count actual SDK
        # activity as well as top-level source jobs, retaining the same bound.
        reward_entered, reward_release, market_done = Event(), Event(), Event()
        reward_lock = Lock()
        reward_active, reward_peak, reward_calls = [0], [0], []
        market_results, market_errors = [], []
        class RewardsSDK(_SDKPublicClient):
            def list_market_rewards(self, *, condition_id, sponsored):
                with reward_lock:
                    reward_calls.append((condition_id, sponsored))
                    reward_active[0] += 1
                    reward_peak[0] = max(reward_peak[0], reward_active[0])
                try:
                    reward_entered.set()
                    assert reward_release.wait(5), 'nested reward SDK watchdog'
                    return ()
                finally:
                    with reward_lock:
                        reward_active[0] -= 1
        rewards_public = RewardsSDK(datetime.now(UTC))
        rewards_adapter, _ = _adapter(tmp_path, public_client=rewards_public)
        rewards_reader = MarketReader(rewards_adapter.lp_reader_source_port())
        def observe_market():
            try:
                market_results.append(rewards_reader.read_current(ReadScope(WALLET, (MARKET_COMPONENT,)), time.monotonic()+.15))
            except BaseException as exc:
                market_errors.append(exc)
            finally:
                market_done.set()
        market_observer = Thread(target=observe_market)
        try:
            market_observer.start()
            assert reward_entered.wait(2) and market_done.wait(2)
            market_observer.join(timeout=2)
            assert not market_observer.is_alive() and not market_errors
            fresh = rewards_reader.read_current(ReadScope(WALLET), time.monotonic()+2)
            assert all(fresh.sources[n].complete for n in ('balance', 'open_orders', 'positions'))
            assert rewards_reader.close(timeout=0) is False
            assert reward_peak[0] == 1, 'one market job forked multiple simultaneous native reward reads'
            reward_release.set()
            assert rewards_reader.drain(timeout=2)
            assert set(reward_calls) == {(CONDITION_ID, False), (CONDITION_ID, True)}
            assert reward_active[0] == 0
        finally:
            reward_release.set()
            market_observer.join(timeout=2)
            assert not market_observer.is_alive()
            assert rewards_reader.close(timeout=3)
            rewards_adapter.close()


@pytest.mark.parametrize('case', ['default_transport', 'retry_after', 'late_after_reset'])
def test_orders_transport_wait_expires_once_and_recovers_without_replay(tmp_path, case):
    from polymarket.errors import TransportError
    adapter, sdk = _adapter(tmp_path)
    clock, requests = [0.0], []
    entered, release, observation_done = Event(), Event(), Event()
    armed = [False]
    failed = [False]
    original = sdk.list_open_orders
    def orders(**kwargs):
        requests.append(clock[0])
        if armed[0] and not failed[0]:
            entered.set()
            assert release.wait(5), 'transport failure completion watchdog'
            clock[0] = 10
            failed[0] = True
            if case == 'retry_after':
                raise HTTPError('https://offline.invalid/data/orders', 503, 'offline unavailable', {'Retry-After': '120'}, None)
            raise TransportError('offline transport failure')
        return original(**kwargs)
    sdk.list_open_orders = orders
    reader = MarketReader(adapter.lp_reader_source_port(), clock=lambda: clock[0])
    store = PredictionArbitrageStore(tmp_path / 'state')
    live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch='lpzb-retry', policy=CalculationPolicy(Decimal('100')), reader=reader,
        clock=lambda: clock[0])
    results, errors = [], []
    observer = None
    def delayed_read():
        try:
            results.append(reader.read_current(ReadScope(WALLET, ('open_orders',)), time.monotonic()+.15))
        except BaseException as exc:
            errors.append(exc)
        finally:
            observation_done.set()
    try:
        live.initialize(ValidatedStartAdmission('lpzb-retry', 'offline-permit'))
        healthy = live.run_once().acceptance.observation
        assert healthy.orders_complete and healthy.source_frame.sources['open_orders'].complete
        armed[0] = True
        if case == 'late_after_reset':
            observer = Thread(target=delayed_read)
            observer.start()
            assert entered.wait(2) and observation_done.wait(2)
            observer.join(timeout=2)
            assert not observer.is_alive() and not errors
            assert results[0].sources['open_orders'].error == 'timeout'
            assert reader.drain(timeout=0) is False
            # The old native request fails after reset, so its new epoch deadline survives.
            clock[0] = 10
            reader.reset_provider_wait()
            release.set()
            assert reader.drain(timeout=2)
            failed_source = reader.read_current(ReadScope(WALLET, ('open_orders',)), time.monotonic()+2).sources['open_orders']
        else:
            release.set()
            failed_source = reader.read_current(ReadScope(WALLET, ('open_orders',)), time.monotonic()+2).sources['open_orders']
        expected = 130 if case == 'retry_after' else 70
        assert failed_source.retry_at == expected
        assert failed_source.failed_at is not None and failed_source.checked_at is None
        assert not failed_source.complete and failed_source.value is None
        assert failed_source.error == ('HTTPError' if case == 'retry_after' else 'TransportError')
        assert requests == [0, 0]
        for stamp in (10, 20, expected-1):
            clock[0] = stamp
            observation = live.run_once().acceptance.observation
            source = observation.source_frame.sources['open_orders']
            assert source.retry_at == expected and source.source_id == failed_source.source_id
            assert source.failed_at == failed_source.failed_at
            assert source.diagnostics['retry_cache_hit'] is True
            assert source.diagnostics['remaining_retry_seconds'] == expected-stamp
            assert source.value is None and not observation.orders_complete
            assert live.state.snapshot().new_capacity.state != 'known'
            assert requests == [0, 0]  # cache observations do not restart SDK or extend wait
        clock[0] = expected
        recovered = live.run_once().acceptance.observation
        source = recovered.source_frame.sources['open_orders']
        assert source.complete and source.error is None and source.retry_at is None
        assert source.source_id != failed_source.source_id and source.checked_at is not None
        assert requests == [0, 0, expected] and recovered.orders_complete
        assert live.state.snapshot().new_capacity.state == 'known'
        assert failed_source.retry_at == expected and failed_source.value is None
        assert sdk.posts == [] and sdk.cancels == [] and sdk.market_orders == []
    finally:
        release.set()
        if observer is not None:
            observer.join(timeout=2)
            assert not observer.is_alive()
        assert live.close(timeout=3)
        adapter.close()
