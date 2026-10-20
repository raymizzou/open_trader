"""Committed history becomes usable before the remaining catalog finishes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
import threading

import pytest

from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from test_polymarket_lp import _LPCandidateQueryExchange


@pytest.mark.parametrize('publish_during_build', [False, True])
def test_completed_history_batch_becomes_candidate_before_full_catalog_finishes(tmp_path, publish_during_build):
    first_started, first_release = threading.Event(), threading.Event()
    second_started, second_release = threading.Event(), threading.Event()
    build_started, build_release = threading.Event(), threading.Event()
    committed = threading.Event()
    stop = threading.Event()
    now = datetime(2026, 10, 6, tzinfo=UTC)

    class Exchange(_LPCandidateQueryExchange):
        def lp_price_history(self, token_ids, **kwargs):
            if 'token-condition-0' in token_ids:
                first_started.set()
                assert first_release.wait(10)
            else:
                second_started.set()
                assert second_release.wait(10)
            return super().lp_price_history(token_ids, **kwargs)

    exchange = Exchange(now, {str(i): Decimal(100) for i in range(21)})
    store = PredictionArbitrageStore(tmp_path)
    service = PolymarketLPService(store, exchange, clock=lambda: now)
    save_progress = service._save_preparation

    def progress(patch, **kwargs):
        result = save_progress(patch, **kwargs)
        if patch.get('completed_count', 0) > 0:
            committed.set()
        return result

    service._save_preparation = progress
    summaries = store.lp_price_history_summary_batches

    def read_summaries(identities, **kwargs):
        for result in summaries(identities, **kwargs):
            if publish_during_build and len(identities) == 21 and not build_started.is_set():
                build_started.set()
                assert build_release.wait(10)
            yield result

    store.lp_price_history_summary_batches = read_summaries
    service.refresh_competition_cache()
    with ThreadPoolExecutor(max_workers=2) as workers:
        job = workers.submit(service.refresh_price_history, stop_event=stop)
        scan = None
        try:
            assert first_started.wait(10)
            scan = workers.submit(service.refresh_candidates)
            if publish_during_build:
                assert build_started.wait(10)
            else:
                assert scan.result(timeout=10)['candidates'] == []
            first_release.set()
            assert second_started.wait(10)
            assert committed.wait(10)
            assert store.lp_price_history_summary('condition-0', 'token-condition-0', now=now)['state'] == 'known'
            assert service.preparation_snapshot()['state'] == 'preparing'
            assert not job.done()
            build_release.set()
            assert scan.result(timeout=10)['candidates'] == []
            result = service.refresh_candidates()
            assert len(result['candidates']) > 0
            assert all(row['condition_id'] != 'condition-20' for row in result['candidates'])
        finally:
            stop.set()
            first_release.set()
            second_release.set()
            build_release.set()
            job.result(timeout=10)
            if scan is not None:
                scan.result(timeout=10)


from tests.test_lp_account_reservation_reconciliation import runtime


def test_real_adapter_sqlite_history_progress_recovers_cached_empty_queue(runtime):
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace
    from tests.test_lp_account_reservation_reconciliation import _FiveMarketPublic, _refill_identity
    from tests.test_lp_order_registration_contract import TOKEN_ID

    first_started, first_release = threading.Event(), threading.Event()
    later_started, later_release = threading.Event(), threading.Event()
    committed = threading.Event()
    stop = threading.Event()

    class Public(_FiveMarketPublic):
        def list_markets(self, **kwargs):
            return [self.get_market(id=f'market-{i}') for i in range(1, 22)]
        def list_market_rewards(self, *, condition_id, sponsored):
            reward = super().list_market_rewards(condition_id=condition_id, sponsored=sponsored)[0]
            index = next(i for i in range(1, 22) if _refill_identity(i)[1] == condition_id)
            config = reward.rewards_config[0].model_copy(update={'id': index})
            return (reward.model_copy(update={'rewards_config': (config,)}),)
        def list_current_rewards(self, *, sponsored):
            if sponsored:
                return ()
            return tuple(self.list_market_rewards(condition_id=_refill_identity(i)[1], sponsored=False)[0]
                         for i in range(1, 22))
        def get_order_book(self, *, token_id):
            index = next(i for i in range(1, 22)
                         if token_id in {_refill_identity(i)[2], f'0x{i + 300:064x}'})
            template = super().get_order_book(token_id=TOKEN_ID)
            return template.model_copy(update={'token_id': token_id,
                'market': _refill_identity(index)[1], 'condition_id': _refill_identity(index)[1]})

    store, adapter, account, lp, _ = runtime(public_client=Public(runtime.clock))
    @contextmanager
    def histories(request, **kwargs):
        body = json.loads(request.data)
        if TOKEN_ID in body['markets']:
            first_started.set()
            assert first_release.wait(10), 'Independent first-batch watchdog'
        else:
            later_started.set()
            assert later_release.wait(10), 'Independent later-batch watchdog'
        yield SimpleNamespace(read=lambda: json.dumps({'history': {
            token: [dict(t=body['start_ts'], p='0.40'), dict(t=body['end_ts'], p='0.401')]
            for token in body['markets']}}).encode())
    adapter._urlopen_fn = histories
    writer = store.lp_save_price_history_batch
    def committed_batch(*args, **kwargs):
        result = writer(*args, **kwargs)
        committed.set()
        return result
    store.lp_save_price_history_batch = committed_batch
    store.lp_competitiveness_upsert((_refill_identity(i)[1], Decimal('2.5'), runtime.clock[0])
                                   for i in range(1, 22))
    with ThreadPoolExecutor(1) as workers:
        job = workers.submit(lp.refresh_price_history, stop_event=stop)
        try:
            assert first_started.wait(10), job.result() if job.done() else "inflight"
            assert lp.refresh_candidates()['candidates'] == []
            first_release.set()
            assert later_started.wait(10)
            assert committed.wait(10)
            assert not job.done()
            result = lp.refresh_candidates()
            assert result['candidates'], result
            ready = {row['condition_id'] for row in result['candidates']}
            assert _refill_identity(21)[1] not in ready
            assert lp._candidate_qualification_facts
            assert account.posts == account.cancels == []
        finally:
            stop.set()
            first_release.set()
            later_release.set()
            job.result(timeout=10)
