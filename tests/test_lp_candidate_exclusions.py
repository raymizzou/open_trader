"""Cloud candidate cooldowns; all exchange reads are offline."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_polymarket_lp import _LPCandidateQueryExchange


def test_exclusion_deadline_restart_and_conditional_expiry(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path)
    assert store.lp_record_market_exclusion('m', 'yes', 'history_amplitude_exceeded',
        checked_at=now, cooldown_until=now + timedelta(hours=1), now=now)
    identities = (('m', 'yes'), ('m', 'no'), ('other', 'yes'))
    assert store.lp_candidate_allowed(identities, now=now) == identities[1:]
    assert not store.lp_record_market_exclusion('m', 'yes', 'reward_inactive',
        checked_at=now + timedelta(minutes=1), cooldown_until=now + timedelta(hours=2), now=now + timedelta(minutes=1))
    reopened = PredictionArbitrageStore(tmp_path)
    boundary = now + timedelta(hours=1)
    assert reopened.lp_candidate_allowed(identities, now=boundary - timedelta(microseconds=1)) == identities[1:]
    assert reopened.lp_candidate_allowed(identities, now=boundary) == identities
    assert reopened.lp_record_market_exclusion('m', 'yes', 'history_amplitude_exceeded',
        checked_at=boundary, cooldown_until=boundary + timedelta(hours=1), now=boundary)
    assert reopened.lp_prune_market_exclusions(now=boundary, limit=1) == 0
    assert not store.lp_record_market_exclusion('m', 'yes', 'reward_inactive',
        checked_at=now, cooldown_until=boundary, now=boundary)
    assert reopened.lp_candidate_allowed(identities, now=boundary) == identities[1:]
    assert reopened.lp_prune_market_exclusions(now=boundary + timedelta(hours=1), limit=1) == 1
    # More than 999 potential SQLite bindings must be split without loading the table.
    many = tuple((f'm{i}', f't{i}') for i in range(1200))
    assert reopened.lp_candidate_allowed(many, now=boundary) == many

class ExclusionExchange(_LPCandidateQueryExchange):
    def __init__(self, now, pools):
        super().__init__(now, pools)
        self.metadata_reads = []
        self.history_tokens = []
        self.inactive = set()
        self.not_accepting = set()
        self.competition = {}
        self.events = {}
        self.amplitudes = {}
        self.two_sides = False
        self.ended = set()

    def lp_reward_catalog(self, **kwargs):
        result = super().lp_reward_catalog(**kwargs)
        for row in result['markets']:
            row['reward_active'] = row['condition_id'] not in self.inactive
            if row['condition_id'] in self.ended:
                row['closed'] = True
        return result

    def lp_market_metadata(self, condition_ids, **kwargs):
        self.metadata_reads.extend(condition_ids)
        result = super().lp_market_metadata(condition_ids, **kwargs)
        for cid, row in result.items():
            row['exchange_type'] = 'CLOB'
            row['accepting_orders'] = cid not in self.not_accepting
            row.update(self.events.get(cid, {}))
            if self.two_sides:
                row['outcomes']['no'] = {'label': 'NO', 'token_id': f'no-{cid}'}
        return result

    def lp_order_books(self, token_ids, **kwargs):
        result = super().lp_order_books(token_ids, **kwargs)
        for token, row in result.items():
            if token.startswith('no-'):
                row['condition_id'] = token.removeprefix('no-')
        return result

    def lp_market_competitiveness(self, **kwargs):
        result = super().lp_market_competitiveness(**kwargs)
        for cid, value in self.competition.items():
            result['competitiveness'][cid] = (value, self.now)
        return result

    def lp_price_history(self, token_ids, **kwargs):
        self.history_tokens.extend(token_ids)
        result = super().lp_price_history(token_ids, **kwargs)
        for token, rows in result['history'].items():
            if token in self.amplitudes:
                rows[-1]['p'] = self.amplitudes[token]
        return result


def test_reward_rejection_stops_preparation_and_survives_restart(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'bad': Decimal(10), 'good': Decimal(20)})
    exchange.inactive.add('condition-bad')
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    assert service.refresh_price_history()['state'] == 'known'
    assert exchange.metadata_reads == ['condition-good']
    assert exchange.history_tokens == ['token-condition-good']
    exchange.inactive.clear()  # Public pagination cannot wake a still cooling market.
    restarted = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange,
        clock=lambda: exchange.now, exclusions_enabled=True)
    exchange.now += timedelta(minutes=29)
    restarted.refresh_price_history()
    restarted.refresh_competition_cache()
    restarted.refresh_candidates()
    restarted.refresh_candidate_recommendations()
    assert 'condition-bad' not in exchange.metadata_reads
    assert all('token-condition-bad' not in batch for batch in exchange.book_token_reads)
    exchange.now = now + timedelta(minutes=30)
    restarted.refresh_price_history()
    assert exchange.metadata_reads.count('condition-bad') == 1
    assert 'token-condition-bad' in exchange.history_tokens


def test_amplitude_exclusion_is_directional_and_stops_all_preparation_entries(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    exchange.two_sides = True
    exchange.amplitudes['token-condition-m'] = '0.55'
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange,
        clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_competition_cache()
    service.refresh_price_history()
    snapshot = service.refresh_candidates()
    assert len(snapshot['candidates']) == 1
    assert snapshot['candidates'][0]['selected_direction']['token_id'] == 'no-condition-m'
    assert exchange.book_token_reads == (('no-condition-m',),)
    exchange.now += timedelta(seconds=31)
    service.refresh_candidate_recommendations()
    service.refresh_candidates()
    service.refresh_price_history()
    assert all('token-condition-m' not in tokens for tokens in exchange.book_token_reads)
    assert exchange.history_tokens.count('token-condition-m') == 1
    # Existing sampler target state cannot bypass a direction exclusion.
    with service._sample_target_lock:
        service._sample_targets = (('condition-m', 'token-condition-m'), ('condition-m', 'no-condition-m'))
    service.sample_candidate_books()
    assert exchange.book_token_reads[-1] == ('no-condition-m',)


def test_competition_and_events_stop_before_history_and_keep_unknown_out(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {name: Decimal(20) for name in ('zero', 'thin', 'crowded', 'unknown', 'closed', 'event')})
    exchange.competition = {'condition-zero': Decimal(0), 'condition-thin': Decimal('.5'),
        'condition-crowded': Decimal(300), 'condition-unknown': None}
    exchange.not_accepting.add('condition-closed')
    exchange.events['condition-event'] = {'event_ended': False, 'event_start_time': now + timedelta(minutes=20)}
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_competition_cache()
    service.refresh_price_history()
    assert exchange.metadata_reads == ['condition-unknown', 'condition-closed', 'condition-event']
    assert exchange.history_tokens == ['token-condition-unknown']
    snapshot = service.refresh_candidates()
    assert snapshot['candidates'] == []
    assert store.lp_candidate_allowed((('condition-unknown', ''),), now=now) == (('condition-unknown', ''),)
    exchange.now += timedelta(minutes=4)
    service.refresh_price_history()
    assert exchange.metadata_reads.count('condition-closed') == 1
    assert exchange.metadata_reads.count('condition-event') == 1


def test_maintenance_first_rejection_prevents_later_queries(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange,
        clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_competition_cache()
    service.refresh_price_history()
    assert service.refresh_candidates()['candidates']
    books_before, metadata_before = len(exchange.book_token_reads), len(exchange.metadata_reads)
    exchange.inactive.add('condition-m')
    exchange.now += timedelta(seconds=61)
    assert service.refresh_candidate_recommendations()['candidates'] == []
    assert len(exchange.book_token_reads) == books_before
    assert len(exchange.metadata_reads) == metadata_before


def test_official_end_clears_cooldown_but_event_end_and_reward_date_do_not(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    exchange.events['condition-m'] = {'event_ended': True, 'event_finished_at': now - timedelta(minutes=20)}
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_price_history()
    identity = (('condition-m', ''),)
    assert store.lp_candidate_allowed(identity, now=now + timedelta(minutes=39)) == ()
    assert store.lp_candidate_allowed(identity, now=now + timedelta(minutes=40)) == identity
    exchange.ended.add('condition-m')
    exchange.now += timedelta(minutes=1)
    service.refresh_price_history()
    assert store.lp_candidate_allowed(identity, now=exchange.now) == identity
    assert store.lp_market_exclusion_counts(now=exchange.now) == {}


def test_late_candidate_result_cannot_republish_after_cooldown_expiry(tmp_path):
    from threading import Event, Thread
    now = datetime(2026, 10, 5, tzinfo=UTC)
    started, release = Event(), Event()
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange,
        clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_price_history()
    service.refresh_competition_cache()
    assert service.refresh_candidates()['candidates']
    read_books = exchange.lp_order_books
    def delayed_books(tokens, **kwargs):
        result = read_books(tokens, **kwargs)
        started.set()
        assert release.wait(5), 'independent scheduling watchdog'
        return result
    exchange.lp_order_books = delayed_books
    result, errors = [], []
    def scan():
        try:
            result.append(service.refresh_candidate_recommendations())
        except Exception as exc:
            errors.append(exc)
    # Maintenance copies qualification facts before the read. Unlike the scan
    # queue, those copies cannot disappear when a concurrent exclusion evicts it.
    exchange.now += timedelta(seconds=31)
    worker = Thread(target=scan)
    worker.start()
    try:
        assert started.wait(5)
        exchange.now += timedelta(seconds=1)
        exchange.events['condition-m'] = {
            'event_ended': True, 'event_finished_at': exchange.now - timedelta(hours=1) + timedelta(seconds=1)}
        service.refresh_price_history()  # Recovery deadline is one second away.
        assert service.store.lp_market_exclusion_counts(now=exchange.now) == {'event_recovery_pending': 1}
        exchange.events.clear()
        exchange.now += timedelta(seconds=2)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert result[0]['candidates'] == []
    assert service.candidate_snapshot()['candidates'] == []


def test_both_directions_cooling_skip_shared_metadata_across_restart(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    exchange.two_sides = True
    exchange.amplitudes = {'token-condition-m': '.55', 'no-condition-m': '.55'}
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_price_history()
    assert store.lp_market_exclusion_counts(now=now) == {'history_amplitude_exceeded': 2}
    exchange.now += timedelta(minutes=1)
    restarted = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange,
        clock=lambda: exchange.now, exclusions_enabled=True)
    restarted.refresh_price_history()
    restarted.refresh_competition_cache()
    assert restarted.refresh_candidates()['candidates'] == []
    assert exchange.metadata_reads == ['condition-m']
    assert exchange.history_calls == 1
    exchange.now = now + timedelta(hours=1)
    exchange.amplitudes.clear()
    restarted.refresh_price_history()
    assert exchange.metadata_reads == ['condition-m', 'condition-m']
    assert exchange.history_calls == 2  # A prior bad summary does not extend cooldown.
    assert store.lp_market_exclusion_counts(now=exchange.now) == {}


def test_candidate_exclusions_default_off_and_keep_order_management(tmp_path, monkeypatch):
    from test_lp_order_management import _RegistrationExchange, _sync_snapshot, _generation_snapshot, NOW
    monkeypatch.delenv('OPEN_TRADER_LP_CANDIDATE_EXCLUSIONS', raising=False)
    store = PredictionArbitrageStore(tmp_path)
    store.lp_record_market_exclusion('condition-1', '', 'reward_inactive', checked_at=NOW,
        cooldown_until=NOW + timedelta(minutes=30), now=NOW)
    exchange = _RegistrationExchange()
    service = PolymarketLPService(store, exchange, clock=lambda: NOW, exclusions_enabled=True)
    snapshot = _sync_snapshot()
    assert service.register_account_snapshot(_generation_snapshot(store, snapshot))['state'] == 'registered'
    assert len(store.lp_active_sessions()) == 1
    assert exchange.book_reads == 1
    disabled_exchange = ExclusionExchange(NOW, {'m': Decimal(20)})
    disabled_exchange.inactive.add('condition-m')
    disabled = PolymarketLPService(PredictionArbitrageStore(tmp_path / 'disabled'), disabled_exchange, clock=lambda: NOW)
    assert disabled.exclusions_enabled is False
    disabled.refresh_price_history()
    assert disabled_exchange.metadata_reads == ['condition-m']
    assert disabled.store.lp_market_exclusion_counts(now=NOW) == {}
    monkeypatch.setenv('OPEN_TRADER_LP_CANDIDATE_EXCLUSIONS', '1')
    enabled = PolymarketLPService(disabled.store, disabled_exchange, clock=lambda: NOW)
    assert enabled.exclusions_enabled is True


def test_metadata_warm_restore_filters_exclusions_without_blocking_risk_reads(tmp_path):
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
    from test_polymarket_trading import _LpMetadataProbe, _lp_cache_market, SIGNER, WALLET
    now = datetime.now(UTC)
    store = PredictionArbitrageStore(tmp_path)
    blocked, good = '0x' + 'a' * 64, '0x' + 'b' * 64
    probe = _LpMetadataProbe()
    probe.market_rows[blocked] = _lp_cache_market(blocked, slug='blocked')
    first = PolymarketTradingClient(TradingConfig(SIGNER, WALLET), object(),
        metadata_cache=store, public_client_factory=probe.public_client_factory())
    payload = first.lp_market_metadata((blocked,))[blocked]
    store.lp_metadata_cache_store_entries({good: ((now + timedelta(hours=1)).timestamp(), {**payload, 'condition_id': good})})
    store.lp_record_market_exclusion(blocked, '', 'reward_inactive', checked_at=now,
        cooldown_until=now + timedelta(minutes=30), now=now)
    rebuilt = PolymarketTradingClient(TradingConfig(SIGNER, WALLET), object(),
        metadata_cache=store, public_client_factory=probe.public_client_factory())
    rebuilt.configure_lp_candidate_exclusions(True)
    rebuilt.lp_market_metadata((good,))
    assert set(rebuilt._metadata_entries) == {good}
    # The shared adapter still reads an excluded market for active-order management.
    assert rebuilt.lp_market_metadata((blocked,))[blocked]['condition_id'] == blocked
    assert set(rebuilt._metadata_entries) == {good}, 'risk response cannot rewarm excluded candidate details'


def test_market_and_direction_deadlines_remain_independent(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path)
    store.lp_record_market_exclusion('m', 'yes', 'history_amplitude_exceeded', checked_at=now,
        cooldown_until=now + timedelta(hours=1), now=now)
    store.lp_record_market_exclusion('m', '', 'market_not_accepting_orders', checked_at=now,
        cooldown_until=now + timedelta(minutes=5), now=now)
    sides = (('m', 'yes'), ('m', 'no'))
    assert store.lp_candidate_allowed(sides, now=now) == ()
    assert store.lp_candidate_allowed(sides, now=now + timedelta(minutes=5)) == (('m', 'no'),)
    assert store.lp_candidate_allowed(sides, now=now + timedelta(hours=1)) == sides
    assert store.lp_prune_market_exclusions(now=now + timedelta(minutes=5)) == 1
    assert store.lp_market_exclusion_counts(now=now + timedelta(minutes=5)) == {'history_amplitude_exceeded': 1}


def test_preparation_unknown_responses_never_enter_exclusions(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    for status in (None, 400, 429):
        exchange = ExclusionExchange(now, {'m': Decimal(20)})
        def failed_metadata(ids, *, stop_event=None):
            return {'state': 'unknown', 'markets': {}, 'failed_ids': {
                cid: {'error_type': 'ReadTimeout', 'status': status} for cid in ids}}
        exchange.lp_market_metadata_batch = failed_metadata
        store = PredictionArbitrageStore(tmp_path / str(status))
        service = PolymarketLPService(store, exchange, clock=lambda: now, exclusions_enabled=True)
        result = service.refresh_price_history()
        assert result['state'] != 'known'
        assert store.lp_market_exclusion_counts(now=now) == {}
        assert exchange.history_tokens == []


def test_late_metadata_cannot_recreate_exclusion_after_official_clear(tmp_path):
    from threading import Event, Thread
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    exchange.two_sides = True
    exchange.amplitudes['token-condition-m'] = '.55'
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_price_history()
    started, release = Event(), Event()
    metadata = exchange.lp_market_metadata
    def delayed_metadata(ids, **kwargs):
        rows = metadata(ids, **kwargs)
        for row in rows.values():
            row['accepting_orders'] = False
        started.set()
        assert release.wait(5)
        return rows
    exchange.lp_market_metadata = delayed_metadata
    result = []
    worker = Thread(target=lambda: result.append(service.refresh_price_history()))
    worker.start()
    try:
        assert started.wait(5)
        exchange.now += timedelta(seconds=1)
        competition = exchange.lp_market_competitiveness
        def terminal_page(**kwargs):
            rows = competition(**kwargs)
            rows['ended_condition_ids'] = ('condition-m',)
            return rows
        exchange.lp_market_competitiveness = terminal_page
        service.refresh_competition_cache()
        assert store.lp_market_exclusion_counts(now=exchange.now) == {}
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert result[0]['preparation_outcome'] == 'superseded'
    assert store.lp_market_exclusion_counts(now=exchange.now) == {}


def test_fixed_offline_load_reduces_requests_and_candidate_cache_rows(tmp_path):
    """Sixteen fixed markets, two rounds, no credentials/network/RSS claim."""
    now = datetime(2026, 10, 5, tzinfo=UTC)
    measurements = {}
    for enabled in (False, True):
        exchange = ExclusionExchange(now, {f'M{i:02}': Decimal(20) for i in range(16)})
        exchange.competition = {f'condition-M{i:02}': Decimal(0) for i in range(15)}
        service = PolymarketLPService(PredictionArbitrageStore(tmp_path / str(enabled)), exchange,
            clock=lambda: exchange.now, exclusions_enabled=enabled)
        service.refresh_competition_cache()
        for _ in range(2):
            service.refresh_price_history()
            service.refresh_candidates()
            exchange.now += timedelta(seconds=31)
        measurements[enabled] = {
            'metadata_id_reads': len(exchange.metadata_reads),
            'history_token_reads': len(exchange.history_tokens),
            'book_token_reads': sum(map(len, exchange.book_token_reads)),
            'prepared_metadata_rows': len(service._prepared_input_snapshot()['metadata']),
            'pool_rows': len(service._candidate_pool),
        }
    assert measurements == {
        False: {'metadata_id_reads': 32, 'history_token_reads': 16, 'book_token_reads': 2, 'prepared_metadata_rows': 16, 'pool_rows': 1},
        True: {'metadata_id_reads': 2, 'history_token_reads': 1, 'book_token_reads': 2, 'prepared_metadata_rows': 1, 'pool_rows': 1},
    }
    print('offline fixed load:', measurements)


def test_public_terminal_flags_are_preserved_without_date_inference(tmp_path):
    import json
    from polymarket.models.gamma.market import Market
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
    from test_polymarket_trading import _LpMetadataProbe, _lp_cache_market, SIGNER, WALLET
    now = datetime.now(UTC)
    blocked, still_unknown = '0x' + 'a' * 64, '0x' + 'b' * 64
    probe = _LpMetadataProbe()
    probe.market_rows[blocked] = Market.parse_response({
        'id': 'closed', 'conditionId': blocked, 'closed': True, 'acceptingOrders': False,
        'question': 'Closed market', 'outcomes': '["Yes", "No"]', 'clobTokenIds': '["y", "n"]', 'events': []})
    probe.market_rows[still_unknown] = _lp_cache_market(still_unknown, slug='past-end-date')
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({'data': [
                {'condition_id': blocked, 'market_competitiveness': 0, 'closed': True},
                {'condition_id': still_unknown, 'market_competitiveness': 0, 'event_ended': True, 'end_date': '2020-01-01'},
            ], 'next_cursor': 'LTE='}).encode()
    store = PredictionArbitrageStore(tmp_path)
    adapter = PolymarketTradingClient(TradingConfig(SIGNER, WALLET), object(),
        metadata_cache=store, public_client_factory=probe.public_client_factory(),
        urlopen_fn=lambda *args, **kwargs: Response())
    metadata = adapter.lp_market_metadata((blocked, still_unknown))
    assert metadata[blocked]['closed'] is True
    assert metadata[still_unknown]['closed'] is not True
    for cid in (blocked, still_unknown):
        store.lp_record_market_exclusion(cid, '', 'reward_inactive', checked_at=now,
            cooldown_until=now + timedelta(minutes=30), now=now)
    service = PolymarketLPService(store, adapter, exclusions_enabled=True)
    result = service.refresh_competition_cache()
    assert result['state'] == 'known'
    assert store.lp_candidate_allowed(((blocked, ''), (still_unknown, '')), now=datetime.now(UTC)) == ((blocked, ''),)


def test_candidate_preview_cannot_query_cooled_sides_or_continue_after_reward_rejection(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    exchange.two_sides = True
    exchange.amplitudes['token-condition-m'] = '.55'
    exchange.lp_market_metadata_fresh = exchange.lp_market_metadata
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange,
        clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_competition_cache()
    service.refresh_price_history()
    identity = {'market_id': 'market-m', 'condition_id': 'condition-m', 'token_id': 'token-condition-m', 'outcome': 'YES'}
    reads = len(exchange.metadata_reads), len(exchange.book_token_reads)
    assert service.preview_candidate(identity) == {'state': 'rejected', 'reason': 'candidate_cooling_down'}
    assert (len(exchange.metadata_reads), len(exchange.book_token_reads)) == reads
    other = {**identity, 'token_id': 'no-condition-m', 'outcome': 'NO'}
    preview = service.preview_candidate(other)
    assert preview['state'] == 'previewed', preview
    assert exchange.book_token_reads[-1] == ('no-condition-m',)
    exchange.inactive.add('condition-m')
    reads = len(exchange.metadata_reads), len(exchange.book_token_reads)
    assert service.preview_candidate(other) == {'state': 'rejected', 'reason': 'reward_inactive'}
    assert (len(exchange.metadata_reads), len(exchange.book_token_reads)) == reads
    assert service.start(preview_id=preview['preview_id'], idempotency_key='cooled-preview') == {
        'state': 'rejected', 'reason': 'candidate_cooling_down'}
    assert store.lp_active_sessions() == []


@pytest.mark.parametrize('insert_first', [True, False])
def test_expiry_cleanup_and_reinsert_are_safe_across_connections(tmp_path, insert_first):
    from threading import Event, Thread
    now = datetime(2026, 10, 5, tzinfo=UTC)
    boundary = now + timedelta(minutes=5)
    first, second = PredictionArbitrageStore(tmp_path), PredictionArbitrageStore(tmp_path)
    first.lp_record_market_exclusion('m', '', 'market_not_accepting_orders', checked_at=now,
        cooldown_until=boundary, now=now)
    done = Event()
    result, errors = {}, []
    def insert():
        try:
            if not insert_first:
                assert done.wait(5)
            result['inserted'] = second.lp_record_market_exclusion('m', '', 'reward_inactive', checked_at=boundary,
                cooldown_until=boundary + timedelta(minutes=30), now=boundary)
            if insert_first:
                done.set()
        except Exception as exc:
            errors.append(exc)
    def prune():
        try:
            if insert_first:
                assert done.wait(5)
            result['deleted'] = first.lp_prune_market_exclusions(now=boundary)
            if not insert_first:
                done.set()
        except Exception as exc:
            errors.append(exc)
    workers = [Thread(target=insert), Thread(target=prune)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert not errors and all(not worker.is_alive() for worker in workers)
    assert result == {'inserted': True, 'deleted': 0 if insert_first else 1}
    assert first.lp_candidate_allowed((('m', 'any-token'),), now=boundary) == ()


@pytest.mark.parametrize('unqualified', ['funds', 'participation', 'history_missing', 'book_timeout'])
def test_nonmarket_admission_failures_and_unknown_are_not_blacklisted(tmp_path, unqualified):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    if unqualified == 'funds':
        exchange.available = Decimal(0)
    elif unqualified == 'participation':
        account = exchange.lp_account_snapshot
        def occupied_account():
            snapshot = account()
            snapshot['positions'] = [{'condition_id': 'condition-m', 'token_id': 'token-condition-m', 'size': Decimal(10)}]
            return snapshot
        exchange.lp_account_snapshot = occupied_account
    elif unqualified == 'history_missing':
        exchange.lp_price_history = lambda *args, **kwargs: {'state': 'unknown', 'history': {}, 'unknown_token_ids': ['token-condition-m']}
    else:
        def timed_out(*args, **kwargs):
            raise TimeoutError('offline timeout')
        exchange.lp_order_books = timed_out
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_competition_cache()
    service.refresh_price_history()
    assert service.refresh_candidates()['candidates'] == []
    assert store.lp_market_exclusion_counts(now=now) == {}


@pytest.mark.parametrize('enabled', [True, False])
def test_history_scheduler_returns_cooled_markets_to_normal_preparation_at_deadline(tmp_path, enabled):
    from threading import Event
    from open_trader.prediction_runtime import PredictionRuntime
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    exchange.not_accepting.add('condition-m')
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path / 'store'), exchange,
        clock=lambda: exchange.now, exclusions_enabled=enabled)
    waits, stopped = [], Event()
    def wait_for_next(stop_event, seconds):
        waits.append(seconds)
        if seconds >= 3600 or stop_event.is_set():
            stopped.set()
            return True
        exchange.not_accepting.clear()
        exchange.now += timedelta(seconds=seconds)
        return False
    runtime = PredictionRuntime(data_dir=tmp_path / 'offline-runtime',
        prediction_config_path=tmp_path / 'unused.json', dashboard_url='http://127.0.0.1/',
        n_leg_paused=True, history_clock=lambda: exchange.now, history_wait=wait_for_next)
    runtime.lp = service
    runtime._start_history_monitor(data_only=True)
    try:
        assert stopped.wait(5), 'independent scheduling watchdog'
    finally:
        runtime._history_stop_event.set()
        runtime._history_wakeup_event.set()
        runtime._history_thread.join(5)
    assert not runtime._history_thread.is_alive()
    assert waits == ([300, 3600] if enabled else [3600])
    assert exchange.metadata_reads == (['condition-m', 'condition-m'] if enabled else ['condition-m'])
    assert exchange.history_tokens == (['token-condition-m'] if enabled else [])


def test_new_maintenance_cooldown_shortens_already_sleeping_history_schedule(tmp_path, monkeypatch):
    from threading import Event
    from types import SimpleNamespace
    import time
    import open_trader.prediction_runtime as runtime_module
    now = datetime(2026, 10, 5, tzinfo=UTC)
    elapsed = [0.0]
    # Patch this module's clock object, never the shared time.monotonic function.
    monkeypatch.setattr(runtime_module, 'time', SimpleNamespace(monotonic=lambda: elapsed[0]))
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    recovered = Event()
    metadata_reader = exchange.lp_market_metadata
    def metadata(ids, **kwargs):
        rows = metadata_reader(ids, **kwargs)
        if elapsed[0] >= 362:
            recovered.set()
        return rows
    exchange.lp_market_metadata = metadata
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path / 'store'), exchange,
        clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_competition_cache()
    runtime = runtime_module.PredictionRuntime(data_dir=tmp_path / 'offline-runtime',
        prediction_config_path=tmp_path / 'unused.json', dashboard_url='http://127.0.0.1/', n_leg_paused=True)
    runtime.lp = service
    runtime._start_history_monitor(data_only=True)
    try:
        assert runtime._history_initial_done.wait(5)
        assert service.refresh_candidates()['candidates']
        elapsed[0] = 61
        exchange.now = now + timedelta(seconds=61)
        exchange.not_accepting.add('condition-m')
        assert service.refresh_candidate_recommendations()['candidates'] == []
        exchange.not_accepting.clear()
        elapsed[0] = 362
        exchange.now = now + timedelta(seconds=362)
        assert recovered.wait(5), 'the original hourly deadline must not retain a 5-minute exclusion'
    finally:
        runtime._history_stop_event.set()
        runtime._history_wakeup_event.set()
        runtime._history_thread.join(5)
    assert not runtime._history_thread.is_alive()


def test_old_history_tokens_do_not_wake_current_cooled_metadata(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    store = PredictionArbitrageStore(tmp_path)
    store.lp_save_price_history('m', 'obsolete', [], {'state': 'known', 'checked_at': now})
    metadata = {'condition_id': 'm', 'outcomes': {
        'yes': {'token_id': 'yes'}, 'no': {'token_id': 'no'}}}
    store.lp_metadata_cache_store_entries({'m': ((now + timedelta(hours=12)).timestamp(), metadata)})
    for token in ('yes', 'no'):
        store.lp_save_price_history('m', token, [], {'state': 'known', 'checked_at': now})
        store.lp_record_market_exclusion('m', token, 'history_amplitude_exceeded', checked_at=now,
            cooldown_until=now + timedelta(hours=1), now=now)
    assert store.lp_candidate_conditions(('m',), now=now) == ()
    assert list(store.lp_metadata_cache_items(now=now, exclude_candidates=True)) == []
    assert store.lp_candidate_conditions(('m',), now=now + timedelta(hours=1)) == ('m',)


def test_excluding_cached_queue_representative_keeps_opposite_and_drops_old_identity(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    exchange.two_sides = True
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_competition_cache()
    service.refresh_price_history()
    assert service.refresh_candidates()['candidates'][0]['selected_direction']['token_id'] == 'no-condition-m'
    store.lp_save_price_history('condition-m', 'no-condition-m', [], {
        'state': 'known', 'checked_at': now, 'amplitude': '.05', 'valid_until': now + timedelta(hours=24)})
    snapshot = service.refresh_candidates()
    assert snapshot['candidates'][0]['selected_direction']['token_id'] == 'token-condition-m'
    assert snapshot['candidates'][0]['token_id'] == 'token-condition-m'
    assert exchange.book_token_reads[-1] == ('token-condition-m',)


def test_preparation_rejection_keeps_raw_metadata_and_rechecks_at_deadline(tmp_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    exchange.two_sides = True
    exchange.not_accepting.add('condition-m')
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange,
        clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_competition_cache()
    assert service.refresh_price_history()['state'] == 'known'
    captured = service._prepared_input_snapshot()
    assert captured['metadata']['condition-m']['accepting_orders'] is False
    assert set(captured['metadata']['condition-m']['outcomes']) == {'yes', 'no'}
    assert service.refresh_candidates()['candidates'] == []
    exchange.not_accepting.clear()
    exchange.now = now + timedelta(seconds=299)
    assert service.refresh_price_history()['state'] == 'known'
    assert exchange.metadata_reads == ['condition-m']
    assert service.candidate_snapshot()['candidates'] == []
    exchange.now = now + timedelta(seconds=300)
    assert service.refresh_price_history()['state'] == 'known'
    assert exchange.metadata_reads == ['condition-m', 'condition-m']
    assert captured['metadata']['condition-m']['accepting_orders'] is False
    assert service._prepared_input_snapshot()['metadata']['condition-m']['accepting_orders'] is True
    assert service.refresh_competition_cache()['state'] == 'known'
    assert service.refresh_candidates()['candidates'][0]['condition_id'] == 'condition-m'
    assert service.store.lp_market_exclusion_counts(now=exchange.now) == {}


def test_direction_exclusion_filters_display_and_auto_pool_without_copy(tmp_path, monkeypatch):
    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    from open_trader.polymarket_lp_scratch import LPReadScratch
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    exchange.two_sides = True
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_competition_cache()
    service.refresh_price_history()
    row = service.refresh_candidates()['candidates'][0]
    assert row['selected_direction']['token_id'] == 'no-condition-m'
    assert set(row['directions']) == {'YES', 'NO'}
    backups = []
    backup = LPReadScratch.__deepcopy__
    def counted_backup(scratch, memo):
        backups.append(len(scratch))
        return backup(scratch, memo)
    monkeypatch.setattr(LPReadScratch, '__deepcopy__', counted_backup)
    assert service._exclude_candidate('condition-m', 'token-condition-m', 'history_amplitude_exceeded', checked_at=now)
    displayed = service.candidate_snapshot()['candidates'][0]
    assert set(displayed['directions']) == {'NO'}
    assert displayed['selected_direction']['token_id'] == 'no-condition-m'
    execution = PredictionExecutionService(store=store, monitor=object(), trading=exchange,
        notifier=object(), lock_path=tmp_path / "execution.lock", lp=service)
    auto = execution._auto_pool
    assert [row['token_id'] for row in auto.candidates()] == ['no-condition-m']
    assert backups == []


def test_concurrent_direction_exclusions_keep_raw_reader_and_fixed_deadlines(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from open_trader.polymarket_lp_scratch import LPReadScratch
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20), 'other': Decimal(30)})
    exchange.two_sides = True
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange,
        clock=lambda: exchange.now, exclusions_enabled=True)
    service.refresh_competition_cache()
    service.refresh_price_history()
    assert len(service.refresh_candidates()['candidates']) == 2
    captured = service._prepared_input_snapshot()
    version = service._prepared_inputs_version
    backups = []
    backup = LPReadScratch.__deepcopy__
    def counted_backup(scratch, memo):
        backups.append(len(scratch))
        return backup(scratch, memo)
    monkeypatch.setattr(LPReadScratch, '__deepcopy__', counted_backup)
    ready = Barrier(2)
    def exclude(token):
        ready.wait(timeout=5)
        return service._exclude_candidate('condition-m', token, 'history_amplitude_exceeded', checked_at=now)
    tokens = ('token-condition-m', 'no-condition-m')
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(exclude, token) for token in tokens]
        assert all(result.result(timeout=5) for result in results)
    assert backups == []
    assert service._prepared_inputs_version == version
    assert set(captured['metadata']['condition-m']['outcomes']) == {'yes', 'no'}
    assert set(service._prepared_input_snapshot()['metadata']['condition-m']['outcomes']) == {'yes', 'no'}
    assert [row['condition_id'] for row in service.candidate_snapshot()['candidates']] == ['condition-other']
    exchange.now += timedelta(minutes=1)
    for token in tokens:
        assert not service._exclude_candidate('condition-m', token, 'history_amplitude_exceeded', checked_at=exchange.now)
    restarted = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange,
        clock=lambda: exchange.now, exclusions_enabled=True)
    assert [row['condition_id'] for row in restarted.candidate_snapshot()['candidates']] == ['condition-other']
    assert restarted.store.lp_candidate_allowed(tuple(('condition-m', token) for token in tokens),
        now=now + timedelta(hours=1) - timedelta(microseconds=1)) == ()
    exchange.now = now + timedelta(hours=1)
    assert restarted.refresh_price_history()['state'] == 'known'
    assert restarted.refresh_competition_cache()['state'] == 'known'
    assert {row['condition_id'] for row in restarted.refresh_candidates()['candidates']} == {'condition-m', 'condition-other'}


@pytest.mark.parametrize('fresh', ['eligible', 'rejected', 'unknown'])
@pytest.mark.parametrize('restart', [False, True])
def test_expired_event_exclusion_revalidates_before_reservation_rebuild_can_recool(tmp_path, monkeypatch, fresh, restart):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    exchange.events['condition-m'] = {'event_ended': False, 'event_start_time': now + timedelta(minutes=31)}
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    assert service.refresh_competition_cache()['state'] == 'known'
    assert service.refresh_price_history()['state'] == 'known'
    assert exchange.metadata_reads == ['condition-m']
    store.lp_create_session('other', 'other', state='entry_open', payload={
        'condition_id': 'condition-other', 'token_id': 'token-other', 'price': '.1', 'quantity': '1'})
    exchange.now = now + timedelta(minutes=1)
    assert service.refresh_candidates()['candidates'] == []
    assert store.lp_market_exclusion_counts(now=exchange.now) == {'event_starting_soon': 1}
    assert store.lp_next_market_exclusion_expiry(now=exchange.now) == now + timedelta(minutes=6)
    exchange.now = now + timedelta(minutes=2)
    assert service.refresh_candidates()['candidates'] == []
    exchange.events['condition-m']['event_start_time'] = now + timedelta(hours=2)
    exchange.now = now + timedelta(minutes=6)
    store.lp_update_session('other', state='complete')
    if restart:
        store = PredictionArbitrageStore(tmp_path)
        service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    from open_trader.polymarket_lp_scratch import LPReadScratch
    backups = []
    backup = LPReadScratch.__deepcopy__
    def counted_backup(scratch, memo):
        backups.append(len(scratch))
        return backup(scratch, memo)
    monkeypatch.setattr(LPReadScratch, '__deepcopy__', counted_backup)
    # Public scan runs before preparation after the unrelated reservation changes.
    assert service.refresh_candidates()['candidates'] == []
    assert store.lp_market_exclusion_counts(now=exchange.now) == {}
    assert exchange.metadata_reads == ['condition-m']
    assert backups == []
    monkeypatch.setattr(LPReadScratch, '__deepcopy__', backup)
    if fresh == 'rejected':
        exchange.events['condition-m']['event_start_time'] = now + timedelta(minutes=20)
    elif fresh == 'unknown':
        metadata = exchange.lp_market_metadata
        def failed_metadata(ids, **kwargs):
            metadata(ids, **kwargs)  # Record the attempted public read.
            raise TimeoutError('offline metadata timeout')
        exchange.lp_market_metadata = failed_metadata
    prepared = service.refresh_price_history()
    assert exchange.metadata_reads == ['condition-m', 'condition-m']
    assert service.refresh_competition_cache()['state'] == 'known'
    candidates = service.refresh_candidates()['candidates']
    if fresh == 'eligible':
        assert prepared['state'] == 'known'
        assert [row['condition_id'] for row in candidates] == ['condition-m']
        assert store.lp_market_exclusion_counts(now=exchange.now) == {}
    elif fresh == 'rejected':
        assert candidates == []
        assert store.lp_market_exclusion_counts(now=exchange.now) == {'event_starting_soon': 1}
        assert store.lp_next_market_exclusion_expiry(now=exchange.now) == now + timedelta(minutes=11)
    else:
        assert prepared['state'] != 'known'
        assert candidates == []
        assert store.lp_market_exclusion_counts(now=exchange.now) == {}


@pytest.mark.parametrize('receipt', ['expired', 'missing', 'future'])
@pytest.mark.parametrize('reason', ['event', 'accepting_orders'])
def test_candidate_preview_invalid_metadata_receipt_stays_unknown_without_cooldown(tmp_path, receipt, reason):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    if reason == 'event':
        exchange.events['condition-m'] = {'event_ended': False, 'event_start_time': now + timedelta(minutes=20)}
    else:
        exchange.not_accepting.add('condition-m')
    metadata = exchange.lp_market_metadata
    stamps = {'expired': now - timedelta(seconds=61), 'missing': None, 'future': now + timedelta(seconds=1)}
    def stale_metadata(ids, **kwargs):
        rows = metadata(ids, **kwargs)
        for row in rows.values():
            row['metadata_checked_at'] = stamps[receipt]
        return rows
    exchange.lp_market_metadata_fresh = stale_metadata
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    identity = {'market_id': 'market-m', 'condition_id': 'condition-m', 'token_id': 'token-condition-m', 'outcome': 'YES'}
    assert service.preview_candidate(identity) == {'state': 'rejected', 'reason': (
        'market_metadata_time_unknown' if receipt == 'missing' else 'market_metadata_stale')}
    assert store.lp_market_exclusion_counts(now=now) == {}
    assert store.lp_active_sessions() == []


@pytest.mark.parametrize('delay_stage', ['metadata', 'competition_lookup'])
def test_delayed_preparation_metadata_cannot_recool_after_receipt_expires(tmp_path, delay_stage):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    now = datetime(2026, 10, 5, tzinfo=UTC)
    exchange = ExclusionExchange(now, {'m': Decimal(20)})
    exchange.events['condition-m'] = {'event_ended': False, 'event_start_time': now + timedelta(minutes=20)}
    entered, release, metadata_seen = Event(), Event(), Event()
    metadata = exchange.lp_market_metadata
    def delayed_metadata(ids, **kwargs):
        rows = metadata(ids, **kwargs)
        metadata_seen.set()
        if delay_stage == 'metadata':
            entered.set()
            assert release.wait(5), 'independent metadata-read watchdog'
        return rows
    exchange.lp_market_metadata = delayed_metadata
    store = PredictionArbitrageStore(tmp_path)
    competition = store.lp_competitiveness_map
    def delayed_competition(*, condition_ids=None, connection=None):
        row = competition(condition_ids=condition_ids, connection=connection)
        if delay_stage == 'competition_lookup' and metadata_seen.is_set():
            entered.set()
            assert release.wait(5), 'independent competition-read watchdog'
        return row
    store.lp_competitiveness_map = delayed_competition
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now, exclusions_enabled=True)
    with ThreadPoolExecutor(1) as workers:
        job = workers.submit(service.refresh_price_history)
        try:
            assert entered.wait(5), 'preparation did not reach metadata'
            exchange.now += timedelta(seconds=61)
        finally:
            release.set()
        job.result(timeout=5)
    assert store.lp_market_exclusion_counts(now=exchange.now) == {}
    assert exchange.metadata_reads == ['condition-m']
    assert service.refresh_candidates()['candidates'] == []
