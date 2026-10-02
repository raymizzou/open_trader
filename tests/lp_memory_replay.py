"""Offline, fixed-clock LP business replay; also runnable in either SHA's image.

PYTHONPATH=src:tests python tests/lp_memory_replay.py --markets 18000 --rounds 3
No SDK clients, credentials, network or trading actions are used.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import resource
import sys
import tempfile
import threading
import time

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_polymarket_lp import (
    _LPCandidateQueryExchange, _LPRollingPoolExchange, _LPBatchQueryExchange,
    _seed_stale_backup_summaries,
)


class MetadataRetryExchange(_LPCandidateQueryExchange):
    """One metadata failure becomes due while bounded history work continues."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed_once = self.retried = self.advanced = False
        self.clock_lock = threading.Lock()

    def lp_market_metadata_batch(self, requested, *, stop_event=None):
        markets = self.lp_market_metadata(requested, stop_event=stop_event)
        target = 'condition-M00000'
        failed = {}
        if target in requested:
            if not self.failed_once:
                self.failed_once = True
                markets.pop(target)
                failed[target] = 'IncompleteRead'
            else:
                self.retried = True
        return {'state': 'known', 'markets': markets, 'failed_ids': failed}

    def lp_price_history(self, *args, **kwargs):
        with self.clock_lock:
            if self.failed_once and not self.advanced:
                self.now += timedelta(seconds=300)
                self.advanced = True
        return super().lp_price_history(*args, **kwargs)


def ranking_trace(root):
    exchange = _LPRollingPoolExchange(datetime(2026, 9, 20, 8, tzinfo=UTC))
    service = PolymarketLPService(PredictionArbitrageStore(root), exchange, clock=lambda: exchange.now)
    service.refresh_price_history()
    service.refresh_competition_cache()
    trace = [service.refresh_candidates()]
    exchange.now += timedelta(seconds=65)
    exchange.pool_override = {"condition-M01": Decimal("24.0")}
    trace.append(service.refresh_candidate_recommendations())
    exchange.now += timedelta(seconds=65)
    exchange.string_rules_conditions = frozenset({"condition-M02"})
    trace.append(service.refresh_candidate_recommendations())
    exchange.now += timedelta(seconds=65)
    exchange.omit_conditions = frozenset({"condition-M03"})
    trace.append(service.refresh_candidate_recommendations())
    exchange.now += timedelta(seconds=301)
    trace.append(service.candidate_snapshot())
    exchange.omit_conditions = exchange.string_rules_conditions = frozenset()
    service.refresh_price_history()
    service.refresh_competition_cache()
    trace.append(service.refresh_candidates())
    # Compare the complete published contract, including reasons, ordering,
    # timestamps, replacement rows, counts and missing/UNKNOWN evidence.
    return json.loads(json.dumps(trace, default=str))


def backfill_trace(root):
    """Traverse >10 normal and >10 backup rows with rejection/UNKNOWN holes."""
    now = datetime(2026, 9, 20, 8, tzinfo=UTC)
    backups = tuple(f'B{i:02}' for i in range(12))
    exchange = _LPBatchQueryExchange(
        now, {suffix: Decimal(100) for suffix in (*[f'N{i:02}' for i in range(14)], *backups)},
        backup=frozenset(backups), reject=frozenset({'N00', 'B00'}),
        omit_tokens=frozenset({'token-condition-N01-yes', 'token-condition-N01-no'}),
    )
    store = PredictionArbitrageStore(root)
    _seed_stale_backup_summaries(store, now, backups)
    service = PolymarketLPService(store, exchange, clock=lambda: exchange.now)
    service.refresh_price_history()
    service.refresh_competition_cache()
    trace = []
    for _ in range(3):
        trace.append(service.refresh_candidates())
        exchange.now += timedelta(seconds=5)
    assert len({token for batch in exchange.book_token_reads for token in batch}) == 52
    assert trace[-1]['candidate_pending_count'] == 0
    return json.loads(json.dumps(trace, default=str))


def measure(markets, rounds, retained=40, retry_metadata=False):
    class Universe(MetadataRetryExchange if retry_metadata else _LPCandidateQueryExchange):
        def lp_market_competitiveness(self, **kwargs):
            result = super().lp_market_competitiveness(**kwargs)
            result['competitiveness'] = {
                key: (value if index < retained else (Decimal(0), self.now))
                for index, (key, value) in enumerate(result['competitiveness'].items())
            }
            return result

    exchange = Universe(
        datetime(2026, 9, 20, 8, tzinfo=UTC),
        {f"M{i:05}": Decimal("57.6") for i in range(markets)},
    )
    samples = []
    with tempfile.TemporaryDirectory() as directory:
        service = PolymarketLPService(PredictionArbitrageStore(Path(directory)), exchange, clock=lambda: exchange.now)
        for index in range(rounds):
            started = time.monotonic()
            prepared = service.refresh_price_history()
            assert prepared["state"] == "known"
            if retry_metadata:
                assert exchange.retried and service.store.lp_preparation_items() == []
            service.refresh_competition_cache()
            # Read every candidate, not only the first displayed batch.
            before = len(exchange.book_token_reads)
            expected_tokens = {f"token-condition-M{i:05}" for i in range(min(markets, retained))}
            for _ in range(max(1, retained + 1)):
                result = service.refresh_candidates()
                read_tokens = {token for batch in exchange.book_token_reads[before:] for token in batch}
                if expected_tokens <= read_tokens:
                    break
            else:
                raise AssertionError('a complete candidate traversal did not finish')
            assert result['candidate_valid_count'] == min(markets, retained)
            snapshot = json.dumps(result, default=str, sort_keys=True)
            sample = {"round": index, "seconds": time.monotonic() - started,
                      "markets": markets, "retained": retained,
                      "candidate_count": result["candidate_valid_count"],
                      "history_count": prepared["target_count"],
                      "traversed_tokens": len(read_tokens),
                      "business_sha256": hashlib.sha256(snapshot.encode()).hexdigest(),
                      "peak_rss_platform_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
            if Path('/proc/self/status').exists():
                sample.update({line.split(':', 1)[0]: line.split(':', 1)[1].strip()
                               for line in Path('/proc/self/status').read_text().splitlines()
                               if line.startswith(('VmRSS:', 'VmHWM:'))})
            for key, names in {
                'cgroup_current_bytes': ('memory.current', 'memory/memory.usage_in_bytes'),
                'cgroup_peak_bytes': ('memory.peak', 'memory/memory.max_usage_in_bytes'),
            }.items():
                counter = next((Path('/sys/fs/cgroup')/name for name in names
                                if (Path('/sys/fs/cgroup')/name).exists()), None)
                sample[key] = int(counter.read_text()) if counter else None
            sample['peak_rss_bytes'] = sample['peak_rss_platform_units'] * (1 if sys.platform == 'darwin' else 1024)
            print(json.dumps(sample), flush=True)
            assert sample['peak_rss_bytes'] <= 1_000_000_000, 'process peak exceeded 1GB'
            assert (sample['cgroup_peak_bytes'] or sample['cgroup_current_bytes'] or 0) <= 1_000_000_000, 'observed cgroup usage exceeded 1GB'
            samples.append(sample)
            exchange.now += timedelta(hours=1)
    return samples


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--markets', type=int, default=18000)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--retained', type=int, default=40)
    parser.add_argument('--baseline', type=Path, help='compare every full-round business hash with baseline JSONL')
    parser.add_argument('--retry-metadata', action='store_true')
    args = parser.parse_args()
    samples = measure(args.markets, args.rounds, args.retained, args.retry_metadata)
    if args.baseline:
        baseline = [json.loads(line) for line in args.baseline.read_text().splitlines()]
        for before, after in zip(baseline, samples, strict=True):
            for key in ('round', 'markets', 'retained', 'candidate_count', 'history_count', 'business_sha256'):
                assert before[key] == after[key], (key, before[key], after[key])
