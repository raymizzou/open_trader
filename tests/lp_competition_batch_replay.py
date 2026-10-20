"""Offline #306 native refresh replay, with public derived input and full-pool hashes.

Run in an isolated process with PYTHONPATH selecting baseline or worktree source.
No credentials, SDK client, network, service, or trading operations are used.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import resource
import shutil
import sqlite3
from threading import Event, Thread, current_thread, local
import time
import tempfile
from urllib.parse import parse_qs, urlsplit

from open_trader import polymarket_lp, polymarket_trading, polymarket_lp_views
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.polymarket_trading import PolymarketTradingClient
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_polymarket_lp_views import _trial_direction

NOW = datetime(2026, 10, 10, 8, tzinfo=UTC)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def replay(args):
    rows = [json.loads(line) for line in args.input.read_text().splitlines()]
    updates = rows[:128500]
    ids = json.loads(args.candidates.read_text())
    assert len(rows) == 130713 and len(updates) == 128500
    polymarket_trading.datetime = FixedDatetime
    polymarket_lp._LP_COMPETITION_BATCH_SIZE = args.batch
    polymarket_lp._LP_COMPETITION_BATCH_PAUSE_SECONDS = args.pause
    with tempfile.TemporaryDirectory(prefix='issue306-replay-') as temporary:
        root = Path(temporary)
        path = root / 'prediction_arbitrage' / 'prediction_arbitrage.sqlite3'
        path.parent.mkdir()
        shutil.copyfile(args.initial_db, path)
        store = PredictionArbitrageStore(root)
        initial_hash = digest(store.lp_competitiveness_map())
        begun, waiter, finished = Event(), Event(), Event()
        transactions, failures, history_durations = [], [], []
        timing = local()
        def observe(method):
            def call(*args, **kwargs):
                timing.method_started = time.monotonic()
                return method(*args, **kwargs)
            return call
        store.lp_competitiveness_upsert = observe(store.lp_competitiveness_upsert)
        store.lp_save_price_history_batch = observe(store.lp_save_price_history_batch)
        open_connection = store._connection
        class TimedConnection:
            def __init__(self, connection):
                self.connection = connection
                self.row = {'thread': current_thread().name, 'created': time.monotonic(),
                            'method_started': getattr(timing, 'method_started', time.monotonic())}
            def __getattr__(self, name):
                return getattr(self.connection, name)
            def execute(self, sql, *values):
                if sql.startswith('BEGIN IMMEDIATE'):
                    self.row['begin'] = time.monotonic()
                    if self.row['thread'] == 'history':
                        waiter.set()
                    try:
                        result = self.connection.execute(sql, *values)
                    except BaseException as exc:
                        self.row['failure'] = type(exc).__name__
                        raise
                    self.row['acquired'] = time.monotonic()
                    if self.row['thread'] == 'competition':
                        begun.set()
                    return result
                if sql == 'COMMIT':
                    self.row['body_end'] = time.monotonic()
                    result = self.connection.execute(sql, *values)
                    self.row['commit_end'] = time.monotonic()
                    return result
                return self.connection.execute(sql, *values)
            def executemany(self, sql, values):
                if self.row['thread'] == 'competition' and not waiter.is_set():
                    assert waiter.wait(10), 'history writer did not attempt BEGIN'
                return self.connection.executemany(sql, values)
            def close(self):
                start = time.monotonic()
                self.connection.close()
                if 'begin' in self.row:
                    self.row.update(close_seconds=time.monotonic()-start, closed=time.monotonic())
                    transactions.append(self.row)
        store._connection = lambda: TimedConnection(open_connection())
        class Response:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def read(self):
                return json.dumps(self.value).encode()
        pages = []
        def opener(request, **kwargs):
            query = parse_qs(urlsplit(request.full_url).query)
            offset = int(query.get('next_cursor', ['0'])[0])
            pages.append(offset)
            end = min(offset + 500, len(updates))
            return Response({'data': [{'condition_id': cid, 'market_competitiveness': value}
                for cid, value, stamp in updates[offset:end]],
                'next_cursor': str(end) if end < len(updates) else 'LTE='})
        # Construct only the public-reader surface; no SDK clients or signer state.
        exchange = object.__new__(PolymarketTradingClient)
        exchange._urlopen_fn = opener
        service = PolymarketLPService(store, exchange, clock=lambda: NOW)
        def history():
            current_thread().name = 'history'
            assert begun.wait(10), 'competition did not begin'
            for index in range(16):
                sample_rows = [
                    {'condition_id': ids[(index * 100 + n) % len(ids)], 'token_id': f'fixture-{n}',
                     'samples': [{'timestamp': (NOW-timedelta(minutes=k)).isoformat(), 'price': '0.33'} for k in range(60)],
                     'summary': {'state': 'known', 'checked_at': NOW.isoformat()}}
                    for n in range(100)]
                start = time.monotonic()
                try:
                    assert store.lp_save_price_history_batch(sample_rows) == 100
                except BaseException as exc:
                    failures.append({'history_batch': index, 'error': type(exc).__name__, 'message': str(exc)})
                history_durations.append(time.monotonic()-start)
        resources = []
        def sample():
            while not finished.is_set():
                resources.append({'seconds': time.monotonic(),
                    'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    'wal_bytes': Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0})
                finished.wait(0.01)
        sampler = Thread(target=sample, daemon=True)
        sampler.start()
        with ThreadPoolExecutor(max_workers=1) as pool:
            other = pool.submit(history)
            current_thread().name = 'competition'
            start = time.monotonic()
            service.refresh_competition_cache(snapshot=False)
            elapsed = time.monotonic()-start
            other.result(timeout=30)
        finished.set()
        sampler.join(10)
        assert not sampler.is_alive()
        facts = store.lp_competitiveness_map()
        assert len(facts) == 130713
        assert all(facts[cid] == (Decimal(value), NOW) for cid, value, stamp in updates)
        projection = service._competition_entries(ids)
        def directions():
            for index, cid in enumerate(ids):
                # Admission/history/budget fields are controlled fixtures, not cloud account facts.
                row = _trial_direction(str(index))
                row['market']['condition_id'] = cid
                row['history_summary'].update(checked_at=NOW, window_start=NOW-timedelta(hours=24),
                    window_end=NOW, valid_until=NOW+timedelta(hours=24))
                yield row
        trial = polymarket_lp_views.lp_trial_candidates(list(directions()), competition=projection,
            account_budget_facts={'available_capital': Decimal('480')}, now=NOW,
            competition_round_checked_at=NOW)
        def metrics(thread):
            result = []
            for row in transactions:
                if row['thread'] != thread:
                    continue
                result.append({key: value for key, value in {
                    'prepare_and_connection_seconds': row['begin']-row['method_started'],
                    'wait_seconds': row.get('acquired', row['closed'])-row['begin'],
                    'body_seconds': row['body_end']-row['acquired'] if 'body_end' in row else None,
                    'commit_seconds': row['commit_end']-row['body_end'] if 'commit_end' in row else None,
                    'observed_span_seconds': row['closed']-row['acquired'] if 'acquired' in row else None,
                    'close_seconds': row['close_seconds'], 'failure': row.get('failure')}.items()})
            return result
        result = {'mode': args.mode, 'batch': args.batch, 'pause_seconds': args.pause,
            'sqlite_version': sqlite3.sqlite_version, 'input_sha256': hashlib.sha256(args.input.read_bytes()).hexdigest(),
            'candidate_input_sha256': hashlib.sha256(args.candidates.read_bytes()).hexdigest(),
            'initial_db_sha256': hashlib.sha256(args.initial_db.read_bytes()).hexdigest(),
            'initial_facts_sha256': initial_hash, 'updates': len(updates), 'updates_sha256': digest(updates), 'all_facts_count': len(facts),
            'all_facts_sha256': digest(facts), 'complete_projection_sha256': digest(projection),
            'all_candidates_count': len(ids), 'all_candidate_output_sha256': digest(trial),
            'normal_queue_count': len(trial['queue_normal']), 'backup_queue_count': len(trial['queue_backup']),
            'refresh_seconds': elapsed, 'pages': len(pages), 'history_batch_seconds': history_durations,
            'competition_transactions': metrics('competition'), 'history_transactions': metrics('history'),
            'refresh_peak_rss_bytes': max(row['peak_rss_bytes'] for row in resources),
            'process_peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'peak_wal_bytes': max(row['wal_bytes'] for row in resources), 'failures': failures,
            'cgroup_anon_file_total': None, 'dirty_pages': None,
            'limits': 'macOS public-cache-derived 128500 replay; exact cloud input unavailable; full candidate view uses controlled fixtures; no cloud capacity claim'}
        args.output.write_text(json.dumps(result, indent=2)+'\n')
        assert not failures, failures
        print(json.dumps({key: result[key] for key in ('mode','batch','pause_seconds','refresh_seconds','refresh_peak_rss_bytes','failures')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('input', 'candidates', 'initial-db', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--mode', required=True)
    parser.add_argument('--batch', type=int, default=500)
    parser.add_argument('--pause', type=float, default=0.01)
    replay(parser.parse_args())
