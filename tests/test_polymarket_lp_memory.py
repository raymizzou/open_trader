"""LP lifetime checks through complete preparation and candidate refreshes."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import gc
import hashlib
import json
from pathlib import Path
import weakref

import pytest

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_polymarket_lp import _LPCandidateQueryExchange
from lp_memory_replay import ranking_trace, backfill_trace


class Detail(dict):
    live = weakref.WeakSet()
    __hash__ = object.__hash__

    def __new__(cls, *args, **kwargs):
        value = super().__new__(cls)
        cls.live.add(value)
        return value


class MemoryExchange(_LPCandidateQueryExchange):
    kept = "condition-M00"

    def lp_market_metadata(self, condition_ids, *, stop_event=None):
        result = super().lp_market_metadata(condition_ids, stop_event=stop_event)
        for condition, row in result.items():
            row["extra_detail"] = Detail(condition=condition, text="x" * 1000)
        return result

    def lp_market_competitiveness(self, **kwargs):
        result = super().lp_market_competitiveness(**kwargs)
        result["competitiveness"] = {
            condition: (Decimal("2") if condition == self.kept else Decimal("0"), self.now)
            for condition in result["competitiveness"]
        }
        return result


def _unchanged_contract(value, *, estimate=False):
    """Audit only #249's known-estimate fields out of the historical trace.

    All other fields, including UNKNOWN reasons, ranks, deadlines and
    traversal counters, must retain their independently replayed old hash.
    The new full hashes below also protect every removed estimate field.
    """
    if isinstance(value, list):
        return [_unchanged_contract(item) for item in value]
    if not isinstance(value, dict):
        return value
    row = {key: _unchanged_contract(item, estimate=key == 'estimate') for key, item in value.items()}
    if estimate and row.get('state') == 'known':
        for key in ('basis', 'price', 'quantity', 'capital_usd', 'midpoint', 'competition_upper_bound',
                    'hourly_reward_usd', 'yield_pct_per_hour', 'yield_pct_per_hour_display'):
            row.pop(key, None)
        if row.get('reason_codes') == []:
            row.pop('reason_codes')
    if 'estimate_state' in row:
        row.pop('estimate_basis', None)
        if row['estimate_state'] == 'known':
            for key in ('estimated_hourly_reward_usd', 'estimated_yield_raw', 'estimated_yield_pct_per_hour'):
                row.pop(key, None)
    return row


def _trace_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _assert_level_one_provenance(value):
    """The fixed replay uses level one; every new marker must say so."""
    if isinstance(value, list):
        for item in value:
            _assert_level_one_provenance(item)
    elif isinstance(value, dict):
        if 'candidates' in value:
            assert type(value['buy_price_level']) is int
            assert value['buy_price_level'] == 1
        if 'estimate_state' in value:
            assert type(value['estimate_buy_price_level']) is int
            assert value['estimate_buy_price_level'] == 1
        for key, item in value.items():
            if key in ('buy_price_level', 'estimate_buy_price_level'):
                assert type(item) is int and item == 1, (key, item)
            _assert_level_one_provenance(item)


def _legacy_strategy_trace(value):
    """Remove only #309's separately asserted provenance from old hashes."""
    if isinstance(value, list):
        return [_legacy_strategy_trace(item) for item in value]
    if isinstance(value, dict):
        return {key: _legacy_strategy_trace(item) for key, item in value.items()
                if key not in ('buy_price_level', 'estimate_buy_price_level')}
    return value


def test_complete_ranking_trace_matches_issue249_minimum_order_contract(tmp_path):
    fixtures = Path(__file__).parent / 'fixtures'
    historical = json.loads((fixtures / 'lp_memory_ranking_4fbddaae.json').read_text())
    contract = json.loads((fixtures / 'lp_memory_ranking_minimum_order_issue249.json').read_text())
    assert historical['baseline_sha'] == contract['historical_baseline_sha'] == '4fbddaae061e95cbbbb3db4d5793e40e33ec2cae'
    assert contract['contract_version'] == 'issue249_minimum_order_v1'
    assert contract['source_sha'] == '17e59970dd8cbb9732470a49c17ebf952640878b'
    for index, (actual, expected, old) in enumerate(zip(ranking_trace(tmp_path), contract['steps'], historical['steps'], strict=True)):
        assert [row['condition_id'] for row in actual['candidates']] == expected['ranked_conditions'] == old['ranked_conditions']
        assert [row.get('estimated_yield_raw') for row in actual['candidates']] == expected['ranked_yields']
        for row in actual['candidates']:
            if row['estimate_state'] == 'known':
                # The replay changes M01 pool 57.6 -> 24 after its first step;
                # displayed catalog metadata intentionally remains unchanged.
                pool = Decimal(24) if index > 0 and row['condition_id'] == 'condition-M01' else Decimal(row['daily_pool_usd'])
                assert Decimal(row['estimated_target_quantity']) == 20
                assert Decimal(row['estimated_target_capital_usd']) == 10
                assert Decimal(row['estimated_yield_pct_per_hour']) == (pool * Decimal(81) / (24 * 1616 * 10) * 100).quantize(Decimal('.000001'))
        _assert_level_one_provenance(actual)
        legacy = _legacy_strategy_trace(actual)
        assert _trace_hash(_unchanged_contract(legacy)) == expected['unchanged_contract_sha256'], expected['step']
        assert _trace_hash(legacy) == expected['sha256'], (expected['step'], actual)


def test_full_normal_and_backup_traversal_matches_issue249_minimum_order_contract(tmp_path):
    contract = json.loads((Path(__file__).parent / 'fixtures/lp_memory_ranking_minimum_order_issue249.json').read_text())
    trace = backfill_trace(tmp_path)  # Also asserts all 52 distinct tokens were read.
    expected = contract['backfill_invariants']
    assert [step['candidate_valid_count'] for step in trace] == expected['valid'] == [8, 17, 23]
    assert [step['candidate_pending_count'] for step in trace] == expected['pending'] == [16, 6, 0]
    for field, counts in [('unknown', [1, 1, 1]), ('rejected', [1, 2, 3]), ('backup_read', [0, 6, 12])]:
        assert [step['funnel'][field] for step in trace] == expected[field] == counts
    for step in trace:
        for row in step['candidates']:
            assert Decimal(row['estimated_target_quantity']) == 20
            assert Decimal(row['estimated_target_capital_usd']) == Decimal('6.80')
            assert Decimal(row['estimated_yield_pct_per_hour']) == (Decimal(100) * 81 / (24 * 1616 * Decimal('6.80')) * 100).quantize(Decimal('.000001'))
    _assert_level_one_provenance(trace)
    legacy = _legacy_strategy_trace(trace)
    assert _trace_hash(_unchanged_contract(legacy)) == contract['backfill_unchanged_contract_sha256']
    assert _trace_hash(legacy) == contract['backfill_sha256']


def test_preparation_and_rejected_queue_details_are_released_and_can_reenter(tmp_path):
    now = datetime(2026, 9, 20, 8, tzinfo=UTC)
    exchange = MemoryExchange(now, {f"M{i:02}": Decimal("57.6") for i in range(40)})
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: exchange.now)

    assert service.refresh_price_history()["state"] == "known"
    gc.collect()
    assert not Detail.live, "prepared inputs retained the full detailed universe"
    assert service.refresh_competition_cache()["state"] == "known"
    first = service.refresh_candidates()
    assert [row["condition_id"] for row in first["candidates"]] == [exchange.kept]
    assert first["funnel"]["read"] == 40
    assert first["funnel"]["excluded"]["competition_empty"] == 39
    gc.collect()
    assert {row["condition"] for row in Detail.live} <= {exchange.kept}

    # A previously excluded market becomes eligible on a later complete scan.
    exchange.kept = "condition-M39"
    exchange.now += timedelta(seconds=360)
    assert service.refresh_price_history()["state"] == "known"
    assert service.refresh_competition_cache()["state"] == "known"
    second = service.refresh_candidates()
    assert [row["condition_id"] for row in second["candidates"]] == [exchange.kept]
    gc.collect()
    assert {row["condition"] for row in Detail.live} <= {exchange.kept}


@pytest.mark.parametrize("enabled", [False, True])
def test_prepared_reader_keeps_its_generation_without_retaining_objects(tmp_path, monkeypatch, enabled):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    exchange = MemoryExchange(datetime(2026, 9, 20, 8, tzinfo=UTC), {'M00': Decimal('57.6')})
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange,
        clock=lambda: exchange.now, exclusions_enabled=enabled)
    service.refresh_price_history()
    captured = service._prepared_input_snapshot()
    old_time = exchange.now
    exchange.now += timedelta(hours=1)
    entered, release = Event(), Event()
    original_reader = exchange.lp_market_metadata

    def blocked_reader(*args, **kwargs):
        entered.set()
        assert release.wait(10), 'refresh reader was not released'
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(exchange, 'lp_market_metadata', blocked_reader)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.refresh_price_history)
        try:
            assert entered.wait(10), 'refresh did not reach metadata'
            assert service._prepared_input_snapshot()['metadata']['condition-M00']['metadata_checked_at'] == old_time
            assert captured['catalog']['markets'][0]['condition_id'] == 'condition-M00'
        finally:
            release.set()
        assert future.result(timeout=10)['state'] == 'known'
    current = service._prepared_input_snapshot()
    assert captured['metadata']['condition-M00']['metadata_checked_at'] == old_time
    assert current['metadata']['condition-M00']['metadata_checked_at'] == exchange.now
    read = captured['metadata']['condition-M00']
    read['outcomes'].clear()
    assert captured['metadata']['condition-M00']['outcomes']
    assert current['metadata']['condition-M00']['outcomes']
    captured['catalog']['markets'][0]['condition_id'] = 'mutated'
    assert captured['catalog']['markets'][0]['condition_id'] == 'condition-M00'
    with pytest.raises(TypeError):
        captured['metadata']['condition-M00'] = {}


@pytest.mark.parametrize("scope", ["market", "direction"])
def test_prepared_reader_keeps_generation_when_current_metadata_is_excluded(tmp_path, monkeypatch, scope):
    class TwoDirectionExchange(MemoryExchange):
        def lp_market_metadata(self, *args, **kwargs):
            rows = super().lp_market_metadata(*args, **kwargs)
            for row in rows.values():
                row["outcomes"]["no"] = {"label": "NO", "token_id": "opposite-token"}
            return rows

        def lp_order_books(self, tokens, **kwargs):
            rows = super().lp_order_books(tokens, **kwargs)
            for token, row in rows.items():
                if token == "opposite-token":
                    row["condition_id"] = "condition-M00"
            return rows

    exchange = TwoDirectionExchange(datetime(2026, 9, 20, 8, tzinfo=UTC), {"M00": Decimal("57.6")})
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange,
        clock=lambda: exchange.now, exclusions_enabled=True)
    assert service.refresh_price_history()["state"] == "known"
    captured = service._prepared_input_snapshot()
    condition = "condition-M00"
    tokens = {key:value["token_id"] for key,value in captured["metadata"][condition]["outcomes"].items()}
    key = next(iter(tokens))
    gc.collect()
    assert not Detail.live, "captured prepared reader retained detailed Python objects"
    from open_trader.polymarket_lp_scratch import LPReadScratch
    backups = []
    backup = LPReadScratch.__deepcopy__
    def counted_backup(scratch, memo):
        backups.append(len(scratch))
        return backup(scratch, memo)
    monkeypatch.setattr(LPReadScratch, "__deepcopy__", counted_backup)
    version = service._prepared_inputs_version
    assert service._exclude_candidate(condition, "" if scope == "market" else tokens[key],
        "market_not_accepting_orders" if scope == "market" else "history_amplitude_exceeded",
        checked_at=exchange.now)
    assert backups == [], "ordinary qualification exclusion copied prepared metadata"
    current = service._prepared_input_snapshot()
    assert service._prepared_inputs_version == version
    identities = tuple((condition, token) for token in tokens.values())
    expected = () if scope == "market" else tuple(pair for pair in identities if pair[1] != tokens[key])
    assert service._candidate_allowed(identities) == expected
    for reader in (captured, current):
        assert set(reader["metadata"][condition]["outcomes"]) == set(tokens)
        read = reader["metadata"][condition]
        read["outcomes"].clear()
        assert set(reader["metadata"][condition]["outcomes"]) == set(tokens)
        with pytest.raises(TypeError):
            reader["metadata"][condition] = {}
    del read
    gc.collect()
    assert not Detail.live, "exclusion eviction retained detailed Python objects"
    service.refresh_competition_cache()
    snapshot = service.refresh_candidates()
    assert [row["selected_direction"]["token_id"] for row in snapshot["candidates"]] == [pair[1] for pair in expected]
    identity = {"market_id": "market-M00", "condition_id": condition, "token_id": tokens[key], "outcome": key.upper()}
    assert service.preview_candidate(identity) == {"state": "rejected", "reason": "candidate_cooling_down"}
    assert backups == []
    gc.collect()
    assert {row["condition"] for row in Detail.live} <= ({condition} if expected else set())


def test_scratch_preserves_datetime_value_without_keeping_a_clock_class():
    from open_trader.polymarket_lp_scratch import LPReadScratch

    class Clock(datetime):
        pass

    value = Clock(2026, 9, 20, 8, 1, 2, 345678, tzinfo=UTC, fold=1)
    cache = LPReadScratch({'market': {'checked_at': value, 'amount': Decimal('1.2300')}})
    saved = cache['market']
    assert saved['checked_at'] == value
    assert saved['checked_at'].fold == value.fold
    assert saved['amount'].as_tuple() == Decimal('1.2300').as_tuple()


def test_queue_build_does_not_expand_all_direction_details(tmp_path, monkeypatch):
    from open_trader import polymarket_lp, polymarket_lp_views

    original_fact = polymarket_lp._lp_direction_fact
    original_trial = polymarket_lp_views.lp_trial_candidates

    def tracked_fact(*args, **kwargs):
        return Detail(original_fact(*args, **kwargs))

    def trial(facts, **kwargs):
        gc.collect()
        assert len(Detail.live) <= 2, 'full universe expanded before filtering'
        return original_trial(facts, **kwargs)

    monkeypatch.setattr(polymarket_lp, '_lp_direction_fact', tracked_fact)
    monkeypatch.setattr(polymarket_lp_views, 'lp_trial_candidates', trial)
    exchange = _LPCandidateQueryExchange(
        datetime(2026, 9, 20, 8, tzinfo=UTC),
        {f'M{i:02}': Decimal('57.6') for i in range(40)},
    )
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: exchange.now)
    service.refresh_price_history()
    service.refresh_competition_cache()
    assert service.refresh_candidates()['funnel']['read'] == 40


def test_candidate_eviction_keeps_active_order_position_and_unknown_action(tmp_path, monkeypatch):
    from test_polymarket_lp import _queue_running_service, _queue_receipt, _queue_runtime_snapshot

    now = datetime(2026, 9, 14, 12, tzinfo=UTC)
    active_condition = '0x' + 'c' * 64
    seed = PredictionArbitrageStore(tmp_path)
    seed.lp_save_price_history(active_condition, '0x' + '1' * 64, [], {
        'state': 'known', 'amplitude': Decimal('0.005'),
        'checked_at': now, 'valid_until': now + timedelta(hours=24),
    })
    store, exchange, service, started = _queue_running_service(tmp_path, now, key='memory-responsibility')
    session_id = started['session_id']
    facts = _LPCandidateQueryExchange(now, {'1': Decimal('100'), '2': Decimal('100')})
    account = facts.lp_account_snapshot()
    account['open_orders'] = [{'order_id': 'order-1', 'condition_id': active_condition, 'token_id': '0x' + '1' * 64, 'side': 'BUY', 'remaining_size': '2000', 'price': '0.30'}]
    account['positions'] = [{'condition_id': 'condition-2', 'token_id': 'token-condition-2', 'size': '1'}]
    for name in ('lp_reward_catalog', 'lp_market_metadata', 'lp_price_history', 'lp_market_competitiveness'):
        monkeypatch.setattr(exchange, name, getattr(facts, name), raising=False)
    def catalog(**kwargs):
        result = facts.lp_reward_catalog(**kwargs)
        result['markets'][0]['condition_id'] = active_condition
        return result

    def competition(**kwargs):
        result = facts.lp_market_competitiveness(**kwargs)
        result['competitiveness'][active_condition] = result['competitiveness'].pop('condition-1')
        return result

    monkeypatch.setattr(exchange, 'lp_reward_catalog', catalog)
    monkeypatch.setattr(exchange, 'lp_market_competitiveness', competition)
    monkeypatch.setattr(exchange, 'lp_account_snapshot', lambda: account, raising=False)
    store.lp_upsert_action(session_id, 'memory-unknown', state='unknown', payload={'role': 'entry', 'order_id': None})
    before_session = store.lp_session(session_id)
    before_actions = store.lp_actions(session_id)
    before_reservations = service._candidate_reservations()
    service.refresh_price_history()
    service.refresh_competition_cache()
    service.refresh_candidates()
    state = service._candidate_queue_state
    assert state['queue_normal'] == state['queue_backup'] == []
    for key in ('directions_by_condition', 'metadata_by_condition', 'reward_market_by_condition'):
        assert state[key] == {}
    assert store.lp_session(session_id) == before_session
    assert store.lp_actions(session_id) == before_actions
    assert service._candidate_reservations() == before_reservations
    assert service._prepared_input_snapshot()['metadata']['condition-2']['condition_id'] == 'condition-2'

    exchange.snapshots = [_queue_runtime_snapshot(now, bid_size='10000', orders=[_queue_receipt('order-1')])]
    assert service.tick()['queue_protection']['state'] == 'monitoring'
    assert store.lp_session(session_id)['state'] != 'complete'
    assert next(row for row in store.lp_actions(session_id) if row['action_key'] == 'memory-unknown')['state'] == 'unknown'
    assert exchange.cancels == []


def test_metadata_retry_does_not_reload_unrequested_reward_details(tmp_path, monkeypatch):
    from lp_memory_replay import MetadataRetryExchange

    class Exchange(MetadataRetryExchange):
        def lp_reward_catalog(self, **kwargs):
            result = super().lp_reward_catalog(**kwargs)
            for row in result['markets']:
                row['detail'] = Detail(condition=row['condition_id'])
            return result

    exchange = Exchange(datetime(2026, 9, 20, 8, tzinfo=UTC), {f'M{i:05}': Decimal(100) for i in range(200)})
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: exchange.now)
    original_publish = service._publish_prepared_inputs

    def publish(*args, **kwargs):
        if exchange.retried:
            gc.collect()
            # Outer preparation may hold its last row; retry needs only M00000.
            assert len(Detail.live) <= 3, 'metadata retry reloaded the whole catalog'
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(service, '_publish_prepared_inputs', publish)
    result = service.refresh_price_history()
    assert exchange.retried
    assert result['state'] == 'known'
    assert result['target_count'] == 200
    assert service.store.lp_preparation_items() == []
