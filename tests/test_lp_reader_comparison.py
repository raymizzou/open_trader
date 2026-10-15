"""Offline same-source observation through the real public automatic round."""
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from threading import Event
import pytest

from tests.test_lp_auto_serial_fact_cycle import runtime, SerialPublic, _resting
from tests.test_lp_account_reservation_reconciliation import runtime as account_runtime
from tests.test_lp_auto_refill_contract import prepare, RefillPublic


def _check_resting(runtime):
    from open_trader.polymarket_lp_reader_comparison import ReaderComparisonObserver
    public = SerialPublic(runtime.clock)
    store, adapter, account, lp, engine = runtime(public_client=public, orders=(_resting(1), _resting(2)))
    engine.lp_auto_configure({'budget_usd':'16', 'target_buy_count':2})
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once(round_id='warmup')
    calls = Counter()
    for obj, names in [(account, ('get_balance_allowance','list_open_orders','list_positions','list_account_trades')),
                       (public, ('get_market','list_market_rewards','get_order_book'))]:
        for name in names:
            external = getattr(obj, name)
            def counted(*a, _name=name, _call=external, **kw):
                calls[_name] += 1
                return _call(*a, **kw)
            setattr(obj, name, counted)
    baseline = engine.lp_auto_run_once(round_id='without-comparison')
    before = calls.copy()
    records = []
    observer = ReaderComparisonObserver(emit=records.append)
    lp.reader_comparison_observer = observer
    try:
        primary = engine.lp_auto_run_once(round_id='with-comparison')
        assert observer.wait_idle(timeout=3)
        result = observer.snapshot()
        assert result['coverage'].get('yield') == 2 and result['coverage'].get('quantity') == 2
        assert result['match'] >= 6 and result['mismatch'] == 0 and result['error'] == 0, records
        assert any(r['round_id'] == 'with-comparison' and r['compared'] >= 6 for r in records)
        assert calls - before == before
        assert primary['last_round']['actions'] == baseline['last_round']['actions'] == []
        assert primary['slots'] == baseline['slots']
        assert account.posts == account.cancels == account.market_orders == []
        assert Decimal(primary['funds']['buy_reserved_usd']) == 16
    finally:
        observer.close()


def _check_candidates(runtime):
    from open_trader.polymarket_lp_reader_comparison import ReaderComparisonObserver
    # A separate real empty-account round exercises comparable candidate targets.
    store, adapter, account, lp, engine, _ = prepare(runtime, count=4, target=3,
        budget='24', public=RefillPublic(runtime.clock))
    records = []
    observer = ReaderComparisonObserver(emit=records.append)
    lp.reader_comparison_observer = observer
    try:
        primary = engine.lp_auto_run_once(round_id='candidate-targets')
        lp.drain_submissions()
        assert observer.wait_idle(timeout=3)
        assert any(f['field'] == 'targets' and f['status'] == 'match'
            for r in records for f in r['fields']), records
        assert observer.snapshot()['mismatch'] == 0
        assert len(account.posts) == 3 and account.cancels == account.market_orders == []
        assert primary['last_round']['round_id'] == 'candidate-targets'
    finally:
        observer.close()



@pytest.mark.parametrize('scenario', ['resting', 'candidates'])
def test_same_source_runtime_capture_matches_without_extra_reads(runtime, scenario):
    (_check_resting if scenario == 'resting' else _check_candidates)(runtime)


def packet(*, old='8', new='7', **changes):
    from datetime import UTC, datetime
    at = datetime(2026,10,15,tzinfo=UTC)
    entry = dict(kind='field', field='buy_reserved', old=Decimal(old) if old is not None else None,
        new=Decimal(new) if new is not None else None, source_id='source-1', old_source_id='source-1',
        as_of=at, old_as_of=at, source_times={'checked_at':at}, account_id='wallet-secret', order_id='order-secret')
    entry.update(changes)
    return dict(round_id='explicit-round', entries=[entry])


def test_difference_reports_both_values_without_changing_primary_result(runtime):
    from open_trader.polymarket_lp_reader_comparison import ReaderComparisonObserver
    store, adapter, account, lp, engine = runtime(public_client=SerialPublic(runtime.clock), orders=(_resting(1),))
    engine.lp_auto_configure({'budget_usd':'8', 'target_buy_count':1})
    engine.lp_auto_set_desired_running(True)
    records=[]
    observer=ReaderComparisonObserver(emit=records.append)
    lp.reader_comparison_observer=observer
    try:
        primary=engine.lp_auto_run_once(round_id='primary-unchanged')
        assert observer.wait_idle(timeout=3)
        before=engine.lp_auto_state()
        late=packet()
        healthy=packet(old='2',new='2')['entries'][0]
        healthy['field']='inventory_cost'
        late['entries']=[dict(healthy) for _ in range(20)]+late['entries']
        for _ in range(2):
            assert observer.offer(late)
            assert observer.wait_idle(timeout=3)
        mismatch=[f for r in records for f in r['fields'] if f['status']=='mismatch']
        assert len(mismatch)==1
        field=mismatch[0]
        assert field['field']=='buy_reserved' and field['old']==Decimal('8') and field['new']==Decimal('7')
        assert field['delta']==Decimal('-1') and field['source_id']=='source-1'
        assert field['as_of']=='2026-10-15T00:00:00+00:00'
        assert field['source_times']['checked_at']=='2026-10-15T00:00:00+00:00'
        assert len(field['order_hash'])==64 and 'order-secret' not in str(records)
        assert observer.snapshot()['mismatch']==2
        assert engine.lp_auto_state()==before
        assert primary['last_round']['actions']==[]
        assert account.posts==account.cancels==account.market_orders==[]
        assert before['desired_running'] and before['plan_wait'] is None
    finally:
        observer.close()


@pytest.mark.parametrize('missing', ['round', 'order', 'account', 'source', 'different_source', 'time', 'different_time', 'unknown_zero'])
def test_missing_or_different_source_is_not_reported_as_match(missing):
    from open_trader.polymarket_lp_reader_comparison import ReaderComparisonObserver
    from datetime import UTC, datetime
    changes = {'order': {'order_id': None}, 'account': {'account_id':None}, 'source': {'source_id':None},
        'different_source': {'old_source_id':'other'}, 'time': {'as_of':None},
        'different_time': {'old_as_of':datetime(2026,10,14,tzinfo=UTC)},
        'unknown_zero': {'old':None, 'new':Decimal(0)}}.get(missing,{})
    value=packet(**{'old':'8', 'new':'8', **changes})
    if missing=='round': value['round_id']=None
    # Independent complete field can still be compared, except when the whole round is absent.
    healthy=packet(old='2',new='2')['entries'][0]
    healthy['field']='inventory_cost'
    value['entries'].append(healthy)
    records=[]
    observer=ReaderComparisonObserver(emit=records.append)
    try:
        observer.offer(value)
        assert observer.wait_idle(timeout=3)
        fields=records[0]['fields']
        affected=next(f for f in fields if f['field']=='buy_reserved')
        assert affected['status']=='not_comparable' and affected['reason']
        assert observer.snapshot()['mismatch']==0
        assert observer.snapshot()['match']==(0 if missing=='round' else 1)
        assert records[0]['compared']==(0 if missing=='round' else 1)
    finally:
        observer.close()


@pytest.mark.parametrize('failure', ['blocked_compare', 'blocked_sink', 'comparison_error'])
def test_blocked_comparison_or_log_sink_never_waits_for_main_flow(runtime, failure):
    from open_trader.polymarket_lp_reader_comparison import ReaderComparisonObserver, compare_snapshot, logger
    from tests.test_lp_account_reservation_reconciliation import _refill_identity
    from threading import Thread
    import logging
    entered, release, done = Event(), Event(), Event()
    primary, errors = [], []
    store, adapter, account, lp, engine, _ = prepare(runtime, count=3, target=3, budget='24', public=RefillPublic(runtime.clock))
    class Sink(logging.Handler):
        def emit(self, record):
            entered.set()
            assert release.wait(4)
    sink=Sink()
    def compare(value):
        entered.set()
        if failure=='comparison_error': raise RuntimeError('secret original error')
        assert release.wait(4)
        return compare_snapshot(value)
    if failure=='blocked_sink': logger.addHandler(sink); logger.setLevel(logging.INFO)
    observer=ReaderComparisonObserver(**({'compare':compare} if failure!='blocked_sink' else {}))
    lp.reader_comparison_observer=observer
    def run():
        try: primary.append(engine.lp_auto_run_once(round_id='nonblocking'))
        except BaseException as exc: errors.append(exc)
        finally: done.set()
    thread=Thread(target=run)
    try:
        thread.start()
        assert entered.wait(3)
        assert done.wait(3), 'public round waited for observation work or log I/O'
        assert not errors
        lp.drain_submissions()
        assert Counter(p.token_id for p in account.posts)==Counter(_refill_identity(i)[2] for i in (1,2,3))
        assert all(p.maker_amount==8000000 and p.taker_amount==20000000 and p.post_only for p in account.posts)
        assert account.cancels==account.market_orders==[]
        assert primary[0]['last_round']['completed_at'] and primary[0]['desired_running']
        release.set()
        assert observer.wait_idle(timeout=3)
        if failure=='comparison_error': assert observer.snapshot()['error']==1
    finally:
        release.set(); thread.join(timeout=4); observer.close(); logger.removeHandler(sink)
        assert not thread.is_alive()


def test_overload_keeps_one_running_and_latest_pending_frame():
    from open_trader.polymarket_lp_reader_comparison import ReaderComparisonObserver, compare_snapshot
    entered, release = Event(), Event()
    executed=[]
    def compare(value):
        executed.append(value['round_id'])
        if value['round_id']=='A':
            entered.set()
            assert release.wait(4)
        return compare_snapshot(value)
    observer=ReaderComparisonObserver(compare=compare, emit=lambda r: None)
    def sample(identity):
        value=packet(old='8',new='8'); value['round_id']=identity; return value
    try:
        assert observer.offer(sample('A')) and entered.wait(2)
        assert observer.offer(sample('B')) and observer.offer(sample('C'))
        state=observer.snapshot()
        assert state['running']==1 and state['pending']==1 and state['skipped']==1
        oversized=sample('huge'); oversized['content']='x'*(4*1024*1024+1)
        assert observer.offer(oversized) is False
        too_many=sample('nodes'); too_many['content']=[None]*50001
        assert observer.offer(too_many) is False
        assert observer.snapshot()['skipped']==3
        release.set()
        assert observer.wait_idle(timeout=3)
        assert executed==['A','C'] and observer.snapshot()['match']==2
    finally:
        release.set(); observer.close(); assert observer.wait_idle(timeout=3)


def test_capture_is_immutable_and_observer_close_releases_references():
    from open_trader.polymarket_lp_reader_comparison import ReaderComparisonObserver, compare_snapshot
    import gc, weakref
    entered, release = Event(), Event()
    results=[]
    def compare(value):
        entered.set()
        assert release.wait(4)
        results.append(compare_snapshot(value))
        return results[-1]
    observer=ReaderComparisonObserver(compare=compare,emit=lambda r:None)
    original=packet(old='8',new='8')
    class Resource: pass
    resource=Resource(); reference=weakref.ref(resource)
    invalid=packet(); invalid['client']=resource
    try:
        assert observer.offer(invalid) is False
        del invalid, resource
        gc.collect()
        assert reference() is None
        assert observer.offer(original) and entered.wait(2)
        original['entries'][0]['new']=Decimal('99')
        assert observer.offer(packet())
        observer.close()  # must return while the pure comparator is blocked
        assert observer.snapshot()['pending']==0 and observer.snapshot()['skipped']==1
        assert observer.offer(packet()) is False
        assert observer.snapshot()['skipped']==2
        release.set()
        assert observer.wait_idle(timeout=3)
        assert results[0][0]['old']==results[0][0]['new']==Decimal('8')
        assert observer.snapshot()['running']==0
    finally:
        release.set(); observer.close(); assert observer.wait_idle(timeout=3)


def test_periodic_summary_and_mismatch_logs_are_bounded_and_redacted():
    from open_trader.polymarket_lp_reader_comparison import ReaderComparisonObserver, compare_snapshot
    clock=[0.0]; records=[]
    wallet='0x'+'a'*40
    secret='credential-and-signature-secret'
    def compare(value):
        if value.get('fail'): raise RuntimeError(secret)
        return compare_snapshot(value)
    observer=ReaderComparisonObserver(emit=records.append,compare=compare,clock=lambda:clock[0])
    def offer(value):
        observer.offer(value); assert observer.wait_idle(timeout=3)
    try:
        for t in range(60):
            clock[0]=float(t); offer(packet(old='8',new='8'))
        assert len(records)<=1
        clock[0]=60; offer(packet(old='8',new='8'))
        summary=[r for r in records if r['event']=='summary'][-1]
        assert summary['counts']['match']==61 and summary['counts']['mismatch']==0
        many=packet(); many['entries']*=30
        many['entries'][0]=dict(many['entries'][0],old=wallet,new=secret)
        offer(many)
        before=len(records)
        offer(many)
        assert len(records)==before
        clock[0]=120; offer(packet(old=None,new='0'))
        failed=packet(); failed['fail']=True; offer(failed)
        oversized=packet(); oversized['value']='x'*(4*1024*1024+1)
        assert observer.offer(oversized) is False
        clock[0]=180; offer(packet(old='8',new='8'))
        summary=[r for r in records if r['event']=='summary'][-1]
        assert summary['counts']==dict(match=62,mismatch=60,not_comparable=1,error=1,skipped=1)
        assert all(len(r.get('fields',()))<=12 for r in records)
        text=str(records)
        assert wallet not in text and secret not in text and 'wallet-secret' not in text
        differences=[r for r in records if r['event']=='difference']
        assert differences and differences[0]['omitted_differences']==18
        assert all(any(f['status']=='mismatch' for f in r['fields']) for r in differences)
        assert all(f['as_of'] for r in differences for f in r['fields'])
    finally:
        observer.close()
