"""Public metadata contracts, with real SQLite and offline API boundaries."""
from datetime import datetime, timezone
import json
import sqlite3

import pytest
from open_trader import polymarket_trading
from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

UTC = timezone.utc
T = datetime(2026, 10, 16, 4, tzinfo=UTC)
A = '0x' + 'a' * 64
B = '0x' + 'b' * 64


def _adapter(store, monkeypatch, *, rows=None, clock=None, calls=None, **kwargs):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = T if clock is None else clock[0]
            scalar = cls.fromisoformat(value.isoformat())
            return scalar if tz is None else scalar.astimezone(tz)

    class Public:
        def list_markets(self, *, condition_ids, page_size, closed=None):
            if calls is not None:
                calls.append(tuple(condition_ids))
            if isinstance(rows, Exception):
                raise rows
            if callable(rows):
                return rows(condition_ids)
            return tuple((rows or {}).get(cid, {
                'id': '349', 'condition_id': cid, 'question': 'Literal title',
                'outcomes': {'yes': {'label': 'Yes', 'token_id': 'yes-current'},
                             'no': {'label': 'No', 'token_id': 'no-current'}},
                'events': [],
            }) for cid in condition_ids)

        def list_market_rewards(self, **kwargs):
            return ()

        def get_order_books(self, *, token_ids):
            return tuple({"token_id": token, "bids": [], "asks": [], "tick_size": "0.01"} for token in token_ids)

        def close(self):
            pass

    monkeypatch.setattr(polymarket_trading, 'datetime', Clock)
    return PolymarketTradingClient(
        TradingConfig('0x' + '1' * 40, '0x' + '2' * 40), client=object(),
        metadata_cache=store, public_client_factory=Public, **kwargs,
    )


def test_startup_initializes_metadata_once_and_preserves_existing_rows(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    sql = []

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(sql.append)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', connect)
    for existing in (False, True):
        store = PredictionArbitrageStore(tmp_path / str(existing))
        if existing:
            with real_connect(store.path) as connection:
                connection.execute('CREATE TABLE lp_market_metadata_cache(condition_id TEXT PRIMARY KEY,payload TEXT NOT NULL,expires_at REAL NOT NULL,present INTEGER NOT NULL)')
                connection.execute('INSERT INTO lp_market_metadata_cache VALUES (?,?,?,1)',
                    (A, json.dumps({'condition_id': A, 'market_title': 'Retained literal',
                         'outcomes': {'yes': {'token_id': 'old-yes'}}}), T.timestamp()+43200))
        sql.clear()
        adapter = _adapter(store, monkeypatch)
        try:
            with real_connect(store.path) as connection:
                assert connection.execute("SELECT name FROM sqlite_master WHERE name='lp_market_metadata_cache'").fetchone()
                if existing:
                    assert json.loads(connection.execute('SELECT payload FROM lp_market_metadata_cache').fetchone()[0])['market_title'] == 'Retained literal'
            adapter.attach_metadata_cache(store)
            assert sum('CREATE TABLE IF NOT EXISTS lp_market_metadata_cache' in statement for statement in sql) == 1
            sql.clear()
            assert adapter.lp_market_metadata_batch((A,))['state'] == 'known'
            assert adapter.lp_market_metadata_fresh((B,))[B]['outcomes']['yes']['token_id'] == 'yes-current'
            adapter.stop_lp_metadata_writer()
            assert not any('CREATE ' in statement.upper() for statement in sql)
            assert not any('FROM lp_market_metadata_cache m' in statement for statement in sql)
        finally:
            adapter.stop_lp_metadata_writer()


def test_startup_metadata_failure_degrades_only_disposable_cache(tmp_path, monkeypatch):
    store = PredictionArbitrageStore(tmp_path)
    real_connect = sqlite3.connect
    authorizer_callbacks, ddl_submissions = [], []
    deny_exclusions = [False]

    def authorize(action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_CREATE_TABLE and arg1 == 'lp_market_metadata_cache':
            authorizer_callbacks.append(arg1)
            return sqlite3.SQLITE_DENY
        if deny_exclusions[0] and action == sqlite3.SQLITE_READ and arg1 == 'lp_market_exclusions':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if 'CREATE TABLE IF NOT EXISTS lp_market_metadata_cache' in ' '.join(sql.split()):
                ddl_submissions.append(sql)
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        from pathlib import Path
        observed = Path(args[0]) == store.path
        if observed:
            kwargs['factory'] = ObservedConnection
        connection = real_connect(*args, **kwargs)
        if observed:
            connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', connect)
    adapter = _adapter(store, monkeypatch)
    try:
        assert len(ddl_submissions) == 1
        first_callbacks = len(authorizer_callbacks)
        assert first_callbacks >= 1
        status = adapter.lp_metadata_persistence_status()
        assert status['persistence_enabled'] is False
        assert status['initialization_state'] == 'DISABLED'
        adapter.attach_metadata_cache(store)
        assert adapter.lp_market_metadata_batch((A,))['markets'][A]['outcomes']['no']['token_id'] == 'no-current'
        assert adapter.lp_market_metadata_fresh((A,))[A]['outcomes']['yes']['token_id'] == 'yes-current'
        assert len(ddl_submissions) == 1
        assert len(authorizer_callbacks) == first_callbacks  # attach/read never retry schema
        # An independent adapter's transport failure never proves absence.
        failing = _adapter(store, monkeypatch, rows=RuntimeError('offline transport'))
        try:
            result = failing.lp_market_metadata_batch((B,))
            assert result['state'] == 'unknown'
            assert result['markets'] == {}
            assert result['confirmed_absent_ids'] == ()
            assert B in result['failed_ids']
        finally:
            failing.stop_lp_metadata_writer()
        # SQLite may consult the authorizer more than once per execute submission.
        # The external driver boundary measures the actual lifecycle SQL attempts.
        assert len(ddl_submissions) == 2
        assert len(authorizer_callbacks) > first_callbacks
        assert adapter.lp_metadata_persistence_status()['saved_entries'] == 0
        deny_exclusions[0] = True
        with pytest.raises(sqlite3.DatabaseError):
            adapter.configure_lp_candidate_exclusions(True)
        assert len(ddl_submissions) == 2
    finally:
        adapter.stop_lp_metadata_writer()


def test_startup_warmload_is_bounded_and_preserves_original_expiry(tmp_path, monkeypatch):
    from datetime import timedelta
    store = PredictionArbitrageStore(tmp_path)
    store.initialize_lp_metadata_cache()
    C, D, E, F = ('0x' + letter * 64 for letter in 'cdef')
    with sqlite3.connect(store.path) as connection:
        for cid, expiry, present, payload in (
            (A, 43200, 1, {'condition_id': A, 'market_title': 'warm A', 'metadata_checked_at': T.isoformat()}),
            (C, 40000, 1, {'condition_id': C, 'market_title': 'warm C', 'metadata_checked_at': (T-timedelta(seconds=3200)).isoformat()}),
            (B, 3600, 0, {'closed_checked': True}),
            (D, 3000, 1, {'condition_id': D, 'market_title': 'not retained'}),
            (E, 2000, 0, {}),
            (F, 1700, 1, {'condition_id': F, 'market_title': 'expired'}),
        ):
            connection.execute('INSERT INTO lp_market_metadata_cache VALUES (?,?,?,?)',
                               (cid, json.dumps(payload), T.timestamp()+expiry, present))
    clock = [T + timedelta(seconds=1800)]
    calls = []
    real_connect = sqlite3.connect
    deny_reads = [False]

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_authorizer(lambda action, arg1, *_: sqlite3.SQLITE_DENY
            if deny_reads[0] and action == sqlite3.SQLITE_READ and arg1 == 'lp_market_metadata_cache'
            else sqlite3.SQLITE_OK)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', connect)
    adapter = _adapter(store, monkeypatch, clock=clock, calls=calls,
                       metadata_cache_policy={'max_entries': 3})
    try:
        deny_reads[0] = True
        result = adapter.lp_market_metadata_batch((A, B, C))
        assert calls == []
        assert result['markets'][A]['market_title'] == 'warm A'
        assert result['markets'][A]['metadata_checked_at'] == T.isoformat()
        assert result['confirmed_absent_ids'] == (B,)
        adapter.lp_market_metadata_batch((D, E, F))
        assert set(calls[-1]) == {D, E, F}
        clock[0] = T + timedelta(seconds=3601)
        assert adapter.lp_market_metadata_batch((B,))['markets'][B]['outcomes']['yes']['token_id'] == 'yes-current'
        clock[0] = T + timedelta(seconds=43201)
        assert adapter.lp_market_metadata_batch((A,))['markets'][A]['market_title'] == 'Literal title'
    finally:
        adapter.stop_lp_metadata_writer()
    # A startup SELECT denial loses only optional warm data.
    calls.clear()
    fallback = _adapter(store, monkeypatch, clock=clock, calls=calls)
    try:
        assert fallback.lp_market_metadata_batch((A,))['state'] == 'known'
        assert calls == [(A,)]
    finally:
        fallback.stop_lp_metadata_writer()

    deny_reads[0] = False
    clock[0] = T
    core_store = PredictionArbitrageStore(tmp_path / 'core-only-restart')
    calls.clear()
    producer = _adapter(core_store, monkeypatch, clock=clock, calls=calls)
    try:
        assert 'market_title' not in producer.lp_market_metadata_batch((A,), planning_only=True)['markets'][A]
        _wait_status(producer, lambda value: value['saved_entries'] == 1)
    finally:
        producer.stop_lp_metadata_writer()
    calls.clear()
    restarted = _adapter(core_store, monkeypatch, clock=clock, calls=calls)
    try:
        narrow = restarted.lp_market_metadata_batch((A,), planning_only=True)['markets'][A]
        assert 'market_title' not in narrow
        assert calls == []
        complete = restarted.lp_market_metadata_batch((A,))['markets'][A]
        assert complete['market_title'] == 'Literal title'
        assert complete['outcomes']['yes']['token_id'] == 'yes-current'
        assert calls == [(A,)]
    finally:
        restarted.stop_lp_metadata_writer()


def test_public_metadata_has_no_private_sqlite_dependency(tmp_path, monkeypatch):
    import gc
    import weakref
    store = PredictionArbitrageStore(tmp_path)
    real_connect = sqlite3.connect
    originals = []

    def connect(filename, *args, **kwargs):
        assert filename != '', 'metadata attempted a private temporary SQLite connection'
        return real_connect(filename, *args, **kwargs)

    class SDKRow:
        def model_dump(self, **kwargs):
            return {'id': '349', 'condition_id': A, 'events': [],
                    'outcomes': {'yes': {'token_id': 'yes-current', 'label': 'Yes'},
                                 'no': {'token_id': 'no-current', 'label': 'No'}}}

    def response(ids):
        row = SDKRow()
        originals.append(weakref.ref(row))
        return (row,)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    adapter = _adapter(store, monkeypatch, rows=response)
    try:
        first = adapter.lp_market_metadata_batch((A,))['markets'][A]
        original_time = first['metadata_checked_at']
        first['outcomes']['yes']['token_id'] = 'caller-corruption'
        second = adapter.lp_market_metadata_batch((A,))['markets'][A]
        assert second['outcomes']['yes']['token_id'] == 'yes-current'
        assert second['outcomes']['no']['token_id'] == 'no-current'
        assert second['metadata_checked_at'] == original_time == T
        assert adapter.lp_market_metadata_fresh((A,))[A]['outcomes']['yes']['token_id'] == 'yes-current'
        gc.collect()
        assert originals and all(reference() is None for reference in originals)
    finally:
        adapter.stop_lp_metadata_writer()


def test_memory_budget_evicts_lru_without_losing_current_result(tmp_path, monkeypatch):
    C, D = ('0x' + letter * 64 for letter in 'cd')
    calls = []

    def rows(ids):
        return tuple({'id': cid, 'condition_id': cid,
            'question': 'display overflow' * 2000 if cid == C else 'Small title',
            'outcomes': {'yes': {'token_id': 'x' * 30000 if cid == D else cid + '-yes'},
                         'no': {'token_id': cid + '-no'}}, 'events': []} for cid in ids)

    adapter = _adapter(None, monkeypatch, rows=rows, calls=calls,
        metadata_cache_policy={'max_entries': 2, 'max_retained_value_bytes': 20000,
                               'display_max_entries': 2, 'display_max_retained_value_bytes': 10000})
    try:
        first = adapter.lp_market_metadata_batch((A,))['markets'][A]
        adapter.lp_market_metadata_batch((B,))
        adapter.lp_market_metadata_batch((A,))
        assert calls == [(A,), (B,)]
        huge_display = adapter.lp_market_metadata_batch((C,))['markets'][C]
        assert huge_display['market_title'] == 'display overflow' * 2000
        adapter.lp_market_metadata_batch((A,))  # display overflow did not evict core A
        assert calls == [(A,), (B,), (C,)]
        adapter.lp_market_metadata_batch((B,))
        assert calls[-1] == (B,)  # B was LRU, despite a more recent insertion
        before = len(calls)
        for _ in range(2):
            assert adapter.lp_market_metadata_batch((D,))['markets'][D]['outcomes']['yes']['token_id'] == 'x' * 30000
        assert len(calls) == before + 2  # oversize result returned but never retained
        status = adapter.lp_metadata_cache_status()
        assert 0 < status['core_entries'] <= 2
        assert 0 < status['core_retained_value_bytes'] <= 20000
        assert status['display_entries'] <= 2
        assert status['display_retained_value_bytes'] <= 10000
        assert first['outcomes']['yes']['token_id'] == A + '-yes'
        assert first['market_title'] == 'Small title'
    finally:
        adapter.stop_lp_metadata_writer()


def test_planning_projection_preserves_risk_inputs_and_excludes_display(tmp_path, monkeypatch):
    from datetime import timedelta
    from decimal import Decimal
    from types import SimpleNamespace
    clock, calls, mode = [T], [], ['planning']
    display = {'question', 'title', 'slug', 'market_url', 'url', 'prices'}
    literal = {'id': '349', 'condition_id': A, 'question': 'Full display title',
        'market_url': 'https://example.invalid/market', 'prices': {'one_day_price_change': '0.12'},
        'state': {'accepting_orders': True, 'closed': False, 'resolved': False},
        'trading': {'minimum_tick_size': '0.01', 'minimum_order_size': '20',
                    'fees_enabled': False, 'fee_schedule': {'exponent': 2, 'rate': '0.02'}},
        'rewards': {'rewards_min_size': '20', 'rewards_max_spread': '3.5'},
        'outcomes': {'yes': {'token_id': 'yes-current', 'label': 'Yes'},
                     'no': {'token_id': 'no-current', 'label': 'No'}}, 'events': []}

    class Trap(dict):
        def __getitem__(self, key):
            assert key not in display, 'planning inspected display field ' + key
            return super().__getitem__(key)

        def get(self, key, default=None):
            assert key not in display, 'planning inspected display field ' + key
            return super().get(key, default)

    def rows(ids):
        if mode[0] == 'failure':
            raise RuntimeError('offline transport')
        return (Trap(literal) if mode[0] == 'planning' else dict(literal),)

    adapter = _adapter(None, monkeypatch, rows=rows, calls=calls, clock=clock)
    try:
        result = adapter.lp_market_metadata_batch((A,), planning_only=True)
        assert result['state'] == 'known'
        metadata = result['markets'][A]
        approved = {'market_id', 'condition_id', 'exchange_type', 'outcomes', 'accepting_orders',
            'closed', 'resolved', 'event_id', 'game_id', 'game_start_time', 'event_start_time',
            'event_ended', 'event_finished_at', 'tick_size', 'minimum_order_size', 'fee',
            'fees_enabled', 'fee_exponent', 'taker_fee_rate', 'reward_min_size', 'reward_max_spread',
            'metadata_checked_at', 'fees_checked_at'}
        assert set(metadata) == approved
        assert metadata['metadata_checked_at'] == metadata['fees_checked_at'] == T
        assert metadata['tick_size'] == Decimal('0.01')
        assert metadata['minimum_order_size'] == metadata['reward_min_size'] == Decimal('20')
        assert metadata['reward_max_spread'] == Decimal('0.035')
        assert metadata['outcomes']['yes']['token_id'] == 'yes-current'
        assert metadata['outcomes']['no']['token_id'] == 'no-current'
        clock[0] = T + timedelta(seconds=60)
        scope = SimpleNamespace(account=adapter.config.wallet_address)
        source = adapter.lp_reader_source_port().read('market:' + A + ':yes-current', scope)
        assert source['metadata'] == metadata
        assert calls == [(A,)]
        clock[0] = T + timedelta(seconds=61)
        mode[0] = 'failure'
        failed = adapter.lp_market_metadata_batch((A,), planning_only=True)
        assert failed['state'] == 'unknown'
        assert failed['markets'] == {}
        assert failed['confirmed_absent_ids'] == ()
        assert A in failed['failed_ids']
        mode[0] = 'display'
        legacy = adapter.lp_market_metadata_batch((A,))['markets'][A]
        assert legacy['market_title'] == 'Full display title'
        assert legacy['market_url'] == 'https://example.invalid/market'
        assert legacy['price_change_24h'] == Decimal('0.12')
        assert legacy['price_change_24h_source'] == 'polymarket.prices.one_day_price_change'
    finally:
        adapter.stop_lp_metadata_writer()

    def failed_event_rows(ids):
        return ({**literal, 'events': [{'id': 'event-unavailable'}]},)

    incomplete = _adapter(None, monkeypatch, rows=failed_event_rows, clock=clock)
    try:
        partial = incomplete.lp_market_metadata_batch((A,), planning_only=True)
        assert A in partial['markets'] and A in partial['failed_ids']
        with pytest.raises(ValueError, match='market_source_unknown'):
            incomplete.lp_reader_source_port().read('market:' + A + ':yes-current', scope)
    finally:
        incomplete.stop_lp_metadata_writer()


def _wait_status(adapter, predicate):
    import threading
    import time
    deadline = time.monotonic() + 5
    wake = threading.Event()
    while True:
        status = adapter.lp_metadata_persistence_status()
        if predicate(status):
            return status
        assert time.monotonic() < deadline, status
        wake.wait(0.005)


@pytest.mark.parametrize('entrypoint', ('batch', 'fresh', 'source'))
def test_public_metadata_returns_while_expired_prune_is_blocked(tmp_path, monkeypatch, entrypoint):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from types import SimpleNamespace
    import threading
    store = PredictionArbitrageStore(tmp_path)
    clock, failure = [T], [False]

    def rows(ids):
        if failure[0]:
            raise RuntimeError('offline transport')
        return ({'id': '349', 'condition_id': A, 'events': [],
                 'outcomes': {'yes': {'token_id': 'yes-current'}, 'no': {'token_id': 'no-current'}}},)

    adapter = _adapter(store, monkeypatch, rows=rows, clock=clock)
    adapter.configure_lp_candidate_exclusions(True)
    real_connect = sqlite3.connect
    with real_connect(store.path) as connection:
        connection.execute('INSERT INTO lp_market_metadata_cache VALUES (?,?,?,1)',
                           (A, json.dumps({'condition_id': A, 'market_title': 'expired durable'}), T.timestamp()-1))
    blocker = real_connect(store.path, isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    started, deleted = threading.Event(), threading.Event()

    class Connection(sqlite3.Connection):
        metadata_delete = False

        def execute(self, sql, parameters=()):
            if sql == 'BEGIN IMMEDIATE':
                started.set()
            if sql.lstrip().startswith('DELETE FROM lp_market_metadata_cache'):
                self.metadata_delete = True
            result = super().execute(sql, parameters)
            if sql == 'COMMIT' and self.metadata_delete:
                deleted.set()
            return result

    def connect(*args, **kwargs):
        kwargs['factory'] = Connection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    scope = SimpleNamespace(account=adapter.config.wallet_address)

    def read():
        if entrypoint == 'source':
            return adapter.lp_reader_source_port().read('market:' + A + ':yes-current', scope)['metadata']
        if entrypoint == 'fresh':
            return adapter.lp_market_metadata_fresh((A,))[A]
        return adapter.lp_market_metadata_batch((A,))['markets'][A]

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(read)
        assert started.wait(5), 'real writer admission was not observed'
        metadata = future.result(timeout=2)
        assert metadata['outcomes']['yes']['token_id'] == 'yes-current'
        assert not deleted.is_set()
        with real_connect(store.path) as connection:
            assert connection.execute('SELECT count(*) FROM lp_market_metadata_cache WHERE expires_at<=?', (T.timestamp(),)).fetchone()[0] == 1
        clock[0] = T + timedelta(seconds=43201)
        failure[0] = True
        result = adapter.lp_market_metadata_batch((A,), planning_only=True)
        assert result['state'] == 'unknown'
        assert result['markets'] == {}
        assert result['confirmed_absent_ids'] == ()
        assert A in result['failed_ids']
    finally:
        blocker.execute('ROLLBACK')
        blocker.close()
        pool.shutdown(wait=True)
        try:
            assert deleted.wait(5), 'actual DELETE commit not observed'
        finally:
            adapter.stop_lp_metadata_writer()


def test_prune_fifo_is_bounded_fair_and_failure_is_not_replayed(tmp_path, monkeypatch, request):
    import threading
    from datetime import timedelta
    real_connect = sqlite3.connect
    started = threading.Event()
    chunks, commits, titles = [], [], ['A1']

    def rows(ids):
        if isinstance(titles[0], Exception):
            raise titles[0]
        return tuple({'id': cid, 'condition_id': cid, 'question': titles[0], 'events': [],
                      'outcomes': {'yes': {'token_id': cid + '-yes'}, 'no': {'token_id': cid + '-no'}}} for cid in ids)

    store = PredictionArbitrageStore(tmp_path / 'fair')
    clock = [T]
    adapter = _adapter(store, monkeypatch, rows=rows, clock=clock,
        metadata_writer_policy={'max_entries': 4, 'batch_rows': 2})
    request.addfinalizer(adapter.stop_lp_metadata_writer)
    with real_connect(store.path) as connection:
        connection.executemany('INSERT INTO lp_market_metadata_cache VALUES (?,?,?,1)',
            [(f'expired-{index:04}', '{}', T.timestamp()-1) for index in range(850)])
        connection.execute('CREATE TABLE audit(kind TEXT, title TEXT)')
        connection.execute("CREATE TRIGGER audit_delete AFTER DELETE ON lp_market_metadata_cache BEGIN INSERT INTO audit VALUES('D',old.condition_id); END")
        connection.execute("CREATE TRIGGER audit_insert AFTER INSERT ON lp_market_metadata_cache BEGIN INSERT INTO audit VALUES('S',json_extract(new.payload,'$.market_title')); END")
        connection.execute("CREATE TRIGGER audit_update AFTER UPDATE ON lp_market_metadata_cache BEGIN INSERT INTO audit VALUES('S',json_extract(new.payload,'$.market_title')); END")
    blocker = real_connect(store.path, isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')

    class Connection(sqlite3.Connection):
        delete_rows = None

        def execute(self, sql, parameters=()):
            if sql == 'BEGIN IMMEDIATE':
                started.set()
            result = super().execute(sql, parameters)
            if sql.lstrip().startswith('DELETE FROM lp_market_metadata_cache'):
                self.delete_rows = result.rowcount
                chunks.append(result.rowcount)
            if sql == 'COMMIT' and self.delete_rows is not None:
                commits.append(self.delete_rows)
            return result

    def connect(*args, **kwargs):
        kwargs['factory'] = Connection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    try:
        adapter.lp_market_metadata_fresh((A,))
        assert started.wait(5)
        titles[0] = 'B1'
        adapter.lp_market_metadata_fresh((B,))
        titles[0] = 'A2'
        adapter.lp_market_metadata_fresh((A,))
        # Another due public read cannot create a second maintenance intent.
        titles[0] = RuntimeError("offline maintenance-only read")
        clock[0] = T + timedelta(seconds=43200)
        adapter.lp_market_metadata_batch((B,))
        status = adapter.lp_metadata_persistence_status()
        assert status['prune_pending'] + status['prune_inflight'] == 1
        assert status['prune_retained_bytes'] <= 1024
        assert status['pending_entries'] == 3
    finally:
        blocker.execute('ROLLBACK')
        blocker.close()
    try:
        _wait_status(adapter, lambda value: value['saved_entries'] == 3 and
                     value['prune_pending'] + value['prune_inflight'] == 0)
        assert chunks == commits == [400, 400, 50]
        with real_connect(store.path) as connection:
            audit = connection.execute('SELECT kind,title FROM audit ORDER BY rowid').fetchall()
            assert connection.execute("SELECT count(*) FROM lp_market_metadata_cache WHERE condition_id LIKE 'expired-%'").fetchone()[0] == 0
        assert audit[:400] == [('D', f'expired-{index:04}') for index in range(400)]
        assert audit[400:403] == [('S', 'A1'), ('S', 'B1'), ('S', 'A2')]
        assert adapter.lp_metadata_persistence_status()['failed_entries'] == 0
    finally:
        adapter.stop_lp_metadata_writer()

    # Abort a real DELETE, then allow later ordinary work without replay.
    failed_store = PredictionArbitrageStore(tmp_path / 'abort')
    failed = _adapter(failed_store, monkeypatch)
    with real_connect(failed_store.path) as connection:
        connection.execute('INSERT INTO lp_market_metadata_cache VALUES (?,?,?,1)', ('expired', '{}', T.timestamp()-1))
        connection.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON lp_market_metadata_cache BEGIN SELECT RAISE(ABORT,'external deletion fault'); END")
    try:
        failed.lp_market_metadata_batch((A,))
        status = _wait_status(failed, lambda value: value['prune_failed'] == 1 and value['saved_entries'] == 1)
        assert status['prune_unknown_outcome'] == 1
        with real_connect(failed_store.path) as connection:
            assert connection.execute("SELECT count(*) FROM lp_market_metadata_cache WHERE condition_id='expired'").fetchone()[0] == 1
            connection.execute('DROP TRIGGER refuse_delete')
        failed.lp_market_metadata_fresh((B,))
        _wait_status(failed, lambda value: value['saved_entries'] == 2)
        with real_connect(failed_store.path) as connection:
            assert connection.execute("SELECT count(*) FROM lp_market_metadata_cache WHERE condition_id='expired'").fetchone()[0] == 1
        assert failed.lp_metadata_persistence_status()['prune_completed_transactions'] == 0
    finally:
        failed.stop_lp_metadata_writer()

    # A real blocked save consumes ordinary capacity; oldest pending drops.
    overflow_store = PredictionArbitrageStore(tmp_path / 'overflow')
    overflow = _adapter(overflow_store, monkeypatch,
        metadata_writer_policy={'max_entries': 2, 'batch_rows': 1})
    try:
        overflow.lp_market_metadata_batch((A,))
        _wait_status(overflow, lambda value: value['saved_entries'] == 1 and value['prune_inflight'] == 0)
        blocker = real_connect(overflow_store.path, isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')
        started.clear()
        C = '0x' + 'c' * 64
        try:
            overflow.lp_market_metadata_fresh((A,))
            assert started.wait(5)
            overflow.lp_market_metadata_fresh((B,))
            overflow.lp_market_metadata_fresh((C,))
            status = overflow.lp_metadata_persistence_status()
            assert status['inflight_entries'] == 1
            assert status['pending_entries'] == 1
            assert status['dropped_overflow'] == 1
        finally:
            blocker.execute('ROLLBACK')
            blocker.close()
        _wait_status(overflow, lambda value: value['saved_entries'] == 3)
        with real_connect(overflow_store.path) as connection:
            assert connection.execute('SELECT condition_id FROM lp_market_metadata_cache ORDER BY condition_id').fetchall() == [(A,), (C,)]
    finally:
        overflow.stop_lp_metadata_writer()


@pytest.mark.parametrize('persistence', ('blocked', 'dropped'))
def test_current_token_exclusions_ignore_lagging_or_dropped_replica(tmp_path, monkeypatch, persistence):
    from datetime import timedelta
    from open_trader.polymarket_lp import PolymarketLPService
    import threading
    real_connect = sqlite3.connect
    store = PredictionArbitrageStore(tmp_path)
    store.initialize_lp_metadata_cache()
    obsolete = {'condition_id': A, 'market_title': 'obsolete durable',
        'metadata_checked_at': T.isoformat(), 'fees_checked_at': T.isoformat(),
        'outcomes': {'yes': {'token_id': 'obsolete-yes'}, 'no': {'token_id': 'obsolete-no'}}}
    store.lp_metadata_cache_store_entries({A: (T.timestamp()+43200, obsolete)})
    store.lp_save_price_history(A, 'obsolete-yes', [], {'state': 'known', 'checked_at': T})
    for cid, token, seconds in ((A, 'yes-current', 200), (A, 'no-current', 100),
                                (B, 'yes-current', 100), (B, 'no-current', 200)):
        store.lp_record_market_exclusion(cid, token, 'history_amplitude_exceeded', checked_at=T,
            cooldown_until=T+timedelta(seconds=seconds), now=T)
    clock, failure = [T], [False]

    def rows(ids):
        if failure[0]:
            raise RuntimeError('offline metadata unavailable')
        return tuple({'id': cid, 'condition_id': cid, 'events': [], 'question': 'Current API',
            'outcomes': {'yes': {'token_id': 'yes-current'}, 'no': {'token_id': 'no-current'}}} for cid in ids)

    policy = {'max_encoded_bytes': 64} if persistence == 'dropped' else None
    adapter = _adapter(store, monkeypatch, rows=rows, clock=clock, metadata_writer_policy=policy)
    service = PolymarketLPService(store, adapter, clock=lambda: clock[0], exclusions_enabled=True)
    blocker = None
    started = threading.Event()
    denied = [False]

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == 'BEGIN IMMEDIATE':
                started.set()
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        kwargs['factory'] = Connection
        connection = real_connect(*args, **kwargs)
        connection.set_authorizer(lambda action, name, *_: sqlite3.SQLITE_DENY
            if denied[0] and action == sqlite3.SQLITE_READ and name == 'lp_market_exclusions' else sqlite3.SQLITE_OK)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', connect)
    try:
        if persistence == 'blocked':
            blocker = real_connect(store.path, isolation_level=None)
            blocker.execute('BEGIN IMMEDIATE')
        assert adapter.lp_market_metadata_fresh((A,))[A]['outcomes']['yes']['token_id'] == 'yes-current'
        if persistence == 'blocked':
            assert started.wait(5)
            assert adapter.lp_metadata_persistence_status()['saved_entries'] == 0
        else:
            assert adapter.lp_metadata_persistence_status()['dropped_oversize'] == 1
        assert service._candidate_conditions((A,), now=clock[0]) == ()
        with real_connect(store.path) as connection:
            durable = json.loads(connection.execute('SELECT payload FROM lp_market_metadata_cache WHERE condition_id=?', (A,)).fetchone()[0])
            assert durable['outcomes']['yes']['token_id'] == 'obsolete-yes'
        # Risk/active-order access still gets actual metadata while candidates cool.
        assert adapter.lp_market_metadata_batch((A,))['markets'][A]['outcomes']['no']['token_id'] == 'no-current'
        clock[0] = T + timedelta(seconds=61)
        assert service._candidate_conditions((A,), now=clock[0]) == (), 'live token identity must outlast dynamic planning freshness'
        clock[0] = T + timedelta(seconds=101)
        assert service._candidate_conditions((A,), now=clock[0]) == (A,)
        assert service._candidate_allowed(((A, 'yes-current'), (A, 'no-current')), now=clock[0]) == ((A, 'no-current'),)
        adapter.lp_market_metadata_fresh((B,))
        assert service._candidate_allowed(((B, 'yes-current'), (B, 'no-current')), now=clock[0]) == ((B, 'yes-current'),)
        # Expired/evicted identity is refresh eligibility only, never known metadata.
        adapter.expire_lp_metadata_cache()
        assert service._candidate_conditions((A,), now=clock[0]) == (A,)
        failure[0] = True
        unknown = adapter.lp_market_metadata_batch((A,), planning_only=True)
        assert unknown['state'] == 'unknown' and unknown['markets'] == {}
        assert unknown['confirmed_absent_ids'] == ()
        denied[0] = True
        with pytest.raises(sqlite3.DatabaseError):
            service._candidate_conditions((A,), now=clock[0])
    finally:
        denied[0] = False
        if blocker is not None:
            blocker.execute('ROLLBACK')
            blocker.close()
        adapter.stop_lp_metadata_writer()


def test_runtime_stop_retains_owner_until_actual_prune_exit(tmp_path, monkeypatch, request):
    import shutil, subprocess
    from pathlib import Path
    from tests.lp_d2_service_harness import ServiceChild

    class PruneChild(ServiceChild):
        def launch(self, scenario, *, expected_running=None):
            # Commit only this helper into the disposable selected release.
            shutil.copyfile(Path(__file__), self.release_root/'tests'/Path(__file__).name)
            for args in (['add', 'tests/'+Path(__file__).name],
                    ['-c', 'user.name=CI prune fixture', '-c', 'user.email=fixture@invalid',
                     'commit', '-qm', 'actual prune assertion helper']):
                subprocess.run(['git', '-C', str(self.release_root), *args], check=True, capture_output=True)
            self.sha=subprocess.check_output(['git', '-C', str(self.release_root), 'rev-parse', 'HEAD'], text=True).strip()
            return super().launch(scenario, expected_running=expected_running)

    with PruneChild(tmp_path, scenario='consumer-ci-prune-stop') as child:
        child.release('metadata')
        child.start(expected_running=None)
        child.wait('ci-prune-assertions-passed')
        assert child.wait_exit()==78, child.output.read_text()
        result=json.loads(child.marker('ci-prune-observations.json').read_text())
        assert result['state']=='STOPPED' and result['production_owner'] is False
        assert child.actual_posts()==0
        child.assert_owner_available()


def _exercise_actual_runtime_prune_stop(runtime, adapter, monkeypatch, real_connect):
    """Original prune/native/owner assertions against the actual claimed Runtime."""
    import asyncio
    import threading
    from types import SimpleNamespace
    from open_trader import prediction_runtime as runtime_module
    from open_trader.polymarket_monitor import PolymarketMonitor
    from open_trader.predict_cross_venue import PredictCrossVenueMonitor
    with pytest.raises(OSError, match='offline account financial facts unavailable'):
        adapter.lp_reader_source_port().read('balance', SimpleNamespace(account=adapter.config.wallet_address))
    assert runtime.state == 'NOT_READY' and runtime.production_owner is True
    store = runtime.store
    assert isinstance(store, PredictionArbitrageStore)
    source_started, source_cancelled = threading.Event(), threading.Event()
    cancellations = []

    class Source:
        async def list_open_markets(self):
            source_started.set()
            try:
                await asyncio.Future()
            finally:
                cancellations.append('actual-cancellation')
                source_cancelled.set()

        def snapshot(self):
            return {'rest': 'ready', 'ws': 'ready'}

    monitor = PolymarketMonitor(store=store, trading=adapter)
    runtime.monitor = monitor
    cross = runtime_module._CrossVenueRuntime(PredictCrossVenueMonitor(
        predict_source=Source(), polymarket_monitor=monitor, validator=object(),
        gamma_lookup=lambda *args, **kwargs: ()))
    runtime._cross_runtime = cross
    cross.start()
    assert source_started.wait(5)
    with real_connect(store.path) as connection:
        connection.executemany('INSERT INTO lp_market_metadata_cache VALUES (?,?,?,1)',
            [(f'expired-{index}', '{}', T.timestamp()-1) for index in range(401)])
    at_commit, release_commit, committed = threading.Event(), threading.Event(), threading.Event()

    class CommitGate(sqlite3.Connection):
        metadata_delete = False

        def execute(self, sql, parameters=()):
            if sql.lstrip().startswith('DELETE FROM lp_market_metadata_cache'):
                self.metadata_delete = True
            if sql == 'COMMIT' and self.metadata_delete:
                at_commit.set()
                assert release_commit.wait(20), 'independent DELETE commit watchdog expired'
            result = super().execute(sql, parameters)
            if sql == 'COMMIT' and self.metadata_delete:
                committed.set()
            return result

    def connect(*args, **kwargs):
        kwargs['factory'] = CommitGate
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    errors, stopped = [], threading.Event()

    def stop():
        try:
            runtime.stop()
        except BaseException as exc:
            errors.append(exc)
        finally:
            stopped.set()

    stopper = threading.Thread(target=stop, name='prune-runtime-stop-349')
    contender = runtime_module._RuntimeOwnershipLock(store.path.parent / 'runtime.lock')
    try:
        assert adapter.lp_market_metadata_batch((A,))['state'] == 'known'
        assert at_commit.wait(5)
        assert adapter.lp_metadata_persistence_status()['pending_entries'] == 1
        stopper.start()
        status = _wait_status(adapter, lambda value: value['dropped_shutdown'] == 1)
        assert status['pending_entries'] == status['inflight_entries'] == 0
        assert status['prune_inflight'] == 1
        assert source_cancelled.wait(2), 'metadata collection blocked actual producer cancellation'
        assert not release_commit.is_set()
        assert stopped.wait(7)
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
        assert runtime.state == 'STOPPING'
        assert runtime.store is store and runtime._prediction_trading is adapter
        assert runtime.production_owner is True
        with pytest.raises(runtime_module.PredictionRuntimeOwnershipError):
            contender.acquire()
        assert adapter.lp_metadata_persistence_status()['thread_alive'] is True
        assert not cross.thread_alive
        release_commit.set()
        assert committed.wait(5)
        _wait_status(adapter, lambda value: not value['thread_alive'])
        runtime.stop()
        assert runtime.state == 'STOPPED' and runtime.production_owner is False
        assert runtime._prediction_trading is None
        assert cancellations == ['actual-cancellation']
        with real_connect(store.path) as connection:
            assert connection.execute('SELECT count(*) FROM lp_market_metadata_cache').fetchone()[0] == 1
            assert connection.execute('SELECT 1 FROM lp_market_metadata_cache WHERE condition_id=?', (A,)).fetchone() is None
        contender.acquire()
        contender.release()
        runtime.stop()
        assert cancellations == ['actual-cancellation']
    finally:
        release_commit.set()
        if stopper.ident is not None:
            stopper.join(timeout=7)
        runtime.stop()
        adapter.close()
        cross.stop()
        monitor.stop()
        assert not stopper.is_alive()


@pytest.mark.parametrize('missing_detail', ('planning-refresh', 'planning-restart', 'display-eviction'))
def test_full_metadata_transport_failure_never_returns_core_only_payload(tmp_path, monkeypatch, missing_detail):
    from datetime import timedelta
    from decimal import Decimal
    clock, fail = [T], [False]

    def rows(ids):
        if fail[0]:
            raise RuntimeError('offline transport')
        return tuple({'id': '349', 'condition_id': cid, 'question': 'Complete literal title',
            'market_url': 'https://example.invalid/literal', 'prices': {'one_day_price_change': '0.12'},
            'events': [], 'outcomes': {'yes': {'token_id': 'yes-current', 'label': 'YES'},
                                     'no': {'token_id': 'no-current', 'label': 'NO'}}} for cid in ids)

    store = PredictionArbitrageStore(tmp_path) if missing_detail == 'planning-restart' else None
    policy = {'display_max_entries': 1} if missing_detail == 'display-eviction' else None
    adapter = _adapter(store, monkeypatch, rows=rows, clock=clock, metadata_cache_policy=policy)
    try:
        complete = adapter.lp_market_metadata((A,))[A]
        assert complete['market_title'] == 'Complete literal title'
        assert complete['market_url'] == 'https://example.invalid/literal'
        assert complete['price_change_24h'] == Decimal('0.12')
        if missing_detail == 'display-eviction':
            adapter.lp_market_metadata((B,))
        else:
            clock[0] = T + timedelta(seconds=61)
            narrow = adapter.lp_market_metadata_batch((A,), planning_only=True)
            assert narrow['state'] == 'known'
            assert not {'market_title', 'market_url', 'price_change_24h', 'price_change_24h_source'} & narrow['markets'][A].keys()
            if missing_detail == 'planning-restart':
                _wait_status(adapter, lambda status: status['saved_entries'] == 2)
                adapter.close()
                adapter = _adapter(store, monkeypatch, rows=rows, clock=clock)
        fail[0] = True
        failed = adapter.lp_market_metadata_batch((A,))
        assert failed['markets'] == {}, 'core-only cache rows cannot satisfy the complete API'
        assert failed['state'] == 'unknown'
        assert failed['confirmed_absent_ids'] == ()
        assert failed['failed_ids'] == {A: 'market_read_RuntimeError'}
        with pytest.raises(RuntimeError, match='market read failed'):
            adapter.lp_market_metadata((A,))
    finally:
        adapter.close()

    # Complete stale values retain the legacy diagnostic/mapping fallback.
    clock[0], fail[0] = T, False
    control = _adapter(None, monkeypatch, rows=rows, clock=clock)
    try:
        original = control.lp_market_metadata((A,))[A]
        clock[0], fail[0] = T + timedelta(seconds=43201), True
        stale = control.lp_market_metadata_batch((A,))
        assert stale['markets'] == {A: original}
        assert stale['markets'][A]['metadata_checked_at'] == stale['markets'][A]['fees_checked_at'] == T
        assert stale['failed_ids'] == {A: 'market_read_RuntimeError'}
        assert stale['confirmed_absent_ids'] == ()
        assert control.lp_market_metadata((A,)) == {A: original}
    finally:
        control.close()
