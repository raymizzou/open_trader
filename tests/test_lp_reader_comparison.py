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



def _check_balance_failure(runtime):
    from open_trader.polymarket_lp_reader_comparison import ReaderComparisonObserver, compare_snapshot
    from tests.test_lp_auto_serial_fact_cycle import _yield
    public = SerialPublic(runtime.clock)
    store, adapter, account, lp, engine = runtime(public_client=public, orders=(_resting(1),))
    engine.lp_auto_configure({'budget_usd': '8', 'target_buy_count': 1})
    engine.lp_auto_set_desired_running(True)
    engine.lp_auto_run_once(round_id='healthy-before-partial')
    def unavailable(**kwargs):
        raise TimeoutError('offline balance unavailable')
    account.get_balance_allowance = unavailable
    runtime.clock[0] += timedelta(seconds=61)
    public.rates = {1: '48'}
    calls = Counter()
    for obj, names in [(account, ('get_balance_allowance', 'list_open_orders', 'list_positions', 'list_account_trades')),
                       (public, ('get_market', 'list_market_rewards', 'get_order_book'))]:
        for name in names:
            external = getattr(obj, name)
            def counted(*args, _name=name, _call=external, **kwargs):
                calls[_name] += 1
                return _call(*args, **kwargs)
            setattr(obj, name, counted)
    entered, release = Event(), Event()
    def compare(value):
        entered.set()
        assert release.wait(4)
        return compare_snapshot(value)
    records = []
    observer = ReaderComparisonObserver(compare=compare, emit=records.append)
    lp.reader_comparison_observer = observer
    try:
        primary = engine.lp_auto_run_once(round_id='partial-failure')
        assert entered.wait(2)
        primary_calls = calls.copy()
        assert primary_calls['get_balance_allowance'] == primary_calls['list_open_orders'] == primary_calls['list_positions'] == 1
        assert primary_calls['list_account_trades'] == 0
        release.set()
        assert observer.wait_idle(timeout=3)
        assert calls == primary_calls
        assert _yield(store, 'resting-1')['state'] == 'known'
        assert observer.snapshot()['coverage'].get('yield') == 1, records
        assert observer.snapshot()['not_comparable'] >= 1
        assert observer.snapshot()['mismatch'] == observer.snapshot()['error'] == 0
        assert any(r['round_id'] == 'partial-failure' and r['compared'] >= 1 for r in records)
        assert primary['last_round']['round_id'] == 'partial-failure'
        assert primary['funds']['status'] == 'unknown'
        assert primary['funds']['spendable_usd'] is None and primary['last_round']['actions'] == []
        assert account.posts == account.cancels == account.market_orders == []
    finally:
        release.set(); observer.close(); assert observer.wait_idle(timeout=3)


@pytest.mark.parametrize('scenario', ['resting', 'candidates', 'balance_failure'])
def test_same_source_runtime_capture_matches_without_extra_reads(runtime, scenario):
    {'resting': _check_resting, 'candidates': _check_candidates, 'balance_failure': _check_balance_failure}[scenario](runtime)


def packet(*, old='8', new='7', **changes):
    from datetime import UTC, datetime
    at = datetime(2026,10,15,tzinfo=UTC)
    entry = dict(kind='field', field='buy_reserved', old=Decimal(old) if old is not None else None,
        new=Decimal(new) if new is not None else None, source_id='source-1', old_source_id='source-1',
        as_of=at, old_as_of=at, source_times={'checked_at':at}, account_id='wallet-secret', order_id='order-secret')
    entry.update(changes)
    return dict(round_id='explicit-round', entries=[entry])


@pytest.mark.parametrize('holding_difference', ['quantity', 'identity', 'cost_attribution', 'unknown_cost'])
def test_difference_reports_both_values_without_changing_primary_result(runtime, holding_difference):
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
        from datetime import UTC, datetime
        from hashlib import sha256
        from open_trader.polymarket_lp_reader_comparison import compare_snapshot
        at = datetime(2026, 10, 15, tzinfo=UTC)
        token = 'holding-token-secret-T'
        healthy_token = 'healthy-token-secret-H'
        source_positions = [dict(condition_id='C', token_id=token, quantity='40', cost_usd='16'),
            dict(condition_id='H', token_id=healthy_token, quantity='5', cost_usd='0')]
        old_positions = [dict(token_id=healthy_token, quantity='5', inventory_cost_usd='0'),
            dict(token_id=token, quantity='20', inventory_cost_usd='16')]
        if holding_difference == 'identity':
            old_positions[1]['token_id'] = 'different-holding-token-secret'
        elif holding_difference == 'cost_attribution':
            source_positions = [dict(condition_id='U', token_id='holding-token-secret-U', quantity='20', cost_usd='12'),
                dict(condition_id='C', token_id=token, quantity='20', cost_usd='4'), source_positions[1]]
            old_positions = [old_positions[0], dict(token_id=token, quantity='20', inventory_cost_usd='8'),
                dict(token_id='holding-token-secret-U', quantity='20', inventory_cost_usd='8')]
        elif holding_difference == 'unknown_cost':
            source_positions[0].update(quantity='20', cost_usd=None)
        source = dict(account_id='wallet-secret', balance='100', allowance='100', balance_complete=True,
            open_orders=(), open_orders_complete=True, positions_complete=True, checked_at=at, positions=source_positions)
        old_account = dict(buys=(), inventory_cost_usd='16', balance_usd='100', allowance_usd='100', positions=old_positions)
        holdings = dict(round_id='holdings', entries=[dict(kind='account', source_id='positions-source',
            account=source, old=old_account, as_of=at)])
        fields = compare_snapshot(holdings)
        expected_field = {'quantity': 'position_quantity', 'identity': 'position_present',
            'cost_attribution': 'position_cost', 'unknown_cost': 'position_cost'}[holding_difference]
        affected = [f for f in fields if f['field'] == expected_field and f.get('token_hash') == sha256(token.encode()).hexdigest()]
        assert affected, fields
        assert affected[0]['status'] == ('not_comparable' if holding_difference == 'unknown_cost' else 'mismatch')
        if holding_difference == 'quantity':
            assert affected[0]['old'] == Decimal('20') and affected[0]['new'] == Decimal('40') and affected[0]['delta'] == Decimal('20')
        elif holding_difference == 'cost_attribution':
            assert affected[0]['old'] == Decimal('8') and affected[0]['new'] == Decimal('4')
            assert {f['field'] for f in fields if f['status'] == 'mismatch'} == {'position_cost'}
        elif holding_difference == 'unknown_cost':
            assert affected[0]['new'] is None and not any(f['status'] == 'mismatch' for f in fields)
        total = next(f for f in fields if f['field'] == 'inventory_cost')
        if holding_difference != 'unknown_cost':
            assert total['old'] == total['new'] == Decimal('16') and total['status'] == 'match'
        healthy_fields = [f for f in fields if f.get('token_hash') == sha256(healthy_token.encode()).hexdigest()]
        assert len(healthy_fields) == 3 and all(f['status'] == 'match' for f in healthy_fields)
        assert any(f['field'] == 'balance' and f['status'] == 'match' for f in fields)
        assert observer.offer(holdings) and observer.wait_idle(timeout=3)
        if holding_difference != 'unknown_cost':
            details = records[-1]['fields']
            assert any(f.get('token_hash') == sha256(token.encode()).hexdigest() and f['status'] == 'mismatch' for f in details)
            assert all(len(r.get('fields', ())) <= 12 for r in records)
        assert token not in str(records) and healthy_token not in str(records) and 'wallet-secret' not in str(records)
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
def test_blocked_comparison_or_log_sink_never_waits_for_main_flow(runtime, failure, monkeypatch):
    from open_trader.polymarket_lp_reader_comparison import ReaderComparisonObserver, compare_snapshot
    from tests.test_lp_account_reservation_reconciliation import _refill_identity
    from threading import Thread
    import io, sys
    entered, release, done = Event(), Event(), Event()
    sink_done = Event()
    primary, errors = [], []
    store, adapter, account, lp, engine, _ = prepare(runtime, count=3, target=3, budget='24', public=RefillPublic(runtime.clock))
    class Sink(io.StringIO):
        def write(self, text):
            result = super().write(text)
            if 'lp_reader_comparison facts=' in text:
                entered.set()
                assert release.wait(4)
            return result
        def flush(self):
            super().flush()
            if 'lp_reader_comparison facts=' in self.getvalue():
                sink_done.set()
    sink=Sink()
    def compare(value):
        entered.set()
        if failure=='comparison_error': raise RuntimeError('secret original error')
        assert release.wait(4)
        return compare_snapshot(value)
    if failure=='blocked_sink': monkeypatch.setattr(sys, 'stdout', sink)
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
        release.set(); thread.join(timeout=4); observer.close()
        if failure == 'blocked_sink' and entered.is_set():
            assert sink_done.wait(3), 'asynchronous stdout sink did not finish after release'
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


def _assert_default_observation_streams(tmp_path, monkeypatch):
    import io
    import logging
    from pathlib import Path
    import re
    import subprocess
    import sys
    from open_trader.polymarket_lp_reader_comparison import ReaderComparisonObserver, logger

    written = Event()
    class Output(io.StringIO):
        def flush(self):
            super().flush()
            if 'lp_reader_comparison facts=' in self.getvalue():
                written.set()
    stdout, stderr = Output(), Output()
    root = logging.getLogger()
    handlers, root_level, module_level = list(root.handlers), root.level, logger.level
    observer = ReaderComparisonObserver()
    business_handler = None
    try:
        with monkeypatch.context() as streams:
            streams.setattr(sys, 'stdout', stdout)
            streams.setattr(sys, 'stderr', stderr)
            for handler in handlers:
                root.removeHandler(handler)
            business_handler = logging.StreamHandler()
            business_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s',
                datefmt='%Y-%m-%dT%H:%M:%S%z'))
            root.addHandler(business_handler)
            root.setLevel(logging.INFO)
            logger.setLevel(logging.INFO)
            logging.getLogger('test_business_startup').info('business startup ready')
            assert observer.offer(packet(old='8', new='8'))
            assert observer.wait_idle(timeout=3)
            assert written.wait(3), 'default observation output did not finish within the real watchdog'
            err_path = tmp_path / 'launchd.err.log'
            err_path.write_text(stderr.getvalue())
            checker = Path(__file__).resolve().parents[1] / 'scripts' / 'check_production_log.py'
            healthy = subprocess.run([sys.executable, str(checker), str(err_path)], capture_output=True, text=True, timeout=3)
            assert healthy.returncode == 0, (healthy.stderr, stderr.getvalue())
            assert 'lp_reader_comparison' not in stderr.getvalue()
            text = stdout.getvalue()
            assert re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:?\d{2}|Z) INFO open_trader\.polymarket_lp_reader_comparison lp_reader_comparison facts=', text)
            assert "'event': 'summary'" in text and "'error': 0" in text and "Decimal('8')" in text
            assert "'as_of': '2026-10-15T00:00:00+00:00'" in text and "'checked_at': '2026-10-15T00:00:00+00:00'" in text
            assert 'wallet-secret' not in text and 'order-secret' not in text
            assert observer.snapshot()['error'] == 0
            logging.getLogger('test_business_error').error('independent business ERROR sentinel')
            err_path.write_text(stderr.getvalue())
            business = subprocess.run([sys.executable, str(checker), str(err_path)], capture_output=True, text=True, timeout=3)
            assert business.returncode == 1 and 'reason=log_error_signal' in business.stderr
            assert 'independent business ERROR sentinel' in stderr.getvalue()
    finally:
        observer.close()
        try:
            assert observer.wait_idle(timeout=3)
        finally:
            for handler in list(root.handlers):
                root.removeHandler(handler)
            if business_handler is not None:
                business_handler.close()
            for handler in handlers:
                root.addHandler(handler)
            root.setLevel(root_level)
            logger.setLevel(module_level)


def test_periodic_summary_and_mismatch_logs_are_bounded_and_redacted(tmp_path, monkeypatch):
    _assert_default_observation_streams(tmp_path, monkeypatch)
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
        # A new mismatching token is changed evidence even with identical values.
        from datetime import UTC, datetime
        from hashlib import sha256
        at = datetime(2026, 10, 15, tzinfo=UTC)
        before_changes = len(differences)
        for token in ('changed-token-secret-T', 'changed-token-secret-U'):
            source = dict(account_id='wallet-secret', balance='100', allowance='100', balance_complete=True,
                open_orders=(), open_orders_complete=True, positions_complete=True, checked_at=at,
                positions=[dict(condition_id='C', token_id=token, quantity='40', cost_usd='16')])
            old_account = dict(buys=(), inventory_cost_usd='16', balance_usd='100', allowance_usd='100',
                positions=[dict(token_id=token, quantity='20', inventory_cost_usd='16')])
            offer(dict(round_id='changed-position', entries=[dict(kind='account', source_id='changed-source',
                account=source, old=old_account, as_of=at)]))
        changes = [r for r in records if r['event'] == 'difference'][before_changes:]
        assert len(changes) == 2
        assert [r['fields'][0]['token_hash'] for r in changes] == [
            sha256(t.encode()).hexdigest() for t in ('changed-token-secret-T', 'changed-token-secret-U')]
        assert all(r['fields'][0]['old'] == Decimal('20') and r['fields'][0]['new'] == Decimal('40') for r in changes)
        assert 'changed-token-secret' not in str(records)
    finally:
        observer.close()
