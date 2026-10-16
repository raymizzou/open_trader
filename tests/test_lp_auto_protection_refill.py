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
    import json,sqlite3,threading
    from pathlib import Path
    from datetime import timedelta
    from copy import deepcopy
    from collections import Counter
    from dataclasses import asdict,is_dataclass
    evidence=dict(cycles=[],native_invalidations=[])
    venue_lock=threading.Lock()
    actual_receipts=[]
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
    planning_reader = adapter._lp_planning_account_snapshot

    def request_record(args, kwargs):
        return dict(max_age=args[0] if args else kwargs.get('max_age_seconds'),
                    generation_provider_matches=kwargs.get('trade_generation_provider') == store.lp_trade_generation,
                    posts_before=len(account.posts), sdk_before=account.position_reads)

    def observed_shared_reader(*args, **kwargs):
        record = request_record(args, kwargs)
        if active_trace is not None:
            if execution.lp_auto_state()['active_plan'] and not getattr(read_role, 'publisher', False):
                active_trace['active_finance'].append(record)
            destination = 'publishers' if getattr(read_role, 'publisher', False) else 'shared'
            active_trace[destination].append(record)
        try:
            result = shared_reader(*args, **kwargs)
            record.update(successful=True,packet=deepcopy(result),read_started_at=result['read_started_at'],
                read_ended_at=result['read_ended_at'],checked_at=result['checked_at'])
            return result
        finally:
            record['sdk_after'] = account.position_reads

    adapter.lp_account_snapshot_shared = observed_shared_reader

    def observed_planning_reader(*args, **kwargs):
        record = request_record(args, kwargs)
        if active_trace is not None:
            active_trace['requests'].append(record)
            if execution.lp_auto_state()['active_plan']:
                active_trace['active_finance'].append(record)
        try:
            result = planning_reader(*args, **kwargs)
            record.update(successful=True, packet=deepcopy(result), read_started_at=result['read_started_at'],
                          checked_at=result['checked_at'], read_ended_at=result['read_ended_at'])
            return result
        finally:
            record['sdk_after'] = account.position_reads

    adapter._lp_planning_account_snapshot = observed_planning_reader

    def start_trace():
        return dict(requests=[], publishers=[], shared=[], posts=[], active_finance=[], active_balance=[], posts_before=len(account.posts),
                    sdk_before=account.position_reads)

    def advance_to_due():
        waiting = execution.lp_auto_state().get('plan_wait')
        if waiting:
            runtime.clock[0] = max(runtime.clock[0], datetime.fromisoformat(waiting['deadline']))
        _advance(runtime)

    def assert_fence_accounting(trace):
        requests = trace['requests']
        posts_before = trace['posts_before']
        assert len(requests) == 1, 'One required complete financial publication approves the new plan'
        assert requests[0]['generation_provider_matches']
        assert requests[0]['read_started_at'] <= requests[0]['checked_at'] <= requests[0]['read_ended_at']
        assert requests[0]['read_ended_at'] <= runtime.clock[0]
        assert requests[0]['successful'] and requests[0]['sdk_after'] - requests[0]['sdk_before'] == 1
        assert requests[0]['posts_before'] == posts_before
        assert [p['request_count'] for p in trace['posts']] == [1, 1]
        assert len(trace['posts'])==2 and len({p['token_id'] for p in trace['posts']})==2
        assert len({p['order_id'] for p in trace['posts']})==2
        state=execution.lp_auto_state()
        buys=[a for a in state['last_round']['actions'] if a['kind']=='buy']
        assert Counter(p['token_id'] for p in trace['posts'])==Counter(a['token_id'] for a in buys)
        assert all(a['state']=='success' and any(p['order_id']==a['order_id'] and p['token_id']==a['token_id'] for p in trace['posts']) for a in buys)
        terms=[{k:t[k] for k in ('token_id','price','quantity')} for t in state['last_round']['targets']]
        assert all(p['targets']==terms for p in trace['posts'])
        assert {t['token_id'] for t in terms}=={_refill_identity(i)[2] for i in range(1,6)}
        assert trace['posts'][0]['sdk_reads'] == trace['posts'][1]['sdk_reads']
        extra = trace['posts'][0]['sdk_reads'] - requests[0]['sdk_after']
        assert 0 <= extra <= 3, 'Existing order observations before plan approval remain permitted'
        assert account.position_reads - trace['sdk_before'] == 1 + extra
        assert trace['active_finance'] == trace['active_balance'] == []
        assert not trace['publishers']
        as_of = trace['posts'][0]['as_of']
        assert trace['posts'][1]['as_of']==as_of
        assert as_of and datetime.fromisoformat(as_of) <= runtime.clock[0]
        state = execution.lp_auto_state()
        assert state['funds']['status'] == 'unknown' and state['funds']['as_of'] == as_of
        assert 'account_financial_facts_changed' in state['admission_block_reasons']

    balance = account.get_balance_allowance
    def observed_balance(**kwargs):
        if active_trace is not None and execution.lp_auto_state()['active_plan']:
            active_trace['active_balance'].append(runtime.clock[0])
        return balance(**kwargs)
    account.get_balance_allowance = observed_balance

    def venue_post(signed):
        observed=None
        if active_trace is not None:
            state=execution.lp_auto_state()
            observed=dict(token_id=signed.token_id,posts_before=len(account.posts),sdk_reads=account.position_reads,
                request_count=len(active_trace['requests']),as_of=state['funds']['as_of'],
                targets=[{k:t[k] for k in ('token_id','price','quantity')} for t in state['active_plan']['targets']])
            active_trace['posts'].append(observed)
        with venue_lock:
            receipt=post(signed)
            original,=[o for o in account.orders if o.id==receipt['order_id'] and o.token_id==signed.token_id]
            order=original.model_copy(update={'id':f'cycle-{len(account.posts)}'})
            account.orders=tuple(order if o.id==original.id and o.token_id==signed.token_id else o for o in account.orders)
            orders_by_id[order.id]=order
            actual_receipts.append(dict(token_id=signed.token_id,order_id=order.id,receipt=deepcopy(receipt)))
        if observed is not None:observed['order_id']=order.id
        return {**receipt,'order_id':order.id}

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

    def native_barrier(request_records):
        native_connect=sqlite3.connect
        reached,release=Event(),Event()
        original_thread,records,commits,errors=[],[],[],[]
        armed=[True]
        revision_sql=("SELECT session_id,json_extract(payload,'$._lp_trade_revision') AS revision "
            "FROM lp_sessions WHERE lower(trim(coalesce(json_extract(payload,'$.account_id'),''))) IN ('',?) "
            "AND coalesce(json_type(payload,'$.d2_audit_only'),'null') != 'true' AND coalesce(json_type(payload,'$.d2_manual_identity_only'),'null') != 'true'")
        class NativeCursor(sqlite3.Cursor):
            def execute(self,sql,parameters=()):
                self.sql,self.parameters=sql,parameters
                return super().execute(sql,parameters)
            def fetchall(self):
                rows=super().fetchall()
                if (self.sql==revision_sql and self.parameters==(adapter.config.wallet_address.casefold(),)
                        and original_thread==[threading.get_ident()] and armed[0]):
                    armed[0]=False
                    apply_free=lp._first_seen_apply_lock.acquire(blocking=False)
                    if apply_free:lp._first_seen_apply_lock.release()
                    record=dict(thread=threading.get_ident(),sql=self.sql,account=self.parameters[0],
                        rows=[dict(row) for row in rows],rows_identity=id(rows),
                        in_transaction=self.connection.in_transaction,apply_free=apply_free,
                        generation=store.lp_trade_generation(),sdk_reads=account.position_reads,
                        original_packet=deepcopy(request_records[0]['packet']) if request_records else None)
                    records.append(record);reached.set()
                    try:
                        assert not record['in_transaction'] and record['apply_free']
                        assert len(request_records)==1 and request_records[0]['sdk_after']-request_records[0]['sdk_before']==1
                        assert record['original_packet'] is not None
                        assert release.wait(5),'Native unchanged row delivery independent watchdog'
                    except BaseException as exc:errors.append(repr(exc));raise
                    finally:record['returned_rows_identity']=id(rows)
                return rows
        class NativeConnection(sqlite3.Connection):
            def execute(self,sql,parameters=()):
                result=self.cursor(factory=NativeCursor).execute(sql,parameters)
                if sql.startswith('UPDATE lp_trade_generation SET generation=generation+1') and result.rowcount==1:
                    self.generation_advance=dict(sql=sql,parameters=list(parameters),thread=threading.get_ident())
                if sql=='COMMIT' and getattr(self,'generation_advance',None):
                    commits.append({**self.generation_advance,'successful_commit':True,'in_transaction_after':self.in_transaction})
                    self.generation_advance=None
                return result
        def observed_connect(database,*args,**kwargs):
            if Path(database).resolve()==store.path.resolve():kwargs['factory']=NativeConnection
            return native_connect(database,*args,**kwargs)
        def run_original(round_id):
            original_thread.append(threading.get_ident())
            return execution.lp_auto_run_once(round_id=round_id)
        return dict(connect=observed_connect,reached=reached,release=release,run=run_original,
            records=records,commits=commits,errors=errors)

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
                _advance(runtime,61)
                lp.refresh_candidate_recommendations()
                advance_to_due()
                before_publication=account.position_reads
                trace=start_trace();active_trace=trace
                gate=native_barrier(trace['requests'])
                native=dict(cycle=cycle,rows=gate['records'],commits=gate['commits'],errors=gate['errors'])
                evidence['native_invalidations'].append(native)
                with monkeypatch.context() as race:
                    race.setattr(sqlite3,'connect',gate['connect'])
                    with ThreadPoolExecutor(1) as workers:
                        pending=workers.submit(gate['run'],f'invalidated-refill-{cycle}')
                        try:
                            assert gate['reached'].wait(5),'Native original expected-revision delivery reached'
                            assert not gate['errors'] and len(gate['records'])==1
                            held=execution.lp_auto_state()
                            assert held['active_plan'] is None and len(account.orders)==3 and len(account.posts)==posts_before
                            assert account.position_reads==before_publication+1,'Original acquired packet did real SDK I/O'
                            original_packet=trace['requests'][0]['packet']
                            original_ids={o.id for o in account.orders}
                            assert {o['order_id'] for o in original_packet['open_orders']}==original_ids
                            generation=gate['records'][0]['generation']
                            assert original_packet['trade_generation']==generation
                            assert store.lp_advance_trade_generation(generation) is True
                            assert store.lp_trade_generation()==generation+1
                            assert len(gate['commits'])==1 and gate['commits'][0]['successful_commit'] and not gate['commits'][0]['in_transaction_after']
                            _advance(runtime)
                            read_role.publisher=True
                            try:fresh=adapter.lp_account_snapshot_shared(max_age_seconds=-1,trade_generation_provider=store.lp_trade_generation)
                            finally:read_role.publisher=False
                            assert fresh['read_started_at']>original_packet['read_ended_at'] and fresh['trade_generation']==generation+1
                            assert {o['order_id'] for o in fresh['open_orders']}==original_ids
                            assert account.position_reads==before_publication+2,'Independent public publisher SDK read once'
                            publication=lp.register_account_snapshot(fresh)
                            assert publication['state']=='registered',publication
                            native_facts=publication['_planning_capture']['document']['account_financial_facts']
                            assert native_facts['snapshot_id'] and native_facts['trade_generation']==fresh['trade_generation']
                            native.update(original_packet=deepcopy(original_packet),public_packet=deepcopy(fresh),
                                public_native_facts=deepcopy(native_facts),held=deepcopy(held))
                        finally:gate['release'].set()
                        result=pending.result(timeout=5)
                native['failed_current']=deepcopy(result)
                assert len(account.orders)==3 and len(account.posts)==posts_before
                assert result['funds']['status']=='known' and Decimal(result['funds']['buy_reserved_usd'])==24
                assert result['active_plan'] is None and result['last_round']['actions']==[]
                assert result['last_round']['completed_at'] and result['last_round']['reason']=='account_round_invalid'
                completed=datetime.fromisoformat(result['last_round']['completed_at'])
                assert result['plan_wait'] is None
                assert len(trace['requests'])==len(trace['publishers'])==1
                assert gate['records'][0]['rows_identity']==gate['records'][0]['returned_rows_identity'] and gate['errors']==[]
                assert execution.lp_auto_set_desired_running(False)['pause_confirmed']
                active_trace = None
                early = execution.lp_auto_run_once(round_id=f'paused-current-refill-{cycle}')
                assert early['last_round']['round_id'] != result['last_round']['round_id']
                assert early['last_round']['actions'] == [] and len(account.posts) == posts_before
                assert execution.lp_auto_set_desired_running(True)['desired_running']
                active_trace=None
                advance_to_due();publish_qualification();lp.refresh_candidate_recommendations()
                trace=start_trace();active_trace=trace
                result=execution.lp_auto_run_once(round_id=f'fresh-refill-{cycle}')
                assert result['last_round']['round_id']!=native['failed_current']['last_round']['round_id']
                assert len(account.orders)==5 and result['slots']['occupied']==5 and Decimal(result['funds']['buy_reserved_usd'])==40
                assert_fence_accounting(trace)
                evidence['cycles'].append(deepcopy(dict(cycle=cycle,trace=trace,state=result)))
                active_trace=None
                assert len(account.posts)==7+2*cycle
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
                        assert limited['last_round']['reason'] == 'account_snapshot_incomplete'
                        component = limited['account_observation']['components']['positions']
                        assert component['state'] == 'failed' and component['error']['error_type'] == 'TimeoutError' and component['error']['error_chain'] == ['TimeoutError']
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
            assert result['funds']['status'] == 'unknown'
            assert_fence_accounting(trace)
            assert 1 <= account.position_reads - before <= 4
            assert Decimal(result['funds']['buy_reserved_usd'])==40
            evidence['cycles'].append(deepcopy(dict(cycle=cycle,trace=trace,state=result)))
            active_trace = None
        native_entries=[a for s in store.lp_sessions() for a in store.lp_actions(s['session_id']) if a.get('role')=='entry' and a['state']=='accepted']
        assert Counter(a['order_id'] for a in native_entries)==Counter(r['order_id'] for r in actual_receipts)
        assert all(any(r['order_id']==a['order_id'] and r['token_id']==a['token_id'] for r in actual_receipts) for a in native_entries)
        assert all(p.post_only is True and p.maker_amount==8000000 and p.taker_amount==20000000 for p in account.posts)
        assert account.market_orders==[]
    finally:
        def serialize(value):
            if is_dataclass(value):return asdict(value)
            if hasattr(value,'model_dump'):return value.model_dump(mode='json')
            return str(value)
        (store.path.parent/'IJ-protection-native-proof.json').write_text(json.dumps(dict(
            dependency=dependency,evidence=evidence,actual_receipts=actual_receipts,current=execution.lp_auto_state(),
            sessions=store.lp_sessions(),audits={s['session_id']:store.lp_actions(s['session_id']) for s in store.lp_sessions()},
            posts=account.posts,cancels=account.cancels),default=serialize,indent=2))
        try:
            settle_workers()
            record_property('cleanup', 'attention joined; public futures completed; market futures done; public readers zero')
        finally:
            adapter.close()
            assert adapter._lp_public_closed is True
            record_property('adapter_closed', 'true')
