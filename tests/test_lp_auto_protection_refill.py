"""Repeated protection/refill through the real adapter and isolated SQLite."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event, local
import time
from types import SimpleNamespace
from datetime import datetime

import pytest
from polymarket.models.clob.order_book import OrderBookLevel

from open_trader import polymarket_trading
from open_trader.polymarket_lp import LpObservationWait
from tests.test_lp_account_reservation_reconciliation import runtime, _advance, _refill_identity
from tests.test_lp_auto_refill_contract import RefillPublic, prepare


class ProtectionPublic(RefillPublic):
    def __init__(self, clock):
        super().__init__(clock)
        self.depleted_tokens = set()

    def get_order_book(self, *, token_id):
        book = super().get_order_book(token_id=token_id)
        if token_id in self.depleted_tokens:
            # Baseline front is 1000 shares before submission. A 3000-share level
            # makes the conservative front ratio fall below the real 0.5 gate.
            book = book.model_copy(update={'bids': (
                OrderBookLevel(price=Decimal('.40'), size=Decimal('3000')),
                book.bids[1],
            )})
        return book


@pytest.mark.parametrize('dependency', ['healthy', 'timeout', 'funds', 'qualification', 'concurrent-publication'])
def test_two_protection_cycles_refill_from_three_api_buys(runtime, monkeypatch, record_property, dependency):
    initial_clock = runtime.clock[0]
    monkeypatch.setattr(polymarket_trading, 'time', SimpleNamespace(
        monotonic=lambda: time.monotonic() + (runtime.clock[0] - initial_clock).total_seconds(),
        time=time.time, sleep=time.sleep,
    ))
    public = ProtectionPublic(runtime.clock)
    store, adapter, account, lp, execution, _ = prepare(runtime, public=public)
    post = account.post_order
    orders_by_id = {}
    active_trace = None
    read_role = local()
    shared_reader = adapter.lp_account_snapshot_shared

    def request_record(args, kwargs):
        return dict(max_age=args[0] if args else kwargs.get('max_age_seconds'),
                    generation_provider_matches=kwargs.get('trade_generation_provider') == store.lp_trade_generation,
                    posts_before=len(account.posts), sdk_before=account.position_reads)

    def observed_shared_reader(*args, **kwargs):
        record = request_record(args, kwargs)
        if active_trace is not None:
            destination = 'publishers' if getattr(read_role, 'publisher', False) else 'requests'
            active_trace[destination].append(record)
        try:
            result = shared_reader(*args, **kwargs)
            record['successful'] = True
            return result
        finally:
            record['sdk_after'] = account.position_reads

    adapter.lp_account_snapshot_shared = observed_shared_reader

    def start_trace():
        return dict(requests=[], publishers=[], posts=[], posts_before=len(account.posts),
                    sdk_before=account.position_reads)

    def advance_to_due():
        waiting = execution.lp_auto_state().get('plan_wait')
        if waiting:
            runtime.clock[0] = max(runtime.clock[0], datetime.fromisoformat(waiting['deadline']))
        _advance(runtime)

    def assert_fence_accounting(trace, *, invalidated=False):
        requests = trace['requests']
        posts_before = trace['posts_before']
        assert len(requests) == 6, 'Initial, two mandatory fresh requests per BUY, final refresh'
        assert all(r['max_age'] == 0 and r['generation_provider_matches'] for r in requests)
        assert [r['posts_before'] for r in requests] == [
            posts_before, posts_before, posts_before, posts_before + 1, posts_before + 1, posts_before + 2]
        assert [p['request_count'] for p in trace['posts']] == [3, 5]
        assert [p['posts_before'] for p in trace['posts']] == [posts_before, posts_before + 1]
        assert [p['sdk_reads'] for p in trace['posts']] == [requests[2]['sdk_after'], requests[4]['sdk_after']]
        successful = requests[1:] if invalidated else requests
        assert all(r.get('successful') is True and r['sdk_after'] - r['sdk_before'] == 1 for r in successful), (
            'Every successful mandatory age-zero request reads current SDK facts once')
        if invalidated:
            assert requests[0].get('invalidated') is True and not requests[0].get('successful')
            assert len(trace['publishers']) == 1
            publisher = trace['publishers'][0]
            assert publisher['sdk_after'] - publisher['sdk_before'] == 1
            assert requests[0]['sdk_after'] - requests[0]['sdk_before'] == 1, (
                'Only the independent publisher reads while the initial request waits')
        else:
            assert not trace['publishers']
        # Extra observations may inspect three existing orders before admission.
        # They cannot replace either mandatory fence, occur between BUY fences,
        # or substitute for the final refresh.
        extra = requests[1]['sdk_before'] - requests[0]['sdk_after']
        assert 0 <= extra <= 3
        for previous, current in zip(requests[1:], requests[2:]):
            assert current['sdk_before'] == previous['sdk_after']
        baseline = requests[0]['sdk_after'] if invalidated else trace['sdk_before']
        mandatory = 5 if invalidated else 6
        assert account.position_reads - baseline == mandatory + extra
        assert mandatory <= account.position_reads - baseline <= mandatory + 3

    def venue_post(signed):
        if active_trace is not None:
            active_trace['posts'].append(dict(posts_before=len(account.posts), sdk_reads=account.position_reads,
                                              request_count=len(active_trace['requests'])))
        receipt = post(signed)
        order = account.orders[-1].model_copy(update={'id': f'cycle-{len(account.posts)}'})
        account.orders = (*account.orders[:-1], order)
        orders_by_id[order.id] = order
        return {**receipt, 'order_id': order.id}

    def venue_cancel(*, order_ids):
        account.cancels.append(order_ids)
        for oid in order_ids:
            orders_by_id[oid] = orders_by_id[oid].model_copy(update={'status': 'CANCELED'})
        account.orders = tuple(o for o in account.orders if o.id not in order_ids)
        return {'canceled': order_ids, 'not_canceled': {}}

    account.post_order = venue_post
    account.cancel_orders = venue_cancel
    account.get_order = lambda *, order_id: orders_by_id[order_id]

    def settle_workers():
        attention = lp._attention_thread
        if attention is not None:
            attention.join(5)
            assert not attention.is_alive(), 'Independent attention cleanup watchdog'
        assert lp._attention_thread is None
        with adapter._lp_public_reads_lock:
            reads = tuple(adapter._lp_public_reads.values())
        for read in reads:
            read.result(timeout=5)
        with lp._market_reads_lock:
            assert all(read.done() for read in lp._market_reads.values())
        with adapter._lp_public_client_lock:
            assert adapter._lp_public_readers == 0

    def publish_qualification():
        # Same real SDK qualification publication used by prepare(); this
        # fixture controls dependency availability, not scan scheduling.
        for i in range(1, 6):
            market, condition, token = _refill_identity(i)
            facts = lp._read_candidate_facts(dict(market_id=market, condition_id=condition,
                                                  token_id=token, outcome='YES'))
            lp._candidate_pool_record_success(condition, dict(condition_id=condition), judged_at=lp._now(),
                                             facts=dict(directions=[facts['direction']], account=facts['account']))

    try:
        first = execution.lp_auto_run_once(round_id='initial-five')
        assert len(account.orders) == len(account.posts) == 5, first['last_round']
        assert first['slots']['occupied'] == 5
        for cycle in range(2):
            victims = account.orders[-2:]
            for victim in victims:
                public.depleted_tokens = {victim.token_id}
                _advance(runtime)
                session = next(s for s in store.lp_sessions() if s.get('entry_order_id') == victim.id)
                # Consume any prior completed public read, then await the new
                # nonblocking read. The monitor still uses its normal path.
                adapter.lp_snapshot(session)
                adapter.lp_snapshot({**session, 'lp_public_wait': False})
                key = f"{session['condition_id']}\0{session['token_id']}"
                with adapter._lp_public_reads_lock:
                    ready = adapter._lp_public_reads[key]
                ready.result(timeout=5)  # Independent real-time I/O watchdog.
                execution.lp_tick()
                settle_workers()
            assert len(account.orders) == 3, [lp.status(s['session_id']) for s in store.lp_sessions()]
            assert set(account.cancels[-2:]) == {(o.id,) for o in victims}
            actions = [a for s in store.lp_sessions() for a in store.lp_actions(s['session_id'])]
            for victim in victims:
                assert any(a['role'] == 'entry-protection-cancel' and a['state'] == 'accepted'
                           and a['reason'] == 'queue_ahead_ratio' and victim.id in a['targets'] for a in actions)
            public.depleted_tokens.clear()
            _advance(runtime)
            execution.lp_tick()  # Confirm terminal venue receipts after both sends.
            settle_workers()
            assert execution.refresh_lp_dashboard_snapshot()['state'] == 'ready'
            after_cancel = execution.lp_auto_state()
            assert after_cancel['slots']['occupied'] == 3, after_cancel
            assert Decimal(after_cancel['funds']['buy_reserved_usd']) == 24
            lp.refresh_candidate_recommendations()
            posts_before = len(account.posts)
            if dependency == 'concurrent-publication':
                # Make the previously successful private account cache stale.
                # Real candidate maintenance publishes fresh ranking facts.
                _advance(runtime, 61)
                lp.refresh_candidate_recommendations()
                reader = adapter.lp_account_snapshot_shared
                entered, release = Event(), Event()
                calls = []
                before_publication = account.position_reads
                trace = start_trace()
                active_trace = trace
                def interrupted(*args, **kwargs):
                    calls.append(1)
                    if len(calls) == 1:
                        record = request_record(args, kwargs)
                        trace['requests'].append(record)
                        entered.set()
                        assert release.wait(5), 'Independent publication watchdog'
                        record.update(invalidated=True, sdk_after=account.position_reads)
                        raise LpObservationWait('account_round_invalid')
                    return reader(*args, **kwargs)
                with monkeypatch.context() as race:
                    race.setattr(adapter, 'lp_account_snapshot_shared', interrupted)
                    with ThreadPoolExecutor(1) as workers:
                        pending = workers.submit(execution.lp_auto_run_once, round_id=f'refill-{cycle}')
                        try:
                            assert entered.wait(5)
                            assert account.position_reads == before_publication, 'Invalidated initial wait has no SDK I/O'
                            _advance(runtime)
                            # Publish a separate complete read through the same
                            # public registration seam used by the dashboard.
                            read_role.publisher = True
                            try:
                                fresh = reader(max_age_seconds=0, trade_generation_provider=store.lp_trade_generation)
                            finally:
                                read_role.publisher = False
                            assert account.position_reads - before_publication == 1, 'Independent publisher reads the SDK once'
                            after_publication = account.position_reads
                            assert lp.register_account_snapshot(fresh)['state'] == 'registered'
                        finally:
                            release.set()
                        result = pending.result(timeout=5)
                assert result['funds']['status'] == 'known', result
                assert result['admission_block_reasons'] == [], result
                assert len(account.orders) == 3 and len(account.posts) == trace['posts_before']
                assert result['plan_wait']['kind'] == 'api'
                assert len(calls) == 1
                assert len(trace['requests']) == len(trace['publishers']) == 1
                assert trace['requests'][0]['invalidated'] is True
                assert trace['requests'][0]['sdk_after'] == trace['requests'][0]['sdk_before'] + 1
                assert account.position_reads == after_publication
                early = execution.lp_auto_scheduled_check()
                assert early['plan_wait'] == result['plan_wait']
                assert len(account.posts) == trace['posts_before']
                active_trace = None
                advance_to_due()
                publish_qualification()
                lp.refresh_candidate_recommendations()
                trace = start_trace()
                active_trace = trace
                result = execution.lp_auto_scheduled_check()
                assert len(account.orders) == 5, result['last_round']
                assert_fence_accounting(trace)
                active_trace = None
                assert result['slots']['occupied'] == 5
                assert len(account.posts) == 7 + 2 * cycle
                continue
            if dependency != 'healthy':
                advance_to_due()
                with monkeypatch.context() as blocked:
                    if dependency == 'timeout':
                        def unavailable(**kwargs):
                            raise TimeoutError('offline account dependency unavailable')
                        blocked.setattr(account, 'list_positions', unavailable)
                    elif dependency == 'funds':
                        blocked.setattr(account, 'get_balance_allowance', lambda **kwargs: SimpleNamespace(
                            balance='0', allowances={account.environment.standard_exchange: '100000000'}))
                    else:
                        public.sizes = {i: '10000' for i in range(1, 7)}
                        publish_qualification()
                    limited = execution.lp_auto_run_once(round_id=f'limited-{dependency}-{cycle}')
                    assert len(account.posts) == posts_before, limited['last_round']
                    assert len(account.orders) == 3
                    if dependency == 'timeout':
                        assert limited['last_round']['reason'] == 'account_order_sync_unknown'
                        assert limited['admission_block_reasons']
                    else:
                        assert limited['last_round']['reason'] == 'candidates_or_funds_insufficient'
                        assert limited['last_round']['candidate_count'] == 0, limited['last_round']
                public.sizes.clear()
                _advance(runtime, 61)
                if dependency == 'qualification':
                    publish_qualification()
                lp.refresh_candidate_recommendations()
            advance_to_due()
            # This fixture has no background candidate scanner. The added
            # business waits can expire its initially published candidate pool.
            publish_qualification()
            before = account.position_reads
            trace = start_trace()
            active_trace = trace
            result = execution.lp_auto_run_once(round_id=f'refill-{cycle}')
            assert len(account.orders) == 5, dict(reason=result['last_round']['reason'],
                candidates=result['last_round']['candidate_count'],
                filters={key: result['last_round']['candidate_filter'].get(key) for key in
                         ('counts', 'reasons', 'recheck_reasons')})
            assert len(account.posts) == 7 + 2 * cycle
            assert result['slots']['occupied'] == 5
            assert result['funds']['status'] == 'known'
            assert_fence_accounting(trace)
            assert 6 <= account.position_reads - before <= 9
            active_trace = None
    finally:
        try:
            settle_workers()
            record_property('cleanup', 'attention joined; public futures completed; market futures done; public readers zero')
        finally:
            adapter.close()
            assert adapter._lp_public_closed is True
            record_property('adapter_closed', 'true')
