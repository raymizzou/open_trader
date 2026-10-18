"""Same captured input contract; independent financial literals."""
from datetime import UTC, datetime
from decimal import Decimal
import socket
import sqlite3

import pytest

from open_trader.polymarket_lp_market_reader import CapturedSources, MarketReader, SourceResult
from open_trader.polymarket_lp_market_calculation import CalculationPolicy, calculate_current


def test_market_frame_adaptation_has_no_reads_and_preserves_source_identity(monkeypatch):
    from open_trader.polymarket_lp_current_inputs import prepare_current_inputs

    def prohibited(*args, **kwargs):
        pytest.fail('value adapter attempted external I/O')
    monkeypatch.setattr(socket, 'create_connection', prohibited)
    monkeypatch.setattr(sqlite3, 'connect', prohibited)
    checked = datetime(2026, 10, 15, tzinfo=UTC)
    evaluated = datetime(2026, 10, 16, tzinfo=UTC)
    policy = CalculationPolicy(Decimal('40'), 1)
    candidate_values = [dict(condition_id='candidate-c', token_id='candidate-t', price='.40',
        quantity='20', direction=dict(market=dict(reward_min_size=Decimal('1'), reward_max_spread=Decimal('.10')),
        book=dict(bids=[dict(price='.40', size='100')], asks=[dict(price='.42', size='100')]),
        daily_pool_usd='24'), evaluation_at=evaluated)]
    candidates = SourceResult('candidates-original', candidate_values, checked_at=checked)
    for matched, reserved, rate in [('0', '8', '0.892857142857'), ('10', '4', '0.862068965517')]:
        for pool in ('24', '0', None):
            order = dict(account_id='account-A', order_id='o1', condition_id='c1', token_id='t1',
                side='BUY', status='LIVE', price='.40', original_size='20', size_matched=matched)
            market = dict(metadata=dict(condition_id='c1', reward_min_size='1', reward_max_spread='.10'),
                rewards=dict(state='known', complete=True, checked_at=checked,
                    markets=[dict(condition_id='c1', daily_pool_usd=pool)]),
                book=dict(condition_id='c1', token_id='t1', bids=[dict(price='.40', size='100')],
                    asks=[dict(price='.42', size='100')]))
            raw = MarketReader.from_captured_sources(CapturedSources('account-A', dict(
                balance=SourceResult('balance-original', ('100', '100'), checked_at=checked),
                open_orders=SourceResult('orders-original', [order], checked_at=checked),
                positions=SourceResult('positions-original', [], checked_at=checked),
                **{'market:c1:t1': SourceResult('market-original', market, checked_at=checked),
                   'market:other:token': SourceResult('failed-original', complete=False, checked_at=checked,
                       error='network', retry_at=123, failed_at=checked)})))
            prepared = prepare_current_inputs(raw, candidates, policy, evaluated)
            assert prepared.account == raw.account and prepared.orders == raw.orders
            for name, source in raw.sources.items():
                assert prepared.sources[name] is source
            assert 'directions' not in raw.sources and 'candidates' not in raw.sources
            assert prepared.sources['candidates'].source_id == 'candidates-original'
            assert prepared.sources['candidates'].checked_at == checked
            assert prepared.sources['candidates'].value[0]['token_id'] == 'candidate-t'
            assert prepared.sources['candidates'].value[0]['price'] == '.40'
            with pytest.raises(TypeError):
                prepared.sources['directions'].value['o1']['market']['reward_min_size'] = 99
            result = calculate_current(prepared, policy, evaluated)
            assert result.buy_reserved == Decimal(reserved)
            assert result.new_capacity == Decimal('40') - Decimal(reserved)
            actual = result.yields['o1']
            if pool is None:
                assert actual is None
            elif pool == '0':
                assert actual == Decimal(0)
            else:
                assert actual.quantize(Decimal('.000000000001')) == Decimal(rate)
            assert result.target_tokens == (('candidate-t',) if pool == '0' else ('o1',))
            for missing in (None, SourceResult('missing', complete=False, error='network', checked_at=checked)):
                sources = dict(raw.sources)
                sources.pop('market:c1:t1')
                if missing is not None: sources['market:c1:t1'] = missing
                unknown = MarketReader.from_captured_sources(CapturedSources(raw.account, sources))
                adapted = prepare_current_inputs(unknown, None, policy, evaluated)
                assert calculate_current(adapted, policy, evaluated).yields['o1'] is None
                assert adapted.sources['candidates'].value is None
            sources = dict(raw.sources, open_orders=SourceResult('orders-missing', None, complete=False))
            unknown = MarketReader.from_captured_sources(CapturedSources(raw.account, sources))
            result = calculate_current(prepare_current_inputs(unknown, None, policy, evaluated), policy, evaluated)
            assert result.buy_reserved is None and result.new_capacity is None
    candidate_values[0]['price'] = '.99'
    assert prepared.sources['candidates'].value[0]['price'] == '.40'


def test_qualified_pool_capture_is_complete_immutable_and_has_no_io(tmp_path, monkeypatch):
    from datetime import timedelta
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    clock = [now]
    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    service = PolymarketLPService(store, object(), clock=lambda: clock[0])
    ids = tuple(f'condition-{i:02}' for i in range(13))
    for i, cid in enumerate(ids):
        judged = now - timedelta(seconds=301 if i == 11 else 2)
        facts = None if i == 12 else dict(checked_at=judged, directions=[dict(
            market=dict(condition_id=cid, token_id=f'token-{i:02}', metadata_checked_at=judged),
            book=dict(condition_id=cid, token_id=f'token-{i:02}', received_at=judged),
            reward_checked_at=judged)])
        assert service._candidate_pool_record_success(cid, dict(condition_id=cid, state='eligible',
            selected_direction=dict(token_id=f'token-{i:02}')), judged_at=judged, facts=facts,
            global_recovery_generation=7)
    def prohibited(*args, **kwargs): pytest.fail('capture attempted external I/O')
    with monkeypatch.context() as blocked:
        blocked.setattr(socket, 'create_connection', prohibited)
        blocked.setattr(sqlite3, 'connect', prohibited)
        blocked.setattr(store, 'lp_preparation', prohibited)
        blocked.setattr(store, 'lp_candidate_allowed', prohibited)
        first = service.capture_qualified_pool()
        clock[0] += timedelta(seconds=1)
        second = service.capture_qualified_pool()
    assert tuple(first.rows) == ids and tuple(second.rows) == ids
    assert first.inventory_complete and first.generation == 7 and first.exclusion_revision == 0
    assert first.captured_at == now and second.captured_at == now + timedelta(seconds=1)
    assert all(first.rows[cid].complete for cid in ids[:11])
    assert first.rows[ids[11]].error == 'qualification_expired'
    assert first.rows[ids[12]].error == 'qualification_missing'
    original_time = now - timedelta(seconds=2)
    assert first.rows[ids[0]].checked_at == original_time
    assert first.rows[ids[0]].value['facts']['directions'][0]['book']['received_at'] == original_time
    with pytest.raises(TypeError): first.rows[ids[0]].value['row']['state'] = 'changed'
    with pytest.raises(TypeError): first.rows[ids[0]].value['facts']['directions'][0]['book']['received_at'] = now
    assert service._candidate_pool_record_success(ids[0], dict(condition_id=ids[0], state='rejected'),
        judged_at=clock[0], facts=dict(checked_at=clock[0], directions=[]), global_recovery_generation=7)
    assert first.rows[ids[0]].value['row']['state'] == 'eligible'
    assert first.rows[ids[0]].checked_at == original_time
    # A later actual producer fence must invalidate earlier inventory facts.
    store.lp_save_preparation(dict(generation=2, global_recovery_generation=8))
    assert service._candidate_pool_record_success(ids[0], dict(condition_id=ids[0],state='eligible'),
        judged_at=clock[0],facts=dict(checked_at=clock[0],directions=[]),global_recovery_generation=8)
    advanced=service.capture_qualified_pool()
    assert advanced.generation==8 and not advanced.rows[ids[1]].complete
    def failed_fence():raise OSError('actual producer fence read unavailable')
    with monkeypatch.context() as failed:
        failed.setattr(store,'lp_preparation',failed_fence)
        assert not service._candidate_pool_record_success(ids[1],dict(condition_id=ids[1]),
            judged_at=clock[0],facts=dict(checked_at=clock[0]),global_recovery_generation=8)
    unavailable=service.capture_qualified_pool()
    assert unavailable.generation is None and unavailable.error=='producer_fence_unknown'
    unfenced = PolymarketLPService(PredictionArbitrageStore(tmp_path/'unfenced'), object(), clock=lambda: now)
    assert unfenced._candidate_pool_record_success('unfenced', dict(condition_id='unfenced'), judged_at=now,
        facts=dict(checked_at=now, directions=[]))
    unknown = unfenced.capture_qualified_pool()
    assert unknown.generation is None and unknown.error == 'producer_fence_unknown'
    assert tuple(unknown.rows) == ('unfenced',) and not unknown.rows['unfenced'].complete


def _qualified_service(tmp_path, now, *, condition='candidate-c', token='candidate-t'):
    from datetime import timedelta
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from tests.test_polymarket_lp_risk import _direction
    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    service = PolymarketLPService(store, object(), clock=lambda: now)
    direction = _direction(minimum_order_size='20')
    direction['market'].update(condition_id=condition, token_id=token, market_id='candidate-market',
        metadata_checked_at=now-timedelta(seconds=2), fees_checked_at=now-timedelta(seconds=3))
    direction['book'].update(condition_id=condition, token_id=token, received_at=now-timedelta(seconds=1),
        timestamp=now-timedelta(seconds=1), bids=[dict(price=Decimal('.40'), size=Decimal('1000')),
            dict(price=Decimal('.39'), size=Decimal('1000'))], asks=[dict(price=Decimal('.42'), size=Decimal('1000'))])
    direction['reward_checked_at'] = now-timedelta(seconds=4)
    direction['history_summary'] = dict(condition_id=condition, token_id=token, state='known',
        checked_at=now-timedelta(seconds=5), amplitude=Decimal('.01'))
    assert service._candidate_pool_record_success(condition, dict(condition_id=condition, state='eligible',
        selected_direction=dict(token_id=token)), judged_at=now, facts=dict(checked_at=now-timedelta(seconds=4),
        directions=[direction], account=dict(balance='0', allowance='0', authenticated=False),
        reservations=[dict(amount='999999')]), global_recovery_generation=7)
    return service, direction


def test_qualified_candidates_use_same_frame_and_independent_source_coverage(tmp_path, monkeypatch):
    from datetime import timedelta
    from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    service, direction = _qualified_service(tmp_path, now)
    assert service._candidate_pool_record_success('missing-c', dict(condition_id='missing-c'), judged_at=now,
        global_recovery_generation=7)
    stale = {**direction, 'market': {**direction['market'], 'condition_id':'stale-c', 'token_id':'stale-t'},
        'book':{**direction['book'], 'condition_id':'stale-c','token_id':'stale-t', 'received_at':now-timedelta(seconds=61)},
        'history_summary':{**direction['history_summary'], 'condition_id':'stale-c','token_id':'stale-t'}}
    assert service._candidate_pool_record_success('stale-c', dict(condition_id='stale-c'), judged_at=now,
        facts=dict(checked_at=now, directions=[stale]), global_recovery_generation=7)
    pool = service.capture_qualified_pool()
    order = dict(account_id='account-A', order_id='other-buy', condition_id='other-c', token_id='other-t',
        side='BUY',status='LIVE',price='.40',original_size='20',size_matched='10')
    current_direction = dict(metadata=dict(condition_id='other-c',reward_min_size='1',reward_max_spread='.10'),
        rewards=dict(complete=True,checked_at=now,markets=[dict(condition_id='other-c',daily_pool_usd='24')]),
        book=dict(condition_id='other-c',token_id='other-t',bids=[dict(price='.40',size='100')],asks=[dict(price='.42',size='100')]))
    sources=dict(balance=SourceResult('balance-current',('100','100'),checked_at=now-timedelta(seconds=3)),
        open_orders=SourceResult('orders-current',[order],checked_at=now-timedelta(seconds=2)),
        positions=SourceResult('positions-current',[dict(account_id='account-A',condition_id='inventory-c',
            token_id='inventory-t',quantity='10',average_price='.30')],checked_at=now-timedelta(seconds=1)),
        **{'market:other-c:other-t':SourceResult('yield-original',current_direction,checked_at=now)})
    raw = MarketReader.from_captured_sources(CapturedSources('account-A',sources))
    def prohibited(*args, **kwargs): pytest.fail('pure preparation attempted I/O')
    monkeypatch.setattr(socket,'create_connection',prohibited)
    monkeypatch.setattr(sqlite3,'connect',prohibited)
    for level, price in [(1,Decimal('.40')),(2,Decimal('.39'))]:
        policy = CalculationPolicy(Decimal('40'),2) if level==1 else CalculationPolicy(Decimal('40'),2,buy_price_level=2)
        prepared = prepare_current_inputs(raw,pool,policy,now)
        result = calculate_current(prepared,policy,now)
        assert (result.buy_reserved,result.inventory_cost,result.new_capacity)==(Decimal('4'),Decimal('3'),Decimal('33'))
        assert result.target_tokens == ('other-buy', 'candidate-t')
        row, = prepared.sources['candidates'].value
        assert row['price']==price and row['quantity']==Decimal('20')
        assert row['policy_version']==policy.config_version
        assert prepared.sources['candidate_inventory'].value==('candidate-c','missing-c','stale-c')
        assert prepared.sources['candidate_sources'].value['candidate-c:candidate-t'].checked_at==now-timedelta(seconds=4)
        assert prepared.sources['candidate_eligibility'].value['missing-c']['state']=='unknown'
        assert prepared.sources['candidate_eligibility'].value['stale-c:stale-t']['state']=='unknown'
        assert 'book_freshness_stale' in prepared.sources['candidate_eligibility'].value['stale-c:stale-t']['reason_codes']
        assert row['direction']['market']['fees_checked_at']==now-timedelta(seconds=3)
        assert row['direction']['reward_checked_at']==now-timedelta(seconds=4)
        assert prepared.sources['current_account'].checked_at==now-timedelta(seconds=3)
        for name,source in raw.sources.items(): assert prepared.sources[name] is source
        for bad in (None,SourceResult('bad',('100','100'),checked_at=now,error='partial'),
                SourceResult('stale',('100','100'),checked_at=now-timedelta(seconds=61))):
            broken=dict(sources)
            if bad is None: broken.pop('balance')
            else: broken['balance']=bad
            frame=MarketReader.from_captured_sources(CapturedSources('account-A',broken))
            unknown=calculate_current(prepare_current_inputs(frame,pool,policy,now),policy,now)
            assert unknown.new_capacity is None and unknown.target_tokens==()
            assert unknown.yields['other-buy']==result.yields['other-buy'] and unknown.yields['other-buy'] is not None


def test_qualified_pool_capture_handles_sdk_trade_in_cached_account(tmp_path, monkeypatch):
    from collections.abc import Mapping
    from datetime import timedelta

    from polymarket.models.clob.account import ClobTrade

    from open_trader import polymarket_trading
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from tests.test_polymarket_lp import _SDKAccountClient

    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    checked_at = now - timedelta(seconds=4)
    book_at = now - timedelta(seconds=3)
    condition_id = 'candidate-sdk-trade'
    token_id = 'candidate-sdk-token'
    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))

    account_client = _SDKAccountClient(now)
    account_client.list_open_orders = lambda **_: []
    account_client.list_positions = lambda **_: []
    trade_reads = [0]
    list_account_trades = account_client.list_account_trades

    def counted_list_account_trades(*args, **kwargs):
        trade_reads[0] += 1
        return list_account_trades(*args, **kwargs)

    account_client.list_account_trades = counted_list_account_trades
    adapter = PolymarketTradingClient(
        TradingConfig('0x' + '1' * 40, '0x' + '2' * 40), account_client
    )

    class ControlledTime:
        def __init__(self, delegate):
            self._delegate = delegate

        def monotonic(self):
            return 1000.0

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    try:
        with monkeypatch.context() as controlled:
            controlled.setattr(
                polymarket_trading,
                'time',
                ControlledTime(polymarket_trading.time),
            )
            fenced = adapter.lp_account_snapshot_shared(
                trade_generation_provider=store.lp_trade_generation
            )
            account = adapter.lp_account_snapshot_shared()
        assert trade_reads[0] == 1
    finally:
        adapter.close()
    assert fenced['trade_generation'] == store.lp_trade_generation()
    assert isinstance(account['raw_trades'][0], ClobTrade)

    service = PolymarketLPService(store, object(), clock=lambda: now)
    row = dict(
        condition_id=condition_id,
        state='eligible',
        selected_direction=dict(token_id=token_id),
    )
    direction = dict(
        market=dict(
            condition_id=condition_id,
            token_id=token_id,
            market_id='candidate-sdk-market',
            minimum_order_size=Decimal('1'),
            reward_min_size=Decimal('1'),
            reward_max_spread=Decimal('.10'),
            metadata_checked_at=checked_at,
            fees_checked_at=checked_at,
        ),
        book=dict(
            condition_id=condition_id,
            token_id=token_id,
            received_at=book_at,
            timestamp=book_at,
            bids=[dict(price=Decimal('.40'), size=Decimal('100'))],
            asks=[dict(price=Decimal('.42'), size=Decimal('100'))],
        ),
        reward_checked_at=checked_at,
        history_summary=dict(
            condition_id=condition_id,
            token_id=token_id,
            state='known',
            checked_at=checked_at,
            amplitude=Decimal('.01'),
        ),
    )
    assert service._candidate_pool_record_success(
        condition_id,
        row,
        judged_at=now,
        facts=dict(checked_at=checked_at, directions=[direction], account=account),
        global_recovery_generation=7,
    )
    private_facts = service._candidate_qualification_facts[condition_id]
    private_account = private_facts['account']
    private_trade = private_account['raw_trades'][0]
    private_trade_fields = private_trade.model_dump(mode='python')

    def prohibited(*args, **kwargs):
        pytest.fail('capture attempted external I/O')

    with monkeypatch.context() as blocked:
        blocked.setattr(socket, 'create_connection', prohibited)
        blocked.setattr(sqlite3, 'connect', prohibited)
        captured = service.capture_qualified_pool()

    assert captured.generation == 7
    assert captured.captured_at == now
    assert tuple(captured.rows) == (condition_id,)
    captured_source = captured.rows[condition_id]
    assert captured_source.complete is True
    assert captured_source.checked_at == checked_at
    assert captured_source.value['row']['condition_id'] == condition_id
    assert captured_source.value['row']['selected_direction']['token_id'] == token_id
    captured_direction = captured_source.value['facts']['directions'][0]
    assert captured_direction['market']['condition_id'] == condition_id
    assert captured_direction['market']['token_id'] == token_id
    assert captured_direction['book']['bids'][0]['price'] == Decimal('.40')
    assert captured_direction['book']['asks'][0]['price'] == Decimal('.42')

    def contains_sdk_trade(value):
        if isinstance(value, ClobTrade):
            return True
        if isinstance(value, Mapping):
            return any(contains_sdk_trade(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(contains_sdk_trade(item) for item in value)
        if hasattr(value, '__dict__'):
            return any(contains_sdk_trade(item) for item in vars(value).values())
        return False

    assert not contains_sdk_trade(captured.rows)
    assert service._candidate_qualification_facts[condition_id] is private_facts
    assert private_facts['account'] is private_account
    assert private_facts['account']['raw_trades'][0] is private_trade
    assert isinstance(private_facts['account']['raw_trades'][0], ClobTrade)
    assert private_facts['account']['raw_trades'][0].model_dump(mode='python') == private_trade_fields
    with pytest.raises(TypeError):
        captured_source.value['row']['state'] = 'rejected'
    with pytest.raises(TypeError):
        captured_direction['book']['bids'][0]['price'] = Decimal('.01')

    row['state'] = 'rejected'
    direction['book']['bids'][0]['price'] = Decimal('.01')
    assert captured_source.value['row']['state'] == 'eligible'
    assert captured_direction['book']['bids'][0]['price'] == Decimal('.40')


def test_qualified_capture_uses_exact_facts_allow_list_and_preserves_private_cache(tmp_path):
    from copy import deepcopy
    from datetime import timedelta

    from polymarket.models.clob.account import ClobTrade

    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from tests.test_polymarket_lp import _SDKAccountClient

    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    checked_at = now - timedelta(seconds=4)
    cid, token = 'allow-list-c', 'allow-list-t'
    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    service = PolymarketLPService(store, object(), clock=lambda: now)
    account_model = _SDKAccountClient(now).trade
    assert isinstance(account_model, ClobTrade)
    class Sentinel:
        def __deepcopy__(self, memo):
            return self

    sentinel = Sentinel()
    direction = dict(
        market=dict(condition_id=cid, token_id=token, market_id='allow-list-market',
                    minimum_order_size=Decimal('1'), reward_min_size=Decimal('1'),
                    reward_max_spread=Decimal('.10'), metadata_checked_at=checked_at,
                    fees_checked_at=checked_at),
        book=dict(condition_id=cid, token_id=token, received_at=checked_at,
                  timestamp=checked_at, bids=[dict(price=Decimal('.40'), size=Decimal('100'))],
                  asks=[dict(price=Decimal('.42'), size=Decimal('100'))]),
        reward_checked_at=checked_at,
        history_summary=dict(condition_id=cid, token_id=token, state='known',
                             checked_at=checked_at, amplitude=Decimal('.01')),
    )
    facts = dict(checked_at=checked_at, directions=[direction],
                 global_recovery_generation=7,
                 account=dict(raw_trades=(account_model,), balance='100'),
                 reservations=(sentinel,), future_unknown=sentinel)
    assert service._candidate_pool_record_success(
        cid, dict(condition_id=cid, state='eligible', selected_direction=dict(token_id=token)),
        judged_at=now, facts=facts, global_recovery_generation=7)
    private = service._candidate_qualification_facts[cid]
    private_account = private['account']
    private_trade = private_account['raw_trades'][0]
    private_reservations = private['reservations']
    private_unknown = private['future_unknown']
    private_entry = private
    private_directions = private['directions']
    private_direction = private_directions[0]
    private_checked = private['checked_at']
    private_generation = private['global_recovery_generation']
    private_contents = deepcopy(private)

    captured = service.capture_qualified_pool()
    source = captured.rows[cid]
    assert source.complete is True
    assert set(source.value['facts']) == {
        'directions', 'checked_at', 'global_recovery_generation'
    }
    assert 'account' not in source.value['facts']
    assert 'reservations' not in source.value['facts']
    assert 'future_unknown' not in source.value['facts']
    assert isinstance(private['account']['raw_trades'][0], ClobTrade)
    assert private['account'] is private_account
    assert private['account']['raw_trades'][0] is private_trade
    assert private['reservations'] is private_reservations
    assert private['future_unknown'] is private_unknown
    assert service._candidate_qualification_facts[cid] is private_entry
    assert private['directions'] is private_directions
    assert private['directions'][0] is private_direction
    assert private['checked_at'] == private_checked
    assert private['global_recovery_generation'] == private_generation
    assert private == private_contents
    private['directions'][0]['book']['bids'][0]['price'] = Decimal('.01')
    assert source.value['facts']['directions'][0]['book']['bids'][0]['price'] == Decimal('.40')


def test_qualified_capture_freezer_agrees_with_reader_value_contract(tmp_path):
    from collections.abc import Mapping
    from dataclasses import dataclass
    from datetime import date
    from math import isnan
    from types import MappingProxyType

    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_market_reader import freeze
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from tests.test_polymarket_lp import _SDKAccountClient

    now = datetime(2026, 10, 16, 1, tzinfo=UTC)

    @dataclass
    class Unsupported:
        value: str

    class HybridScalar(str):
        pass

    Mapping.register(HybridScalar)

    supported = [
        None, 'text', True, 3, 1.25, float('nan'), float('inf'), float('-inf'),
        Decimal('.40'), now,
        {'key': [None, (Decimal('.10'), now)]},
        {1: 'value'},
        MappingProxyType({'key': ('value',)}),
        HybridScalar('mapping-registered-string'),
    ]
    rejected = [
        b'bytes', date(2026, 10, 16), {1}, frozenset({1}),
        Unsupported('sentinel'), object(), _SDKAccountClient(now).trade,
        {'nested': [_SDKAccountClient(now).trade]},
        {'nested': {'deeper': (_SDKAccountClient(now).trade,)}},
    ]
    values = [(True, value) for value in supported] + [(False, value) for value in rejected]

    # Use a local cache fixture so the public capture seam sees the exact
    # producer representations, including mappingproxy values that a normal
    # publication deepcopy cannot carry.
    for index, (accepted, value) in enumerate(values):
        root = tmp_path / f'capture-freezer-{index}'
        store = PredictionArbitrageStore(root)
        store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
        service = PolymarketLPService(store, object(), clock=lambda: now)
        cid = f'freezer-{index}'
        row = dict(condition_id=cid, state='eligible', selected_direction={
            'token_id': f'token-{index}'})
        assert service._candidate_pool_record_success(
            cid, row, judged_at=now, facts=dict(checked_at=now, directions=[]),
            global_recovery_generation=7)
        service._candidate_qualification_facts[cid] = dict(
            checked_at=now,
            directions=[dict(payload=value)],
            global_recovery_generation=7,
        )
        if accepted:
            captured = service.capture_qualified_pool()
            source = captured.rows[cid]
            expected = freeze(dict(row=service._candidate_pool[cid], facts={
                'directions': service._candidate_qualification_facts[cid]['directions'],
                'checked_at': now,
                'global_recovery_generation': 7,
            }))
            assert source.complete is True and source.value == expected
            assert type(source.value) is MappingProxyType
            assert type(source.value['facts']) is MappingProxyType
            assert tuple(source.value) == ('row', 'facts')
            literal_row = source.value['row']
            assert type(literal_row) is MappingProxyType
            assert set(literal_row) == {
                'condition_id', 'state', 'selected_direction', 'updated_at',
                'expires_at', 'refresh_failed', 'global_recovery_generation',
            }
            assert literal_row['condition_id'] == cid
            assert literal_row['state'] == 'eligible'
            assert literal_row['selected_direction'] == MappingProxyType(
                {'token_id': f'token-{index}'})
            assert literal_row['updated_at'] == '2026-10-16T01:00:00.000000Z'
            assert literal_row['expires_at'] == '2026-10-16T01:05:00.000000Z'
            assert literal_row['refresh_failed'] is False
            assert literal_row['global_recovery_generation'] == 7
            literal_facts = source.value['facts']
            assert set(literal_facts) == {
                'directions', 'checked_at', 'global_recovery_generation'
            }
            assert literal_facts['checked_at'] == now
            assert literal_facts['global_recovery_generation'] == 7
            assert type(literal_facts['directions']) is tuple
            assert len(literal_facts['directions']) == 1
            assert type(literal_facts['directions'][0]) is MappingProxyType
            payload = source.value['facts']['directions'][0]['payload']
            if value is None:
                assert payload is None
            elif isinstance(value, HybridScalar):
                assert type(payload) is HybridScalar and payload == 'mapping-registered-string'
            elif value == 'text':
                assert type(payload) is str and payload == 'text'
            elif value is True:
                assert type(payload) is bool and payload is True
            elif value == 3:
                assert type(payload) is int and payload == 3
            elif value == 1.25:
                assert type(payload) is float and payload == 1.25
            elif isinstance(value, float) and isnan(value):
                assert type(payload) is float and isnan(payload)
            elif value in (float('inf'), float('-inf')):
                assert type(payload) is float and payload == value
            elif value == Decimal('.40'):
                assert type(payload) is Decimal and payload == Decimal('.40')
            elif value is now:
                assert type(payload) is datetime and payload == now
            elif isinstance(value, MappingProxyType):
                assert type(payload) is MappingProxyType
                assert tuple(payload['key']) == ('value',)
                assert type(payload['key']) is tuple
            elif value == {'key': [None, (Decimal('.10'), now)]}:
                assert type(payload) is MappingProxyType
                assert type(payload['key']) is tuple
                assert payload['key'][0] is None
                assert payload['key'][1] == (Decimal('.10'), now)
                assert type(payload['key'][1]) is tuple
            elif value == {1: 'value'}:
                assert type(payload) is MappingProxyType
                assert dict(payload) == {'1': 'value'}
            if value == float('inf'):
                assert source.value['facts']['directions'][0]['payload'] == float('inf')
            if value == float('-inf'):
                assert source.value['facts']['directions'][0]['payload'] == float('-inf')
            if isinstance(value, dict) and 1 in value:
                assert source.value['facts']['directions'][0]['payload']['1'] == 'value'
        else:
            with pytest.raises(TypeError):
                freeze(value)
            captured = service.capture_qualified_pool()
            source = captured.rows[cid]
            assert source.value is None and source.complete is False
            assert source.error == 'qualification_value_invalid'
            assert len(source.error_facts['path']) <= 128
            assert len(source.error_facts['type']) <= 128
            expected_types = (
                'bytes', 'date', 'set', 'frozenset', 'Unsupported', 'object',
                'ClobTrade', 'ClobTrade', 'ClobTrade',
            )
            assert source.error_facts['type'] == expected_types[index - len(supported)]
            assert source.error_facts['path'].startswith('packet.facts.directions[0].payload')


def test_qualified_capture_only_isolates_freeze_type_errors(tmp_path):
    from collections.abc import Mapping
    from datetime import date, timedelta

    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    service, direction = _qualified_service(tmp_path / 'invalid', now,
                                            condition='invalid-c', token='invalid-t')
    service._candidate_qualification_facts['invalid-c']['directions'][0]['book']['sdk_extra'] = object()
    captured = service.capture_qualified_pool()
    invalid = captured.rows['invalid-c']
    assert invalid.value is None and invalid.complete is False
    assert invalid.error == 'qualification_value_invalid'
    assert invalid.error_facts['path'].endswith('sdk_extra')

    expired_store = PredictionArbitrageStore(tmp_path / 'expired')
    expired_store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    expired = PolymarketLPService(expired_store, object(), clock=lambda: now)
    expired_row = dict(condition_id='expired-c', state='eligible', selected_direction={
        'token_id': 'expired-t'}, unsupported=object())
    assert expired._candidate_pool_record_success(
        'expired-c', expired_row, judged_at=now - timedelta(seconds=301),
        facts=dict(checked_at=now - timedelta(seconds=301), directions=[]),
        global_recovery_generation=7)
    expired_capture = expired.capture_qualified_pool()
    expired_source = expired_capture.rows['expired-c']
    assert expired_source.value is None and expired_source.complete is False
    assert expired_source.error == 'qualification_expired'
    assert expired_source.error_facts['path'].startswith('packet.row.key_')
    assert 'unsupported' not in expired_source.error_facts['path']

    class BrokenMapping(Mapping):
        def __getitem__(self, key):
            raise TypeError('projection_type_error')

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

    projection_store = PredictionArbitrageStore(tmp_path / 'projection')
    projection_store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    projection = PolymarketLPService(projection_store, object(), clock=lambda: now)
    assert projection._candidate_pool_record_success(
        'projection-c', dict(condition_id='projection-c', state='eligible'),
        judged_at=now, facts=dict(checked_at=now, directions=[]),
        global_recovery_generation=7)
    projection._candidate_qualification_facts['projection-c'] = BrokenMapping()
    with pytest.raises(TypeError, match='projection_type_error'):
        projection.capture_qualified_pool()

    class TraversalMapping(Mapping):
        def __init__(self, error):
            self.error = error

        def __getitem__(self, key):
            raise KeyError(key)

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

        def items(self):
            raise self.error

    traversal_store = PredictionArbitrageStore(tmp_path / 'traversal')
    traversal_store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    traversal = PolymarketLPService(traversal_store, object(), clock=lambda: now)
    assert traversal._candidate_pool_record_success(
        'traversal-c', dict(condition_id='traversal-c', state='eligible'),
        judged_at=now, facts=dict(checked_at=now, directions=[],
                                  global_recovery_generation=7),
        global_recovery_generation=7)
    traversal._candidate_qualification_facts['traversal-c'] = dict(
        checked_at=now, directions=TraversalMapping(TypeError('items_type_error')),
        global_recovery_generation=7)
    with pytest.raises(TypeError, match='items_type_error'):
        traversal.capture_qualified_pool()

    non_type_store = PredictionArbitrageStore(tmp_path / 'non-type')
    non_type_store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    non_type = PolymarketLPService(non_type_store, object(), clock=lambda: now)
    assert non_type._candidate_pool_record_success(
        'non-type-c', dict(condition_id='non-type-c', state='eligible'),
        judged_at=now, facts=dict(checked_at=now, directions=[],
                                  global_recovery_generation=7),
        global_recovery_generation=7)
    non_type._candidate_qualification_facts['non-type-c'] = dict(
        checked_at=now, directions=TraversalMapping(RuntimeError('items_runtime_error')),
        global_recovery_generation=7)
    with pytest.raises(RuntimeError, match='items_runtime_error'):
        non_type.capture_qualified_pool()

    class MetadataRow(dict):
        def get(self, key, default=None):
            raise RuntimeError('source_metadata_error')

    metadata_store = PredictionArbitrageStore(tmp_path / 'metadata')
    metadata_store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    metadata = PolymarketLPService(metadata_store, object(), clock=lambda: now)
    assert metadata._candidate_pool_record_success(
        'metadata-c', dict(condition_id='metadata-c', state='eligible'),
        judged_at=now, facts=dict(checked_at=now, directions=[]),
        global_recovery_generation=7)
    metadata._candidate_pool['metadata-c'] = MetadataRow(
        condition_id='metadata-c', global_recovery_generation=7)
    with pytest.raises(RuntimeError, match='source_metadata_error'):
        metadata.capture_qualified_pool()

    unknown_store = PredictionArbitrageStore(tmp_path / 'fence-unknown')
    unknown = PolymarketLPService(unknown_store, object(), clock=lambda: now)
    assert unknown._candidate_pool_record_success(
        'unknown-c', dict(condition_id='unknown-c', state='eligible'),
        judged_at=now, facts=dict(checked_at=now,
                                  directions=[dict(bad=object())]))
    unknown_source = unknown.capture_qualified_pool().rows['unknown-c']
    assert unknown_source.error == 'producer_fence_unknown'
    assert unknown_source.error_facts['type'] == 'object'

    changed_store = PredictionArbitrageStore(tmp_path / 'fence-changed')
    changed_store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    changed = PolymarketLPService(changed_store, object(), clock=lambda: now)
    assert changed._candidate_pool_record_success(
        'changed-c', dict(condition_id='changed-c', state='eligible'),
        judged_at=now, facts=dict(checked_at=now,
                                  directions=[dict(bad=object())]),
        global_recovery_generation=7)
    changed_store.lp_save_preparation(dict(generation=2, global_recovery_generation=8))
    changed._qualified_pool_generation = 8
    changed_source = changed.capture_qualified_pool().rows['changed-c']
    assert changed_source.error == 'producer_fence_changed'
    assert changed_source.error_facts['type'] == 'object'

    time_store = PredictionArbitrageStore(tmp_path / 'bad-time')
    time_store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    bad_time = PolymarketLPService(time_store, object(), clock=lambda: now)
    assert bad_time._candidate_pool_record_success(
        'bad-time-c', dict(condition_id='bad-time-c', state='eligible'),
        judged_at=now, facts=dict(checked_at=date(2026, 10, 16),
                                  directions=[dict(bad=object())]),
        global_recovery_generation=7)
    bad_time_source = bad_time.capture_qualified_pool().rows['bad-time-c']
    assert bad_time_source.error == 'qualification_time_unknown'
    assert bad_time_source.error_facts['type'] == 'object'


def test_qualified_pool_capture_rejects_invalid_row_without_losing_inventory(tmp_path):
    from copy import deepcopy
    from tests.test_polymarket_lp import _SDKAccountClient

    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    service, direction = _qualified_service(tmp_path / 'neighbors', now,
                                            condition='healthy-c', token='healthy-t')
    bad_direction = deepcopy(direction)
    bad_direction['market'].update(condition_id='invalid-c', token_id='invalid-t',
                                   market_id='invalid-market')
    bad_direction['book'].update(condition_id='invalid-c', token_id='invalid-t')
    assert service._candidate_pool_record_success(
        'invalid-c', dict(condition_id='invalid-c', state='eligible',
                          selected_direction=dict(token_id='invalid-t')),
        judged_at=now, facts=dict(checked_at=now, directions=[bad_direction]),
        global_recovery_generation=7)
    service._candidate_qualification_facts['invalid-c']['directions'][0]['book']['sdk_extra'] = _SDKAccountClient(now).trade

    captured = service.capture_qualified_pool()
    invalid = captured.rows['invalid-c']
    healthy = captured.rows['healthy-c']
    assert tuple(captured.rows) == ('healthy-c', 'invalid-c')
    assert invalid.value is None and invalid.complete is False
    assert invalid.error == 'qualification_value_invalid'
    assert healthy.complete is True and healthy.error is None
    assert healthy.value['facts']['directions'][0]['market']['token_id'] == 'healthy-t'


def test_invalid_qualification_capture_is_visible_and_cannot_authorize_buy(tmp_path):
    from copy import deepcopy
    from datetime import timedelta
    from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
    from tests.test_polymarket_lp import _SDKAccountClient

    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    service, direction = _qualified_service(tmp_path / 'visible', now,
                                            condition='healthy-c', token='healthy-t')
    bad_direction = deepcopy(direction)
    bad_direction['market'].update(condition_id='invalid-c', token_id='invalid-t',
                                   market_id='invalid-market')
    bad_direction['book'].update(condition_id='invalid-c', token_id='invalid-t')
    assert service._candidate_pool_record_success(
        'invalid-c', dict(condition_id='invalid-c', state='eligible',
                          selected_direction=dict(token_id='invalid-t')),
        judged_at=now, facts=dict(checked_at=now, directions=[bad_direction]),
        global_recovery_generation=7)
    service._candidate_qualification_facts['invalid-c']['directions'][0]['book']['sdk_extra'] = _SDKAccountClient(now).trade
    capture = service.capture_qualified_pool()

    from open_trader.polymarket_lp_market_reader import CapturedSources, MarketReader, SourceResult
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy, calculate_current
    checked = now - timedelta(seconds=1)
    frame = MarketReader.from_captured_sources(CapturedSources('account-A', {
        'balance': SourceResult('balance', ('100', '100'), checked_at=checked),
        'open_orders': SourceResult('orders', [], checked_at=checked),
        'positions': SourceResult('positions', [], checked_at=checked),
    }))
    prepared = prepare_current_inputs(frame, capture, CalculationPolicy(Decimal('40'), 1), now)
    eligibility = prepared.sources['candidate_eligibility'].value
    assert eligibility['invalid-c']['state'] == 'unknown'
    assert 'qualification_value_invalid' in eligibility['invalid-c']['reason_codes']
    assert 'invalid-t' not in tuple(prepared.sources['candidates'].value or ())
    result = calculate_current(prepared, CalculationPolicy(Decimal('40'), 1), now)
    assert result.target_tokens == ('healthy-t',)


def test_qualification_capture_never_invents_valid_candidates_from_unknown_inputs(tmp_path):
    from datetime import timedelta
    from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
    from open_trader.polymarket_lp_market_reader import CapturedSources, MarketReader, SourceResult
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy, calculate_current
    from tests.test_polymarket_lp import _SDKAccountClient

    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    policy = CalculationPolicy(Decimal('40'), 1)
    service, direction = _qualified_service(tmp_path / 'all-invalid', now,
                                            condition='invalid-c', token='invalid-t')
    service._candidate_qualification_facts['invalid-c']['directions'][0]['book']['sdk_extra'] = _SDKAccountClient(now).trade
    invalid_capture = service.capture_qualified_pool()
    checked = now - timedelta(seconds=1)
    complete_frame = MarketReader.from_captured_sources(CapturedSources('account-A', {
        'balance': SourceResult('balance', ('100', '100'), checked_at=checked),
        'open_orders': SourceResult('orders', [], checked_at=checked),
        'positions': SourceResult('positions', [], checked_at=checked),
    }))
    invalid_prepared = prepare_current_inputs(complete_frame, invalid_capture, policy, now)
    assert calculate_current(invalid_prepared, policy, now).target_tokens == ()

    healthy_service, _ = _qualified_service(tmp_path / 'missing-account', now,
                                             condition='healthy-c', token='healthy-t')
    healthy_capture = healthy_service.capture_qualified_pool()
    missing_frame = MarketReader.from_captured_sources(CapturedSources('account-A', {
        'open_orders': SourceResult('orders', [], checked_at=checked),
        'positions': SourceResult('positions', [], checked_at=checked),
    }))
    missing_prepared = prepare_current_inputs(missing_frame, healthy_capture, policy, now)
    missing_result = calculate_current(missing_prepared, policy, now)
    assert missing_result.target_tokens == ()
    assert missing_result.new_capacity is None


def test_invalid_qualification_capture_warning_is_redacted_bounded_and_rate_limited(tmp_path, monkeypatch):
    from open_trader import polymarket_lp
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path / 'warnings')
    store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    service = PolymarketLPService(store, object(), clock=lambda: now)
    for index in range(11):
        cid = f'warning-c-{index}'
        row = dict(condition_id=cid, state='eligible', selected_direction={
            'token_id': f'warning-token-{index}'},
            **{f'UNTRUSTED-KEY-{index}-' + 'x' * 200: object()})
        assert service._candidate_pool_record_success(
            cid, row, judged_at=now, facts=dict(checked_at=now, directions=[]),
            global_recovery_generation=7)

    warning_records = []
    monkeypatch.setattr(polymarket_lp, '_lp_capture_read_log', lambda emit: emit())
    monkeypatch.setattr(polymarket_lp.logger, 'warning',
                        lambda message, *args: warning_records.append((message, args)))
    clock = [0.0]
    monkeypatch.setattr(polymarket_lp, 'monotonic', lambda: clock[0])
    first = service.capture_qualified_pool()
    assert all(not source.complete and source.error == 'qualification_value_invalid'
               for source in first.rows.values())
    assert len(warning_records) == 1
    facts = warning_records[0][1][1]
    assert facts['event'] == 'lp_qualified_pool_capture_invalid'
    assert facts['skipped'] == 11 and len(facts['samples']) == 10 and facts['omitted'] == 1
    assert all(len(sample['path']) <= 128 and len(sample['type']) <= 128
               and len(sample['market_hash']) == 16 for sample in facts['samples'])
    rendered = repr(warning_records[0])
    assert 'warning-c-' not in rendered and 'warning-token-' not in rendered
    assert 'UNTRUSTED-KEY' not in rendered and 'object at 0x' not in rendered

    clock[0] = 10.0
    second = service.capture_qualified_pool()
    assert len(warning_records) == 1
    assert all(source.error == 'qualification_value_invalid' for source in second.rows.values())
    clock[0] = 60.0
    service.capture_qualified_pool()
    assert len(warning_records) == 2

    healthy_store = PredictionArbitrageStore(tmp_path / 'healthy-warning')
    healthy_store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    healthy = PolymarketLPService(healthy_store, object(), clock=lambda: now)
    assert healthy._candidate_pool_record_success(
        'healthy-c', dict(condition_id='healthy-c', state='eligible'), judged_at=now,
        facts=dict(checked_at=now, directions=[]), global_recovery_generation=7)
    healthy.capture_qualified_pool()
    assert len(warning_records) == 2


def test_qualification_capture_logs_after_releasing_candidate_lock(tmp_path, monkeypatch):
    from threading import Event, Thread
    from open_trader import polymarket_lp
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    now = datetime(2026, 10, 16, 1, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_preparation(dict(generation=1, global_recovery_generation=7))
    service = PolymarketLPService(store, object(), clock=lambda: now)
    assert service._candidate_pool_record_success(
        'lock-c', dict(condition_id='lock-c', state='eligible', bad=object()),
        judged_at=now, facts=dict(checked_at=now, directions=[]),
        global_recovery_generation=7)

    entered, release = Event(), Event()
    acquired = Event()
    monkeypatch.setattr(polymarket_lp, '_lp_capture_read_log', lambda emit: emit())
    monkeypatch.setattr(polymarket_lp.logger, 'warning',
                        lambda *args: (entered.set(), release.wait(5))[1])
    monkeypatch.setattr(polymarket_lp, 'monotonic', lambda: 0.0)
    capture_thread = Thread(target=service.capture_qualified_pool)
    lock_thread = None
    try:
        capture_thread.start()
        assert entered.wait(2), 'logging handler watchdog'

        def acquire_lock():
            with service._candidate_state_lock:
                acquired.set()

        lock_thread = Thread(target=acquire_lock)
        lock_thread.start()
        assert acquired.wait(2), 'candidate lock release watchdog'
    finally:
        release.set()
        capture_thread.join(3)
        if lock_thread is not None:
            lock_thread.join(3)
    assert not capture_thread.is_alive() and not lock_thread.is_alive()

    def failing_warning(*args):
        raise RuntimeError('diagnostic handler failed')

    monkeypatch.setattr(polymarket_lp.logger, 'warning', failing_warning)
    monkeypatch.setattr(polymarket_lp, 'monotonic', lambda: 60.0)
    completed = service.capture_qualified_pool()
    assert completed.rows['lock-c'].value is None
    assert completed.rows['lock-c'].error == 'qualification_value_invalid'
