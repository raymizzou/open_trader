"""Offline #495 telemetry contracts; external transports only are doubled."""
import json
from decimal import Decimal as D

import pytest

from open_trader.polymarket_lp_orderbook import ShadowDiagnostics
from test_lp_async_read_runtime_contract import Clock

REASONS = ('baseline_absent', 'baseline_incomplete', 'anchor_changed', 'remaining_unknown',
           'demand_filter', 'token_filter', 'invalid_bid_level', 'context_capacity', 'unattributed')


def test_missing_reason_partition_counts_only_actual_missing_reads(tmp_path):
    clock = Clock(); summary = ShadowDiagnostics(clock=clock)
    assert summary.snapshot().get('context_missing_reason_counts') == {}
    assert summary.snapshot().get('context_missing_reason_fractions') == {}
    summary.set_mode('shadow')
    for reason in ('baseline_absent', *REASONS):
        summary.eligible_read(has_context=False, missing_context_reason=reason)
    summary.eligible_read(has_context=True, missing_context_reason={'secret': 'never-public'})
    summary.sample(dict(state='monitoring'), dict(state='monitoring'))
    expected = dict(baseline_absent=2, baseline_incomplete=1, anchor_changed=1,
                    remaining_unknown=1, demand_filter=1, token_filter=1,
                    invalid_bid_level=1, context_capacity=1, unattributed=1)
    for mode in ('off', 'use', 'shadow', 'shadow'):
        summary.set_mode(mode)
        report = summary.snapshot()
        assert report['context_missing_reason_counts'] == expected
        assert report['context_missing_reason_fractions'] == {k: .2 if k == 'baseline_absent' else .1 for k in expected}
        assert report['context_missing_reads'] == 10
        assert report['eligible_rest_reads'] == 11 and report['samples'] == 1
        assert report['unknown_sample_denominator'] == 11 and report['unknown_fraction'] == 10 / 11
        assert report['not_comparable'] == 10
        assert report['unknown_reason_counts'] == {'missing_comparison_context': 10}
    summary.audit_result('different')
    for mode in ('off', 'use', 'shadow'):
        summary.set_mode(mode)
        assert summary.snapshot()['assessment'] == 'FAIL'
    # Offline synthetic evidence only; no live/historical attribution.
    target = tmp_path / 'offline-sample.json'
    target.write_text(json.dumps(dict(offline=True, counts=report['context_missing_reason_counts'],
                                    fractions=report['context_missing_reason_fractions']), indent=2) + '\n')


def test_owner_rejection_classifier_preserves_original_guards():
    from open_trader.polymarket_lp_orderbook import shadow_context_rejection
    levels = ((D('.40'), D(100)),)
    assert shadow_context_rejection(bid_levels=(), bid_level=1,
        has_current_anchor=True, remaining_known=True) == 'baseline_incomplete'
    assert shadow_context_rejection(bid_levels=levels, bid_level=None,
        has_current_anchor=True, remaining_known=True) == 'baseline_incomplete'
    assert shadow_context_rejection(bid_levels=levels, bid_level=1,
        has_current_anchor=False, remaining_known=True) == 'anchor_changed'
    assert shadow_context_rejection(bid_levels=levels, bid_level=1,
        has_current_anchor=True, remaining_known=False) == 'remaining_unknown'
    assert shadow_context_rejection(bid_levels=levels, bid_level=1,
        has_current_anchor=True, remaining_known=True) is None
    assert shadow_context_rejection(bid_levels=(), bid_level=None,
        has_current_anchor=False, remaining_known=False) == 'baseline_incomplete'
    assert shadow_context_rejection(bid_levels=levels, bid_level=1,
        has_current_anchor=False, remaining_known=False) == 'anchor_changed'


@pytest.fixture
def offline_ws(monkeypatch):
    """The actual adapter runs against a socket-free external WS double."""
    import asyncio
    import websockets.asyncio.client
    from test_lp_orderbook_ws_487 import wire_book
    class Socket:
        async def __aenter__(self):
            self.queue = asyncio.Queue()
            return self
        async def __aexit__(self, *args):
            await self.close()
        async def send(self, raw):
            if raw == 'PING':
                await self.queue.put('PONG')
            else:
                row = json.loads(raw)
                if row.get('operation') != 'unsubscribe':
                    for token in row.get('assets_ids', ()):
                        await self.queue.put(json.dumps(wire_book(token=token)))
        def __aiter__(self):return self
        async def __anext__(self):
            value = await self.queue.get()
            if value is None:raise StopAsyncIteration
            return value
        async def close(self):await self.queue.put(None)
    monkeypatch.setattr(websockets.asyncio.client, 'connect', lambda *a, **kw: Socket())


def wait_until(predicate, tick=lambda: None):
    import time
    import threading
    end = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < end, 'independent real-time watchdog'
        tick()
        threading.Event().wait(.005)


def missing_logs(caplog):
    return [json.loads(record.getMessage().split(' ', 1)[1]) for record in caplog.records
            if record.getMessage().startswith('prediction_lp_orderbook_shadow {')
            and 'missing_comparison_context' in record.getMessage()]


def install_baseline(live, clock, *, complete, event):
    from open_trader.polymarket_lp_current_state import (
        ProtectionKey, ProtectionGuard, ProtectionBaseline, SetBaseline, Fact, SourceStamp)
    snapshot = live.state.snapshot()
    order, = snapshot.active_orders
    key = ProtectionKey('queue_bucket', (*order.key[:3], 'group'), D('.40'), None)
    stamp = SourceStamp('offline-book', checked_at=clock.utc(), source_timestamp=clock.utc(),
                        complete=True, state='known')
    baseline = ProtectionBaseline(key, ('A',), D('.40'), Fact('known', D(80), (stamp,)),
        'first_observation', clock.utc(), clock.utc(), None, 1, clock.utc(), D('.5'), stamp,
        1 if complete else None, ((D('.40'), D(80)),) if complete else ())
    guard = ProtectionGuard(key, snapshot.current.cycle_generation, snapshot.scope_versions.get(key, 0),
        ((order.key, order.terms_version),), snapshot.observation.observation_id, event)
    assert live.state.apply_protection(SetBaseline(guard, baseline)) == 'applied'
    return baseline


def test_owner_rejection_provenance_reaches_actual_rest_reads(tmp_path, offline_ws, caplog, monkeypatch):
    from test_lp_orderbook_ws_487 import ws_live
    from test_lp_async_source_contract import CID
    caplog.set_level('INFO')
    clock, boundary, trading, live, store, account = ws_live(tmp_path, mode='shadow')
    store.lp_save_observed_pool_roles(account, expected_revision=0,
        roles={CID: dict(pool='trial', episode='A')})
    runtime = live.read_runtime
    native_calls = []
    def forbidden_native(*args, **kwargs):
        native_calls.append('native-trading-boundary')
        raise AssertionError('shadow telemetry must not submit native orders')
    for name in ('lp_prepare_limit_order', 'lp_create_limit_order', 'lp_post_order',
                 'lp_prepare_protected_sell', 'submit_protected_sell'):
        monkeypatch.setattr(trading, name, forbidden_native, raising=False)
    try:
        wait_until(lambda: runtime.status()['orderbook']['shadow']['context_missing_reads'] == 1,
                   live.run_once)
        first = runtime.status()['orderbook']['shadow']
        assert first.get('context_missing_reason_counts') == {'baseline_absent': 1}
        assert missing_logs(caplog)[-1]['context_missing_reason'] == 'baseline_absent'
        wait_until(lambda: bool(live.state.snapshot().active_orders), live.run_once)
        install_baseline(live, clock, complete=False, event='incomplete')
        live.run_once()
        clock.value = 5; runtime.wake()
        wait_until(lambda: runtime.status()['orderbook']['shadow']['context_missing_reads'] == 2,
                   live.run_once)
        second = runtime.status()['orderbook']['shadow']
        assert second['context_missing_reason_counts'] == {'baseline_absent': 1, 'baseline_incomplete': 1}
        assert missing_logs(caplog)[-1]['context_missing_reason'] == 'baseline_incomplete'
        wait_until(lambda: live.state.snapshot().observation.read_ticket.trade_version_at_start
                   == live.state.snapshot().trade_version, live.run_once)
        baseline = install_baseline(live, clock, complete=True, event='complete')
        live.run_once()
        clock.value = 10; runtime.wake()
        wait_until(lambda: runtime.status()['orderbook']['shadow']['samples'] == 1, live.run_once)
        final = runtime.status()['orderbook']['shadow']
        assert final['context_missing_reason_counts'] == second['context_missing_reason_counts']
        assert final['context_missing_reads'] == 2 and final['eligible_rest_reads'] == 3
        assert final['unknown_sample_denominator'] == 3
        assert live.state.snapshot().baselines[baseline.key] is baseline
        assert trading.cancels == [] and native_calls == [] and len(missing_logs(caplog)) == 2
    finally:
        boundary.release.set(); assert live.close(4)


from contextlib import contextmanager
from dataclasses import replace
from open_trader.polymarket_lp_read_runtime import LPReadRuntime
from test_lp_async_read_runtime_contract import Boundary, demand, collect
from test_lp_async_source_contract import CID


def comparison_context(d, basis='baseline'):
    return dict(key=(d.key, basis), demand_key=d.key, order_ids=(d.order_id,),
                condition_id=d.condition_id, token_id=d.token_id, price=D('.40'),
                own_remaining=D(20), bid_level=1, baseline_levels=((D('.40'), D(80)),),
                accepted_at=None)


@contextmanager
def offline_runtime():
    clock = Clock(); boundary = Boundary(clock)
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc,
                            orderbook_url='ws://offline.invalid/ws')
    runtime.start()
    try:yield clock, boundary, runtime
    finally:
        boundary.release.set(); runtime.reconcile(()); assert runtime.close(4)


def configure(runtime, ds, *, contexts=(), hints=None, mode='shadow', tokens=None):
    runtime.configure_orderbook(mode, {'yes': CID} if tokens is None else tokens,
        eligible_keys=tuple(d.key for d in ds), comparison_contexts=contexts,
        missing_context_reasons=hints)


def actual_read(runtime, boundary, d):
    runtime.reconcile((d,))
    results = collect(runtime, boundary, lambda rows: any(r.demand.key == d.key for r in rows))
    result, = [r for r in results if r.demand.key == d.key]
    assert result.complete and result.sources['book'].complete
    assert result.sources['book'].value.get('transport') != 'ws'
    return result


def test_runtime_rejections_are_partitioned_without_changing_admission(offline_ws, caplog):
    caplog.set_level('INFO')
    for reason, level in (('demand_filter', None), ('token_filter', None),
                          ('invalid_bid_level', True), ('invalid_bid_level', 0),
                          ('invalid_bid_level', 65), ('context_capacity', None)):
        with offline_runtime() as (clock, boundary, runtime):
            d = demand(sources=('book',)); filler = demand('filler', sources=('book',))
            context = comparison_context(d)
            contexts = []
            if reason == 'demand_filter':
                context.update(demand_key=demand('ineligible').key,
                               demand_keys=(demand('ineligible').key, d.key))
            elif reason == 'token_filter':context['condition_id'] = 'wrong-condition'
            elif reason == 'invalid_bid_level':context['bid_level'] = level
            else:contexts = [comparison_context(filler, str(i)) for i in range(64)]
            contexts.append(context)
            configure(runtime, (d, filler), contexts=contexts)
            before = runtime.status()['orderbook']['shadow']
            assert before['active_contexts'] == (64 if reason == 'context_capacity' else 0)
            assert before['incomplete_observations'] == int(reason in ('invalid_bid_level', 'context_capacity'))
            assert before['context_missing_reads'] == 0
            actual_read(runtime, boundary, d)
            after = runtime.status()['orderbook']['shadow']
            assert after['context_missing_reason_counts'] == {reason: 1}
            assert after['context_missing_reads'] == 1 and after['samples'] == 0
            assert after['not_comparable'] == before['not_comparable'] + 1
            assert missing_logs(caplog)[-1]['context_missing_reason'] == reason
        with offline_runtime() as (_, boundary, runtime):
            # A valid candidate takes precedence over a competing rejected one.
            good = comparison_context(d, 'accepted')
            competing = ((good, *contexts[:63], context) if reason == 'context_capacity'
                         else (context, good))
            configure(runtime, (d, filler), contexts=competing, hints={d.key: 'remaining_unknown'})
            actual_read(runtime, boundary, d)
            after = runtime.status()['orderbook']['shadow']
            assert after['samples'] == 1 and after['context_missing_reads'] == 0
            assert after['context_missing_reason_counts'] == {}


def test_reason_hints_do_not_cross_identity_or_recount_history(offline_ws, caplog):
    caplog.set_level('INFO')
    with offline_runtime() as (clock, boundary, runtime):
        original = demand(sources=('book',))
        configure(runtime, (original,), hints={original.key: 'baseline_absent'})
        for _ in range(3):
            configure(runtime, (original,), hints={original.key: 'baseline_absent'})
            assert runtime.drain_results() == ()
            assert runtime.status()['orderbook']['shadow']['context_missing_reads'] == 0
        actual_read(runtime, boundary, original)
        expected = {'baseline_absent': 1}
        assert runtime.status()['orderbook']['shadow']['context_missing_reason_counts'] == expected
        variants = (replace(original, generation='new-cycle'), replace(original, order_id='new-order'),
                    replace(original, terms_version=2))
        for i, changed in enumerate(variants, 1):
            configure(runtime, (changed,))
            actual_read(runtime, boundary, changed)
            expected['unattributed'] = i
            assert runtime.status()['orderbook']['shadow']['context_missing_reason_counts'] == expected
        configure(runtime, ())
        runtime.reconcile(())
        wait_until(lambda: runtime.status()['owned_buy_scopes'] == 0)
        configure(runtime, (original,))
        actual_read(runtime, boundary, original)
        expected['unattributed'] += 1
        for mode in ('off', 'use', 'shadow'):
            configure(runtime, (original,), hints={original.key: 'anchor_changed'}, mode=mode)
            runtime.status(); assert runtime.drain_results() == ()
            assert runtime.status()['orderbook']['shadow']['context_missing_reason_counts'] == expected
        # Reconfigure while a physical read is waiting: use the current hint at
        # completion, rather than a reason captured when this request started.
        boundary.holds = {(original.order_id, 'book')}
        clock.value = 5; runtime.wake()
        boundary.wait(lambda: ('entered', original.order_id, 'book', 5) in boundary.events)
        configure(runtime, (original,), hints={original.key: 'remaining_unknown'})
        boundary.release.set()
        collect(runtime, boundary, bool)
        expected['remaining_unknown'] = 1
        assert runtime.status()['orderbook']['shadow']['context_missing_reason_counts'] == expected
        assert missing_logs(caplog)[-1]['context_missing_reason'] == 'remaining_unknown'
        # An old waiting scope cannot use metadata for a new generation/order/terms.
        boundary.release.clear(); clock.value = 10; runtime.wake()
        boundary.wait(lambda: ('entered', original.order_id, 'book', 10) in boundary.events)
        fresh = replace(original, generation='fresh', order_id='fresh', terms_version=3)
        configure(runtime, (fresh,), hints={fresh.key: 'anchor_changed'})
        boundary.release.set()
        collect(runtime, boundary, bool)
        # Existing eligibility guard excludes this departed identity entirely.
        assert runtime.status()['orderbook']['shadow']['context_missing_reason_counts'] == expected
        configure(runtime, (fresh,))
        actual_read(runtime, boundary, fresh)
        expected['unattributed'] += 1
        assert runtime.status()['orderbook']['shadow']['context_missing_reason_counts'] == expected
        assert missing_logs(caplog)[-1]['context_missing_reason'] == 'unattributed'
        for _ in range(5):
            runtime.status(); assert runtime.drain_results() == ()
        assert runtime.status()['orderbook']['shadow']['context_missing_reason_counts'] == expected


def test_reason_precedence_bounds_and_public_output(offline_ws, caplog):
    caplog.set_level('INFO')
    d = demand(sources=('book',)); filler = demand('filler', sources=('book',))
    candidates = (
        dict(comparison_context(d, 'demand'), demand_key=demand('wrong-primary').key, demand_keys=(d.key,)),
        dict(comparison_context(d, 'token'), token_id='wrong-token'),
        dict(comparison_context(d, 'level'), bid_level='1'),
    )
    for candidate_count, capacity in ((1, False), (2, False), (3, False), (3, True)):
        for reverse in (False, True):
            with offline_runtime() as (_, boundary, runtime):
                rejected = [*candidates, comparison_context(d, 'capacity')] if capacity else list(candidates[:candidate_count])
                if reverse:rejected.reverse()
                prefix = [comparison_context(filler, str(i)) for i in range(64)] if capacity else []
                configure(runtime, (d, filler), contexts=(*prefix, *rejected),
                          hints={d.key: 'remaining_unknown'})
                actual_read(runtime, boundary, d)
                report = runtime.status()['orderbook']['shadow']
                expected = 'context_capacity' if capacity else ('demand_filter', 'token_filter', 'invalid_bid_level')[candidate_count - 1]
                assert report['context_missing_reason_counts'] == {expected: 1}
                assert report['context_missing_reason_fractions'] == {expected: 1.0}
    with offline_runtime() as (_, boundary, runtime):
        ds = tuple(demand(str(i), sources=('book',)) for i in range(70))
        configure(runtime, ds, hints={x.key: 'baseline_absent' for x in ds})
        assert runtime.status()['orderbook']['context_missing_reason_hints_retained'] == 64
        actual_read(runtime, boundary, ds[0]); actual_read(runtime, boundary, ds[69])
        report = runtime.status()['orderbook']['shadow']
        assert report['context_missing_reason_counts'] == {'baseline_absent': 1, 'unattributed': 1}
        assert report['context_missing_reason_fractions'] == {'baseline_absent': .5, 'unattributed': .5}
        configure(runtime, ())
        assert runtime.status()['orderbook']['context_missing_reason_hints_retained'] == 0
    class Hostile:
        def __str__(self):raise AssertionError('reason must not be stringified')
        def __repr__(self):raise AssertionError('reason must not be represented')
    hostile = ('secretlike-private-rejection-495', {'secretlike-private-rejection-495': ['private']},
               Hostile(), None, 3)
    with offline_runtime() as (_, boundary, runtime):
        for i, value in enumerate(hostile):
            current = replace(d, order_id='identity-private-' + str(i))
            configure(runtime, (current,), hints={current.key: value})
            actual_read(runtime, boundary, current)
        report = runtime.status()['orderbook']['shadow']
        assert report['context_missing_reason_counts'] == {'unattributed': 5}
        assert report['context_missing_reason_fractions'] == {'unattributed': 1.0}
        assert sum(report['context_missing_reason_fractions'].values()) == pytest.approx(1)
        assert 'secretlike-private-rejection-495' not in repr(report)
        assert 'identity-private-' not in repr(report)
    # Owner causes not constructible via legal guarded events still reach the
    # actual runtime completion seam as captured, identity-bound hints.
    for cause in ('anchor_changed', 'remaining_unknown'):
        with offline_runtime() as (_, boundary, runtime):
            configure(runtime, (d,), hints={d.key: cause})
            actual_read(runtime, boundary, d)
            report = runtime.status()['orderbook']['shadow']
            assert report['context_missing_reason_counts'] == {cause: 1}
            assert missing_logs(caplog)[-1]['context_missing_reason'] == cause
    for row in missing_logs(caplog):
        assert set(row) == {'reason', 'context_missing_reason', 'token_prefix', 'rest_exchange_at', 'rest_read_at'}
        assert row['reason'] == 'missing_comparison_context'
        assert row['context_missing_reason'] in REASONS
        assert 'secretlike-private-rejection-495' not in json.dumps(row)
        assert 'identity-private-' not in json.dumps(row)
    summary = ShadowDiagnostics()
    for value in hostile:summary.eligible_read(has_context=False, missing_context_reason=value)
    assert summary.snapshot()['context_missing_reason_counts'] == {'unattributed': 5}


def qualified_summary(*, missing=2, cause=None, segment=0, unexplained=False, seconds=7200, cancels=30):
    clock = Clock(); summary = ShadowDiagnostics(clock=clock); summary.set_mode('shadow')
    for _ in range(missing):summary.eligible_read(has_context=False, missing_context_reason=cause)
    for i in range(100 - missing):
        summary.eligible_read(has_context=True)
        state = dict(state='triggered' if i < cancels else 'monitoring')
        summary.sample(state, state)
        if i < cancels:
            summary.cancel_started(observation_id=str(i)); summary.cancel_result(0)
    for stamp in range(0, seconds + 1, 10):
        clock.value = stamp
        bad = bool(segment and stamp in (100, 110) or unexplained and stamp == 100)
        summary.availability(not bad, reason=None if unexplained else 'book_unknown')
        if stamp == 110 and segment:
            clock.value = 100 + segment; summary.availability(True)
    clock.value = seconds; summary.availability(True)
    return summary


def test_missing_reason_telemetry_preserves_acceptance_and_mode_contract(offline_ws):
    added = {'context_missing_reason_counts', 'context_missing_reason_fractions'}
    for missing, seconds, cancels, segment, unexplained, expected in (
        (2, 7200, 30, 0, False, 'PASS'), (3, 7200, 30, 0, False, 'FAIL'),
        (2, 7199, 30, 0, False, 'PENDING'), (2, 7200, 29, 0, False, 'PENDING'),
        (2, 7200, 30, 15, False, 'PASS'), (2, 7200, 30, 15.001, False, 'FAIL'),
        (2, 7200, 30, 0, True, 'FAIL'),
    ):
        args = dict(missing=missing, seconds=seconds, cancels=cancels,
                    segment=segment, unexplained=unexplained)
        legacy = qualified_summary(**args).snapshot()
        report = qualified_summary(**args, cause='baseline_incomplete').snapshot()
        assert {k:v for k,v in report.items() if k not in added} == {k:v for k,v in legacy.items() if k not in added}
        assert report['assessment'] == expected
        assert report['context_missing_reason_counts'] == {'baseline_incomplete': missing}
        assert report['context_missing_reason_fractions'] == {'baseline_incomplete': 1.0}
        assert report['unknown_sample_denominator'] == 100 and report['unknown_fraction'] == missing / 100
        assert report['automatic_enable'] is False
    for mode in ('off', 'use'):
        with offline_runtime() as (_, boundary, runtime):
            d = demand(sources=('book',))
            configure(runtime, (d,), mode=mode, hints={d.key: 'remaining_unknown'})
            before = runtime.status()['orderbook']['shadow']
            actual_read(runtime, boundary, d)
            after = runtime.status()['orderbook']['shadow']
            assert after['eligible_rest_reads'] == before['eligible_rest_reads'] == 0
            assert after['samples'] == after['context_missing_reads'] == 0
            assert after['context_missing_reason_counts'] == after['context_missing_reason_fractions'] == {}
            assert runtime.status()['orderbook']['context_missing_reason_hints_retained'] == 0
