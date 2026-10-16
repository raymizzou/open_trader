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
