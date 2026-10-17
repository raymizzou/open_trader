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
            assert result.target_tokens == ('candidate-t',)
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
        assert result.target_tokens == ('candidate-t',)
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
