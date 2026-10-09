"""Issue #306: committed batches, page recovery and stable ranking projections."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event

import pytest

from open_trader import polymarket_lp
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_lp_competition_memory import NOW
from test_polymarket_trading import CompetitionOpener, make_competition_adapter


def test_native_commits_first_page_before_reading_next(tmp_path, monkeypatch):
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, make_competition_adapter(CompetitionOpener()), clock=lambda: NOW)
    opener = service.exchange._urlopen_fn
    def inspect_page(request, **kwargs):
        if 'next_cursor=' in request.full_url:
            assert store.lp_competitiveness_entry('condition-a')[0] == Decimal('16.6')
            assert service._competition_entries(('condition-a',))['condition-a']['value'] == Decimal('16.6')
        return opener(request, **kwargs)
    monkeypatch.setattr(service.exchange, '_urlopen_fn', inspect_page)
    result = service.refresh_competition_cache()
    assert result['state'] == 'known', 'page one was not committed before page two'
    assert set(store.lp_competitiveness_map()) == {'condition-a', 'condition-b', 'condition-c'}


def test_failed_later_batch_preserves_committed_facts_and_page_cursor(tmp_path, monkeypatch):
    store = PredictionArbitrageStore(tmp_path)
    store.lp_competitiveness_upsert([('condition-b', Decimal('8'), NOW)])
    service = PolymarketLPService(store, make_competition_adapter(CompetitionOpener()), clock=lambda: NOW)
    monkeypatch.setattr(polymarket_lp, '_LP_COMPETITION_BATCH_SIZE', 1, raising=False)
    original = store.lp_competitiveness_upsert
    def fail_b(entries, **kwargs):
        entries = list(entries)
        if any(cid == 'condition-b' for cid, _, _ in entries):
            raise RuntimeError('failed second batch')
        return original(entries, **kwargs)
    monkeypatch.setattr(store, 'lp_competitiveness_upsert', fail_b)
    service.refresh_competition_cache()
    assert store.lp_competitiveness_entry('condition-a')[0] == Decimal('16.6')
    assert store.lp_competitiveness_entry('condition-b') == (Decimal('8'), NOW)
    assert service._competition_entries(('condition-a', 'condition-b'))['condition-b']['value'] == Decimal('8')
    assert store.lp_competitiveness_progress()['next_start_cursor'] is None
    monkeypatch.setattr(store, 'lp_competitiveness_upsert', original)
    restarted = PolymarketLPService(store, make_competition_adapter(CompetitionOpener()), clock=lambda: NOW)
    assert restarted.refresh_competition_cache()['state'] == 'known'
    assert restarted.exchange._urlopen_fn.calls[0] is None
    assert store.lp_competitiveness_count() == 3


def test_store_cancel_before_commit_rolls_back_facts_and_progress(tmp_path, monkeypatch):
    store = PredictionArbitrageStore(tmp_path)
    store.lp_competitiveness_upsert([('a', Decimal('8'), NOW)])
    stop = Event()
    original = store._connection
    def connection():
        conn = original()
        def trace(sql):
            if sql.lstrip().startswith('INSERT OR REPLACE INTO lp_market_competitiveness'):
                stop.set()
        conn.set_trace_callback(trace)
        return conn
    monkeypatch.setattr(store, '_connection', connection)
    with pytest.raises(RuntimeError, match='competition_cancelled'):
        store.lp_competitiveness_upsert([('a', Decimal('2'), NOW)],
            progress={'next_start_cursor': 'page-two'}, stop_event=stop)
    assert store.lp_competitiveness_entry('a') == (Decimal('8'), NOW)
    assert store.lp_competitiveness_progress() == {}


def test_fallback_and_cache_projection_do_not_mix_one_committed_batch(tmp_path, monkeypatch):
    store = PredictionArbitrageStore(tmp_path)
    store.lp_competitiveness_upsert([('b', Decimal('1'), NOW)])
    service = PolymarketLPService(store, object(), clock=lambda: NOW)
    service._competition_state = {'round_checked_at': NOW, 'competitiveness': {'a': (Decimal('1'), NOW)}}
    entered, release, attempted = Event(), Event(), Event()
    read = store.lp_competitiveness_map
    def blocked(*, condition_ids=None, connection=None):
        entered.set()
        assert release.wait(10)
        return read(condition_ids=condition_ids, connection=connection)
    monkeypatch.setattr(store, 'lp_competitiveness_map', blocked)
    def publish():
        attempted.set()
        service._publish_competition_batch({'a': (Decimal('2'), NOW), 'b': (Decimal('2'), NOW)},
            page_cursor=None, resume_cursor=None, checked_at=NOW, stop_event=None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        projection = pool.submit(service._competition_entries, ('a', 'b'))
        assert entered.wait(10)
        publication = pool.submit(publish)
        assert attempted.wait(10)
        try:
            publication.result(timeout=10)
        finally:
            release.set()
        values = projection.result(timeout=10)
    assert [values[cid]['value'] for cid in ('a', 'b')] == [Decimal('1'), Decimal('1')]
    assert [service._competition_entries(('a', 'b'))[cid]['value'] for cid in ('a', 'b')] == [Decimal('2'), Decimal('2')]


def test_queue_reuses_until_next_schedule_then_merges_competition_changes(tmp_path):
    from test_polymarket_lp import _LPCandidateQueryExchange
    exchange = _LPCandidateQueryExchange(NOW, {'A': Decimal('57.6'), 'B': Decimal('57.6')})
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW)
    service.refresh_price_history()
    service.refresh_competition_cache()
    first = service._candidate_queue_state_build()
    assert service._candidate_queue_state_build() is first
    service._publish_competition_batch({'condition-A': (Decimal('3'), NOW), 'condition-B': (Decimal('2'), NOW)},
        page_cursor=None, resume_cursor=None, checked_at=NOW, stop_event=None)
    service._publish_competition_batch({'condition-A': (Decimal('4'), NOW + timedelta(seconds=1))},
        page_cursor=None, resume_cursor=None, checked_at=NOW, stop_event=None)
    assert service._candidate_queue_state is first
    second = service._candidate_queue_state_build()
    assert second is not first
    assert second['competition_version'] == service._competition_version
    assert service._candidate_queue_state_build() is second


def test_commit_returned_before_cache_publish_cannot_mix_fallback(tmp_path, monkeypatch):
    """The vulnerable window begins after physical COMMIT, not before SQL execution."""
    store = PredictionArbitrageStore(tmp_path)
    store.lp_competitiveness_upsert([('b', Decimal('1'), NOW)])
    service = PolymarketLPService(store, object(), clock=lambda: NOW)
    service._competition_state = {'round_checked_at': NOW, 'competitiveness': {'a': (Decimal('1'), NOW)}}
    committed, release, started = Event(), Event(), Event()
    connection = store._connection
    class CommitBarrier:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, key):
            return getattr(self.conn, key)
        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            if sql == 'COMMIT':
                committed.set()
                assert release.wait(10), 'post-COMMIT barrier not released'
            return result
    monkeypatch.setattr(store, '_connection', lambda: CommitBarrier(connection()))
    def refresh():
        # This legacy reader deliberately updates A/B together, exercising the real entry point.
        class Exchange:
            def lp_market_competitiveness(self, **kwargs):
                return {'state': 'known', 'complete': True, 'round_checked_at': NOW,
                        'competitiveness': {'a': (Decimal('2'), NOW), 'b': (Decimal('2'), NOW)}}
        service.exchange = Exchange()
        return service.refresh_competition_cache(snapshot=False)
    def project():
        started.set()
        return service._competition_entries(('a', 'b'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(refresh)
        try:
            assert committed.wait(10)
            assert service._competition_lock.locked(), 'COMMIT/publication not fenced'
            assert service._competition_state['competitiveness']['a'][0] == Decimal('1'), 'cache published before commit completion'
            projection = pool.submit(project)
            assert started.wait(10)
            release.set()
            values = projection.result(timeout=10)
            publication.result(timeout=10)
        finally:
            release.set()
    assert [values[cid]['value'] for cid in ('a', 'b')] in ([Decimal('1'), Decimal('1')], [Decimal('2'), Decimal('2')])


@pytest.mark.parametrize('stop_after_commit', [False, True])
def test_uncommitted_batch_is_invisible_and_commit_survives_stop(tmp_path, monkeypatch, stop_after_commit):
    store = PredictionArbitrageStore(tmp_path)
    store.lp_competitiveness_upsert([('b', Decimal('1'), NOW)])
    service = PolymarketLPService(store, object(), clock=lambda: NOW)
    service._competition_state = {'round_checked_at': NOW, 'competitiveness': {'a': (Decimal('1'), NOW)}}
    written, release = Event(), Event()
    stop = Event()
    connection = store._connection
    class BodyBarrier:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, key):
            return getattr(self.conn, key)
        def executemany(self, sql, rows):
            result = self.conn.executemany(sql, rows)
            written.set()
            assert release.wait(10)
            return result
        def execute(self, sql, *args):
            result = self.conn.execute(sql, *args)
            if sql == 'COMMIT' and stop_after_commit:
                stop.set()
            return result
    monkeypatch.setattr(store, '_connection', lambda: BodyBarrier(connection()))
    with ThreadPoolExecutor(max_workers=1) as pool:
        publication = pool.submit(service._publish_competition_batch,
            {'a': (Decimal('2'), NOW), 'b': (Decimal('2'), NOW)},
            page_cursor=None, resume_cursor='page-two', checked_at=NOW, stop_event=stop)
        try:
            assert written.wait(10)
            assert not service._competition_lock.locked(), 'write body held competition read lock'
            values = service._competition_entries(('a', 'b'))
            assert [values[cid]['value'] for cid in ('a', 'b')] == [Decimal('1'), Decimal('1')]
        finally:
            release.set()
        publication.result(timeout=10)
    assert [service._competition_entries(('a', 'b'))[cid]['value'] for cid in ('a', 'b')] == [Decimal('2'), Decimal('2')]
    assert store.lp_competitiveness_progress() == {'next_start_cursor': 'page-two'}


def test_batch_sql_failure_rolls_back_both_facts_and_bookmark(tmp_path, monkeypatch):
    import sqlite3
    store = PredictionArbitrageStore(tmp_path)
    store.lp_competitiveness_upsert([('a', Decimal('1'), NOW), ('b', Decimal('1'), NOW)])
    service = PolymarketLPService(store, object(), clock=lambda: NOW)
    connection = store._connection
    def failing():
        conn = connection()
        conn.execute("CREATE TEMP TRIGGER fail_b BEFORE INSERT ON lp_market_competitiveness "
                     "WHEN NEW.condition_id='b' BEGIN SELECT RAISE(ABORT,'batch failed'); END")
        return conn
    monkeypatch.setattr(store, '_connection', failing)
    with pytest.raises(sqlite3.IntegrityError):
        service._publish_competition_batch({'a': (Decimal('2'), NOW), 'b': (Decimal('2'), NOW)},
            page_cursor=None, resume_cursor='next', checked_at=NOW, stop_event=None)
    assert store.lp_competitiveness_map() == {'a': (Decimal('1'), NOW), 'b': (Decimal('1'), NOW)}
    assert store.lp_competitiveness_progress() == {}
    assert service._competition_entries(('a', 'b'))['a']['value'] == 1


def test_stopped_page_replays_and_history_writes_during_batch_yield(tmp_path, monkeypatch):
    store = PredictionArbitrageStore(tmp_path)
    stop = Event()
    entered, release = Event(), Event()
    class StopBetweenBatches:
        is_set = stop.is_set
        def wait(self, pause):
            assert not service._competition_lock.locked()
            entered.set()
            assert release.wait(10)
            stop.set()
            return True
    exchange = make_competition_adapter(CompetitionOpener())
    service = PolymarketLPService(store, exchange)
    monkeypatch.setattr(polymarket_lp, '_LP_COMPETITION_BATCH_SIZE', 1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        refresh = pool.submit(service.refresh_competition_cache, StopBetweenBatches())
        try:
            assert entered.wait(10)
            assert store.lp_save_price_history_batch([{'condition_id': 'history', 'token_id': 'token',
                'samples': [], 'summary': {'state': 'known'}}]) == 1
            assert store.lp_competitiveness_progress() == {'next_start_cursor': None}
        finally:
            release.set()
        assert refresh.result(timeout=10)['state'] == 'unknown'
    restarted = PolymarketLPService(store, make_competition_adapter(CompetitionOpener()))
    assert restarted.refresh_competition_cache()['state'] == 'known'
    assert restarted.exchange._urlopen_fn.calls == [None, 'Mg==']
    assert store.lp_competitiveness_count() == 3


def test_completed_page_resumes_next_page_after_transport_failure(tmp_path, monkeypatch):
    from open_trader import polymarket_trading
    monkeypatch.setattr(polymarket_trading, 'LP_COMPETITIVENESS_RETRY_PAUSE_SECONDS', 0)
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, make_competition_adapter(CompetitionOpener(page_two_failures=-1)))
    assert service.refresh_competition_cache()['state'] == 'partial'
    assert store.lp_competitiveness_progress() == {'next_start_cursor': 'Mg=='}
    restarted = PolymarketLPService(store, make_competition_adapter(CompetitionOpener()))
    assert restarted.refresh_competition_cache()['state'] == 'known'
    assert restarted.exchange._urlopen_fn.calls == ['Mg==']
    assert store.lp_competitiveness_count() == 3
    assert restarted._competition_entries(('condition-a',))['condition-a']['source'] == 'store'


@pytest.mark.parametrize('page', ['empty', 'error', 'repeat'])
def test_native_empty_error_and_duplicate_cursor_do_not_skip_page(tmp_path, page):
    from test_polymarket_trading import FakeResponse
    store = PredictionArbitrageStore(tmp_path)
    def opener(request, **kwargs):
        if 'next_cursor=' not in request.full_url:
            if page == 'error':
                return FakeResponse({'data': 'invalid', 'next_cursor': 'p2'})
            return FakeResponse({'data': [] if page == 'empty' else [
                {'condition_id': 'a', 'market_competitiveness': '2'}],
                'next_cursor': 'p2' if page == 'repeat' else 'LTE='})
        return FakeResponse({'data': [{'condition_id': 'b', 'market_competitiveness': '2'}], 'next_cursor': 'p2'})
    service = PolymarketLPService(store, make_competition_adapter(opener))
    result = service.refresh_competition_cache()
    assert result['state'] == {'empty': 'known', 'error': 'unknown', 'repeat': 'partial'}[page]
    assert store.lp_competitiveness_progress() == ({'next_start_cursor': None} if page == 'empty' else
                                                {'next_start_cursor': 'p2'} if page == 'repeat' else {})
    assert store.lp_competitiveness_map().keys() == ({'a'} if page == 'repeat' else set())


def test_restart_recovers_commit_before_cache_publication_and_same_value_new_time(tmp_path):
    store = PredictionArbitrageStore(tmp_path)
    stamp = NOW + timedelta(seconds=1)
    def crash():
        raise RuntimeError('crash after commit')
    with pytest.raises(RuntimeError, match='crash after commit'):
        store.lp_competitiveness_upsert([('a', Decimal('1.1234567890123456789'), NOW)],
            progress={'next_start_cursor': 'p2'}, after_commit=crash)
    restarted = PolymarketLPService(store, object(), clock=lambda: stamp)
    assert restarted._competition_entries(('a',))['a'] == {
        'value': Decimal('1.1234567890123456789'), 'checked_at': NOW, 'source': 'store', 'updated': None}
    restarted._publish_competition_batch({'a': (Decimal('1.1234567890123456789'), stamp)},
        page_cursor='p2', resume_cursor=None, checked_at=stamp, stop_event=None)
    assert store.lp_competitiveness_count() == 1
    assert store.lp_competitiveness_entry('a') == (Decimal('1.1234567890123456789'), stamp)
    assert restarted._competition_entries(('a',))['a']['updated'] is True


@pytest.mark.parametrize('newer_build', [False, True])
def test_queue_build_keeps_snapshot_and_old_build_never_overwrites_new(tmp_path, monkeypatch, newer_build):
    from open_trader import polymarket_lp_views
    from test_polymarket_lp import _LPCandidateQueryExchange
    exchange = _LPCandidateQueryExchange(NOW, {'A': Decimal('57.6'), 'B': Decimal('57.6')})
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path), exchange, clock=lambda: NOW)
    service.refresh_price_history()
    service.refresh_competition_cache()
    entered, release = Event(), Event()
    trial = polymarket_lp_views.lp_trial_candidates
    projections = []
    def blocked(directions, **kwargs):
        projections.append(dict(kwargs['competition']))
        if len(projections) == 1:
            entered.set()
            assert release.wait(10)
        return trial(directions, **kwargs)
    monkeypatch.setattr(polymarket_lp_views, 'lp_trial_candidates', blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(service._candidate_queue_state_build)
        try:
            assert entered.wait(10)
            service._publish_competition_batch({'condition-A': (Decimal('3'), NOW), 'condition-B': (Decimal('4'), NOW)},
                page_cursor=None, resume_cursor=None, checked_at=NOW, stop_event=None)
            new = service._candidate_queue_state_build() if newer_build else None
        finally:
            release.set()
        result = old.result(timeout=10)
    assert len(projections) == (2 if newer_build else 1), 'build chased batch updates'
    assert projections[0]['condition-A']['value'] != Decimal('3')
    assert result is service._candidate_queue_state
    if newer_build:
        assert result is new
        assert result['competition_version'] == service._competition_version
    else:
        assert result['competition_version'] < service._competition_version
        assert service._candidate_queue_state_build() is not result


def test_complete_round_releases_absent_cache_keys_but_preserves_store_facts(tmp_path):
    from test_polymarket_trading import FakeResponse
    store = PredictionArbitrageStore(tmp_path)
    def reader(identity):
        return make_competition_adapter(lambda *args, **kwargs: FakeResponse({
            'data': [{'condition_id': identity, 'market_competitiveness': '2'}], 'next_cursor': 'LTE='}))
    service = PolymarketLPService(store, reader('a'), clock=lambda: NOW)
    for identity in ('a', 'b', 'c', 'd'):
        service.exchange = reader(identity)
        service.refresh_competition_cache()
        assert set(service._competition_state['competitiveness']) == {identity}
    projection = service._competition_entries(('a', 'b', 'c', 'd'))
    assert set(projection) == {'a', 'b', 'c', 'd'}
    assert all(projection[cid]['source'] == 'store' and projection[cid]['updated'] is None for cid in ('a', 'b', 'c'))
    assert store.lp_competitiveness_count() == 4


def test_condition_only_compatibility_reader_fences_cache_and_fallback(tmp_path, monkeypatch):
    store = PredictionArbitrageStore(tmp_path)
    store.lp_competitiveness_upsert([('b', Decimal('1'), NOW)])
    service = PolymarketLPService(store, object(), clock=lambda: NOW)
    service._competition_state = {'round_checked_at': NOW, 'competitiveness': {'a': (Decimal('1'), NOW)}}
    entered, release, body_finished = Event(), Event(), Event()
    read = store.lp_competitiveness_map
    def wrapper(*, condition_ids=None):
        entered.set()
        assert release.wait(10)
        return read(condition_ids=condition_ids)
    monkeypatch.setattr(store, 'lp_competitiveness_map', wrapper)
    upsert = store.lp_competitiveness_upsert
    def observed(entries, **kwargs):
        callback = kwargs['after_commit']
        guard = kwargs['commit_guard']
        from contextlib import contextmanager
        @contextmanager
        def barrier_guard():
            body_finished.set()
            with guard:
                yield
        return upsert(entries, **{**kwargs, 'commit_guard': barrier_guard(), 'after_commit': callback})
    monkeypatch.setattr(store, 'lp_competitiveness_upsert', observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        projection = pool.submit(service._competition_entries, ('a', 'b'))
        try:
            assert entered.wait(10)
            publication = pool.submit(service._publish_competition_batch,
                {'a': (Decimal('2'), NOW), 'b': (Decimal('2'), NOW)},
                page_cursor=None, resume_cursor=None, checked_at=NOW, stop_event=None)
            assert body_finished.wait(10), 'writer body blocked by compatibility read'
            assert store.lp_competitiveness_entry('b')[0] == 1
        finally:
            release.set()
        values = projection.result(timeout=10)
        publication.result(timeout=10)
    assert [values[cid]['value'] for cid in ('a', 'b')] == [Decimal('1'), Decimal('1')]


def test_failed_native_read_keeps_old_checked_at_and_marks_not_updated(tmp_path, monkeypatch):
    from open_trader import polymarket_trading
    monkeypatch.setattr(polymarket_trading, 'LP_COMPETITIVENESS_RETRY_PAUSE_SECONDS', 0)
    store = PredictionArbitrageStore(tmp_path)
    exchange = make_competition_adapter(CompetitionOpener(fail_all=True))
    service = PolymarketLPService(store, exchange)
    old = NOW - timedelta(days=1)
    service._competition_state = {'competitiveness': {'a': (Decimal('2'), old)}}
    service.clock = lambda: old + timedelta(minutes=1)
    result = service.refresh_competition_cache()
    assert result['state'] == 'unknown'
    assert result['not_updated'] == ['a']
    entry = service._competition_entries(('a',))['a']
    assert entry == {'value': Decimal('2'), 'checked_at': old, 'source': 'fresh', 'updated': False}


def test_native_numeric_decimal_keeps_all_digits(tmp_path):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"data":[{"condition_id":"a","market_competitiveness":0.1234567890123456789}],"next_cursor":"LTE="}'
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, make_competition_adapter(lambda *args, **kwargs: Response()))
    service.refresh_competition_cache()
    assert store.lp_competitiveness_entry('a')[0] == Decimal('0.1234567890123456789')


def test_later_build_sequence_cannot_overwrite_newer_competition_snapshot(tmp_path, monkeypatch):
    from open_trader import polymarket_lp_views
    from test_polymarket_lp import _LPCandidateQueryExchange
    from threading import Lock
    service = PolymarketLPService(PredictionArbitrageStore(tmp_path),
        _LPCandidateQueryExchange(NOW, {'A': Decimal('57.6'), 'B': Decimal('57.6')}), clock=lambda: NOW)
    service.refresh_price_history()
    service.refresh_competition_cache()
    first_prepared, release_first, second_trial, release_second = Event(), Event(), Event(), Event()
    guard, calls = Lock(), []
    snapshot = service._prepared_input_snapshot
    trial = polymarket_lp_views.lp_trial_candidates
    def delayed_snapshot():
        with guard:
            calls.append(True)
            first = len(calls) == 1
        if first:
            first_prepared.set()
            assert release_first.wait(10)
        return snapshot()
    def delayed_trial(directions, **kwargs):
        if kwargs['competition']['condition-A']['value'] != Decimal('3'):
            second_trial.set()
            assert release_second.wait(10)
        return trial(directions, **kwargs)
    monkeypatch.setattr(service, '_prepared_input_snapshot', delayed_snapshot)
    monkeypatch.setattr(polymarket_lp_views, 'lp_trial_candidates', delayed_trial)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service._candidate_queue_state_build)
        try:
            assert first_prepared.wait(10)
            second = pool.submit(service._candidate_queue_state_build)
            assert second_trial.wait(10)
            service._publish_competition_batch({'condition-A': (Decimal('3'), NOW)},
                page_cursor=None, resume_cursor=None, checked_at=NOW, stop_event=None)
            release_first.set()
            new_snapshot = first.result(timeout=10)
        finally:
            release_first.set()
            release_second.set()
        assert second.result(timeout=10) is new_snapshot
    assert service._candidate_queue_state is new_snapshot
    assert new_snapshot['competition_version'] == service._competition_version


def test_old_database_adds_progress_table_without_removing_competition(tmp_path):
    store = PredictionArbitrageStore(tmp_path)
    store.lp_competitiveness_upsert([('a', Decimal('2'), NOW)])
    with store._transaction() as connection:
        connection.execute('DROP TABLE lp_competition_progress')
    restored = PredictionArbitrageStore(tmp_path)
    assert restored.lp_competitiveness_progress() == {}
    assert restored.lp_competitiveness_entry('a') == (Decimal('2'), NOW)


@pytest.mark.parametrize('reader_kind', ['kwargs_ignored', 'explicit_without_snapshot', 'snapshot_failure', 'empty_snapshot'])
def test_legacy_wrapper_requires_explicit_connection_and_pinned_snapshot(tmp_path, monkeypatch, reader_kind):
    """A kwargs bind cannot prove that a reopened fallback shares the cache snapshot."""
    store = PredictionArbitrageStore(tmp_path)
    store.lp_competitiveness_upsert([('b', Decimal('1'), NOW)])
    service = PolymarketLPService(store, object(), clock=lambda: NOW)
    service._competition_state = {'round_checked_at': NOW, 'competitiveness': {'a': (Decimal('1'), NOW)}}
    entered, release, body_finished, committed = Event(), Event(), Event(), Event()
    read = store.lp_competitiveness_map
    def fallback(condition_ids):
        guarded = service._competition_lock.locked()
        entered.set()
        assert release.wait(10), 'legacy fallback not released'
        if not guarded:
            assert committed.wait(10), 'unfenced publication did not commit'
        return read(condition_ids=condition_ids)
    def kwargs_wrapper(*, condition_ids=None, **kwargs):
        return fallback(condition_ids)  # Legacy reader intentionally ignores connection.
    def explicit_wrapper(*, condition_ids=None, connection=None):
        return fallback(condition_ids)
    monkeypatch.setattr(store, 'lp_competitiveness_map', kwargs_wrapper if reader_kind == 'kwargs_ignored' else explicit_wrapper)
    if reader_kind == 'explicit_without_snapshot':
        monkeypatch.setattr(store, 'lp_competitiveness_snapshot', None)
    elif reader_kind == 'snapshot_failure':
        def failed_snapshot():
            raise RuntimeError('snapshot unavailable')
        monkeypatch.setattr(store, 'lp_competitiveness_snapshot', failed_snapshot)
    elif reader_kind == 'empty_snapshot':
        from contextlib import contextmanager
        @contextmanager
        def empty_snapshot():
            yield None
        monkeypatch.setattr(store, 'lp_competitiveness_snapshot', empty_snapshot)
    upsert = store.lp_competitiveness_upsert
    def observed(entries, **kwargs):
        callback, guard = kwargs['after_commit'], kwargs['commit_guard']
        from contextlib import contextmanager
        @contextmanager
        def observed_guard():
            body_finished.set()
            with guard:
                yield
        def after_commit():
            callback()
            committed.set()
        return upsert(entries, **{**kwargs, 'commit_guard': observed_guard(), 'after_commit': after_commit})
    monkeypatch.setattr(store, 'lp_competitiveness_upsert', observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        projection = pool.submit(service._competition_entries, ('a', 'b'))
        try:
            assert entered.wait(10), 'fallback did not start'
            publication = pool.submit(service._publish_competition_batch,
                {'a': (Decimal('2'), NOW), 'b': (Decimal('2'), NOW)},
                page_cursor=None, resume_cursor=None, checked_at=NOW, stop_event=None)
            assert body_finished.wait(10), 'writer body blocked by fallback'
        finally:
            release.set()
        values = projection.result(timeout=10)
        publication.result(timeout=10)
    assert [values[cid]['value'] for cid in ('a', 'b')] == [Decimal('1'), Decimal('1')]
    assert committed.is_set()
