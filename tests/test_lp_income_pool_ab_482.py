"""#482 A+B: real public owners; all exchange/HTTP boundaries are offline."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import socket

import pytest

from tests.test_lp_observed_pools import AsyncPoolRig, _measured_service


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('#482 tests must not connect to a network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def _roles(rig, names, *, income=''):
    saved = rig.store.lp_load_observed_pool_roles(rig.account)
    rig.store.lp_save_observed_pool_roles(rig.account, expected_revision=saved['revision'],
        roles={rig.cid(n): dict(pool='income' if n in income else 'trial', episode='old-'+n) for n in names})


def test_income_gap_reports_each_skip_without_changing_selection(tmp_path, monkeypatch):
    scenarios = ['no_sample', 'unknown', 'expired', 'old_source', 'missing_row', 'minimum', 'stress', 'comparison', 'bounded']
    for scenario in scenarios:
        rig = AsyncPoolRig(tmp_path/scenario, monkeypatch, trial='1000', wallet='2000')
        try:
            names = 'AB' if scenario in ('minimum', 'comparison') else ''.join(chr(65+i) for i in range(25)) if scenario=='bounded' else 'A'
            for n in names:
                rig.market(n, minimum='100' if scenario=='minimum' and n=='B' else '1')
                rig.add_order(n, '100' if scenario=='minimum' and n=='B' else '1')
            _roles(rig, names, income='A' if scenario=='comparison' else '')
            if scenario in ('no_sample','unknown','bounded'):
                rig.observe_initial_measurement({n:'1' for n in names})
                _roles(rig,names)
                rig.tick(120)
                # Keep the missing-share negative case after the new rest window.
                rig.converge_current(lambda: all(rig.live.pool_measurement_state()['measurement_attempts'].get(
                    rig.cid(n),{}).get('result',{}).get('reason')=='reward_share_unknown' for n in names),
                    drive=lambda:rig.live.run_once(pool='trial'))
                measured=None
            else:
                measured = rig.measure({n:'1' for n in names if scenario!='comparison' or n!='A'})
            if scenario=='expired':
                rig.hourly.clear()  # current share source lacks this positive-pool market
                rig.tick(121)
            elif scenario=='old_source':
                rig.release('old-A');rig.add_order('A','1',order_id='replacement-A')
                rig.publish_account({'replacement-A'})
                saved=rig.store.lp_load_observed_pool_roles(rig.account)
                rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=saved['revision'],
                    roles={rig.cid('A'):dict(pool='trial',episode='replacement-A')})
            elif scenario=='stress':
                from polymarket.models.clob.order_book import OrderBookLevel
                rig.markets['A']['direction']['book']['bids']=[dict(price='.4',size='10000'),dict(price='.39',size='3')]
                book = rig.public.get_order_book
                rig.public.get_order_book=lambda **kw: book(**kw).model_copy(update=dict(bids=(
                    OrderBookLevel(price=Decimal('.4'),size=Decimal('10000')),
                    OrderBookLevel(price=Decimal('.39'),size=Decimal('3')))))
            if scenario=='missing_row':
                # Captured source has no candidate row; current account still has A.
                original=rig.capture
                from dataclasses import replace
                rig.capture=lambda:replace(original(),value=())
            outcome = rig.cycle(pool='income')
            rig.finish_cancels(outcome)
            before=(len(rig.requests),deepcopy(rig.sdk.posts),deepcopy(rig.sdk.cancels),rig.store.lp_trade_generation())
            state=rig.execution.lp_auto_state()['observed_pools']
            assert 'income_gaps' in state, scenario
            gaps=state['income_gaps']; items=gaps['items']
            expected={'no_sample':'sample_unknown:reward_share_unknown','unknown':'sample_unknown:reward_share_unknown',
                'expired':'sample_expired','old_source':'source_not_current','missing_row':'candidate_row_missing',
                'minimum':'below_minimum','stress':'exit_liquidity_insufficient','comparison':'comparison_deferred',
                'bounded':'sample_unknown:reward_share_unknown'}[scenario]
            if scenario=='stress':
                assert expected not in {r['reason'] for r in items}
                estimate,=state['income_estimates']
                assert estimate['stress_exit']['reason_codes']==['exit_liquidity_insufficient']
                assert estimate['stress_exit']['loss_ratio'] is None
            else:
                assert expected in {r['reason'] for r in items}, (scenario,gaps)
            assert all(r['condition_id'] and len(r['condition_id'])<=96 for r in items)
            if scenario=='bounded':
                assert gaps['total']==25 and len(items)==20 and gaps['omitted']==5
            assert gaps['total']==len(items)+gaps['omitted']
            if scenario in ('minimum','stress'):
                assert {o.key[3] for o in outcome.rotation.victims}=={'old-A'}
                assert set(oid for batch in rig.sdk.cancels for oid in batch)=={'old-A'}
            else:
                assert rig.sdk.cancels==[] and rig.sdk.posts==[]
            detached=deepcopy(state)
            items.clear()
            assert rig.execution.lp_auto_state()['observed_pools']==detached
            assert before==(len(rig.requests),rig.sdk.posts,rig.sdk.cancels,rig.store.lp_trade_generation())
        finally:
            rig.close()


def test_latest_measurement_attempt_does_not_replace_retained_valid_sample(tmp_path, monkeypatch):
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    from tests.test_lp_d2_execution_bridge import attach
    from open_trader.polymarket_lp import PolymarketLPService
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        rig.market('A');rig.add_order('A','1')
        known=rig.measure(dict(A='1'))['observations'][rig.cid('A')]['pool_result']
        rig.add_order('A','1',order_id='other-A')
        rig.tick()
        def drive():rig.live.run_once(pool='trial')
        rig.converge_current(lambda:rig.execution.lp_observed_pool_state()['measurement_attempts'].get(
            rig.cid('A'),{}).get('result',{}).get('reason')=='multiple_orders',drive=drive)
        state=rig.execution.lp_observed_pool_state()
        assert state['results'][rig.cid('A')]['result']==known
        attempt=state['measurement_attempts'][rig.cid('A')]
        assert attempt['result']['state']=='unknown' and attempt['result']['reason']=='multiple_orders'
        assert attempt['checked_at']!=known['measured_at']
        attempt['result']['reason']='changed by reader'
        assert rig.execution.lp_observed_pool_state()['measurement_attempts'][rig.cid('A')]['result']['reason']=='multiple_orders'
        assert len(state['measurement_attempts'])<=64
        assert rig.live.close(3)
        new=LPD2LiveExecution(rig.store,rig.adapter,account_id=rig.adapter.config.wallet_address,
            venue='polymarket',process_epoch='measurement-restart',policy=CalculationPolicy(None))
        rig.live=new
        execution=attach(new,PolymarketLPService(rig.store,rig.adapter),tmp_path)
        assert execution.lp_observed_pool_state()['measurement_attempts']=={}
        assert execution.lp_observed_pool_state()['results'][rig.cid('A')]['result']==known
        assert new.pool_result_projection()=={}  # disk history grants no new process authority
    finally:rig.close()


def test_monitor_shows_observation_stall_after_120_and_clears_on_refresh(tmp_path, monkeypatch):
    from open_trader.lp_monitor_readonly import safe_summary,render_page
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        rig.market('A');rig.add_order('A','1')
        rig.measure(dict(A='1'))
        before=(rig.store.lp_trade_generation(),len(rig.sdk.posts),len(rig.sdk.cancels),len(rig.requests))
        for delta in (120,121):
            summary=safe_summary(rig.execution.lp_monitor_summary(now=rig.utc()+timedelta(seconds=delta)))
            attempt,=summary['pools']['measurement_attempts']
            assert attempt['sample_age_seconds']>=delta
            assert summary['pools']['reward_share']['age_seconds']>=delta
            assert '观测刷新停滞' not in render_page(summary)
            assert '每单样本年龄' in render_page(summary)
            assert before==(rig.store.lp_trade_generation(),len(rig.sdk.posts),len(rig.sdk.cancels),len(rig.requests))
        rig.add_order('A','1',order_id='other-A');rig.tick()
        rig.converge_current(lambda:rig.execution.lp_observed_pool_state()['measurement_attempts'][rig.cid('A')][
            'result'].get('reason')=='multiple_orders',drive=lambda:rig.live.run_once(pool='trial'))
        summary=safe_summary(rig.execution.lp_monitor_summary(now=rig.utc()))
        attempt,=summary['pools']['measurement_attempts']
        assert attempt['reason']=='multiple_orders'
        attempt['reason']='<script>secret</script>'
        rendered=render_page(summary)
        assert '<script>' not in rendered and '&lt;script&gt;' in rendered
    finally:rig.close()


def test_shortage_timer_survives_unknown_and_restart_is_unknown():
    from datetime import UTC, datetime
    from open_trader.polymarket_lp_monitor import LPMonitorSummary
    at=datetime(2026,10,27,tzinfo=UTC)
    account='offline-wallet'
    auto=dict(trial_target_buy_count=3,income_target_buy_count=3,observed_pools=dict(current_membership=dict(
        state='known',account_id=account,observed_at=at.isoformat(),orders=[])))
    monitor=LPMonitorSummary()
    def read(seconds, *, unknown=False, full=False, fresh=True, owner=monitor):
        current=deepcopy(auto)
        member=current['observed_pools']['current_membership']
        if fresh:member['observed_at']=(at+timedelta(seconds=seconds)).isoformat()
        if unknown:member['state']='unknown'
        if full:
            member['orders']=[dict(account_id=account,condition_id='c'+str(i),token_id='t'+str(i),
                order_id='o'+str(i),side='BUY',pool=pool) for pool in ('trial','income') for i in
                (range(3) if pool=='trial' else range(3,6))]
        return owner.summarize(auto=current,dashboard={},trades={},audits=[],account_id=account,
            now=at+timedelta(seconds=seconds))['pools']['items']['income']
    assert read(0)['shortage_seconds'] is None
    assert read(20,fresh=False)['shortage_seconds'] is None
    assert read(100)['shortage_seconds']==100
    assert read(200,unknown=True)['shortage_seconds'] is None
    assert read(400)['shortage_seconds']==400
    assert read(400,owner=LPMonitorSummary())['shortage_seconds'] is None
    assert read(401,full=True)['shortage_seconds'] is None
    assert read(402)['shortage_seconds'] is None
    assert read(403)['shortage_seconds']==1


def _finish(handles):
    for handle in handles:
        handle.result(3)
        handle._thread.join(3)
        assert not handle._thread.is_alive()


def _cancel_reasons(rig):
    return [(t.order_key[3],t.reason) for p in rig.live.state.snapshot().progress.values() for t in p.cancel_targets]


def test_managed_partial_buy_cancels_remainder_once_and_keeps_passive_sell(tmp_path, monkeypatch):
    for pool in ('trial','income'):
        rig=AsyncPoolRig(tmp_path/pool,monkeypatch)
        try:
            rig.market('A');rig.add_order('A','8')
            rig.sdk.orders[0]['size_matched']=Decimal('5')
            rig.sdk.orders[0]['status']='PARTIALLY_FILLED'
            rig.sdk.positions=(dict(condition_id=rig.cid('A'),token_id='tA',size='5',average_price='.4'),)
            rig.observe_initial_measurement({'A':'1'})
            _roles(rig,'A',income='A' if pool=='income' else '')
            rig.cancel_removes=True
            # The offline exchange publishes the actual side sent by the owner.
            rig.sdk.create_limit_order=lambda **kw:dict(kw,order_type='GTC' if kw.get('expiration') is None else 'GTD')
            post=rig.sdk.post_order
            def publish(signed):
                receipt=post(signed)
                row=rig.history[receipt['order_id']]
                row.update(side=signed['side'],order_type=signed['order_type'])
                return receipt
            rig.sdk.post_order=publish
            _finish(rig.live.run_protection_once())
            assert rig.sdk.cancels==[('old-A',)], pool
            assert ('old-A','buy_partial_fill') in _cancel_reasons(rig)
            for _ in range(3):_finish(rig.live.run_protection_once())
            assert rig.sdk.cancels==[('old-A',)]
            try:
                rig.converge_current(lambda:any(p['side']=='SELL' for p in rig.sdk.posts))
            except AssertionError:
                raise AssertionError(rig.live.holdings_summary()) from None
            sells=[p for p in rig.sdk.posts if p['side']=='SELL']
            assert len(sells)==1 and sells[0]['size']==5
            assert sells[0]['price']>=Decimal('.4') and sells[0]['post_only'] is True
            outcome=rig.cycle(pool='trial',qualified=True)
            rig.finish_cancels(outcome)
            assert not [p for p in rig.sdk.posts if p['side']=='BUY']
            assert all('new-' not in oid for batch in rig.sdk.cancels for oid in batch)
            assert rig.sdk.market_orders==[]
        finally:rig.close()


def test_partial_buy_protection_preserves_unknown_identity_and_mode_boundaries(tmp_path, monkeypatch):
    for case in ('unknown','filled','terminal','manual','unclassified','order','token','account','single','sell'):
        rig=AsyncPoolRig(tmp_path/case,monkeypatch)
        try:
            rig.market('A');rig.add_order('A','8')
            rig.observe_initial_measurement({'A':'1'})
            row=rig.sdk.orders[0]
            row.update(size_matched=Decimal('5'),status='PARTIALLY_FILLED')
            if case=='unknown':row['size_matched']=None
            elif case=='filled':row.update(size_matched=Decimal('20'),status='FILLED')
            elif case=='terminal':row['status']='CANCELED'
            elif case=='sell':row['side']='SELL'
            if case in ('filled','terminal'):rig.sdk.orders=()
            rig.tick()
            if case!='unknown':rig.publish_account(set() if case in ('filled','terminal','sell') else {'old-A'})
            else:
                rig.pump(lambda:rig.live.state.snapshot().observation.components['open_orders'].state=='unknown',
                    drive=lambda:rig.live.run_once())
            if case not in ('manual','unclassified'):
                role=dict(pool='trial',episode='other-A' if case=='order' else 'old-A')
                if case=='token':role['token_id']='wrong-token'
                if case=='account':role['account_id']='wrong-account'
                saved=rig.store.lp_load_observed_pool_roles(rig.account)
                rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=saved['revision'],roles={rig.cid('A'):role})
            if case=='single':
                settings=rig.store.lp_load_current_settings(rig.account)
                rig.store.lp_save_current_settings(rig.account,expected_config_version=settings['config_version'],changes=dict(allocation_mode='minimum'))
                rig.live.load_settings()
            for _ in range(3):_finish(rig.live.run_protection_once())
            assert not [p for p in _cancel_reasons(rig) if p[1]=='buy_partial_fill'],case
            assert rig.sdk.posts==[] and rig.sdk.market_orders==[]
            assert rig.sdk.cancels==[],case
        finally:rig.close()


def _timed_rig(path, monkeypatch, *, pool='trial'):
    from tests.test_lp_cancel_flow_contract import CancelRig
    rig=CancelRig(path,monkeypatch)
    rig.market('A');rig.add_order('A','8')
    _roles(rig,'A',income='A' if pool=='income' else '')
    rig.live.run_once()
    return rig


def _sample(rig, *, order_id='old-A', age=0, value='0'):
    return dict(state='known',account_id=rig.account,condition_id=rig.cid('A'),
        measured_at=(rig.date[0]-timedelta(seconds=age)).isoformat(),yield_pct_per_hour=value,
        source_order=dict(order_id=order_id,token_id='tA',side='BUY',price='.4',
            remaining_quantity='20',original_quantity='20',filled_quantity='0'))


def _protect_at(rig, seconds, *, utc_delta=0):
    rig.seconds[0]=seconds
    rig.date[0]+=timedelta(seconds=utc_delta)
    rig.now=rig.date[0]
    rig.live.run_once()
    _finish(rig.live.run_protection_once())


def test_trial_measurement_unknown_cancels_at_600_once(tmp_path, monkeypatch):
    for variant in ('missing','expired','wrong_source'):
        rig=_timed_rig(tmp_path/variant,monkeypatch)
        try:
            if variant!='missing':
                rig.store.save_lp_observation(rig.account,rig.cid('A'),dict(pool_result=_sample(
                    rig,age=121 if variant=='expired' else 0,order_id='previous-A' if variant=='wrong_source' else 'old-A')))
            _protect_at(rig,0)
            # The existing model result is known; its old UNKNOWN timer cannot explain the cancel.
            assert all(y.yield_pct_per_hour.state=='known' for y in rig.live.state.snapshot().yields.values())
            assert rig.live.state.snapshot().yields
            _protect_at(rig,0,utc_delta=86400)
            _protect_at(rig,599)
            assert rig.sdk.cancels==[]
            _protect_at(rig,600)
            assert rig.sdk.cancels==[('old-A',)],variant
            assert ('old-A','measured_yield_unknown_timeout') in _cancel_reasons(rig)
            for _ in range(3):_finish(rig.live.run_protection_once())
            assert rig.sdk.cancels==[('old-A',)]
            assert rig.sdk.posts==rig.sdk.market_orders==[]
        finally:rig.close()


def test_trial_measurement_timer_resets_on_valid_sample_and_restart(tmp_path, monkeypatch):
    from tests.test_lp_income_pool_v4_482 import _zero_at_600
    with monkeypatch.context() as scoped:
        _zero_at_600(tmp_path/'zero-reset',scoped)
    rig=_timed_rig(tmp_path/'restart',monkeypatch)
    try:
        _protect_at(rig,0);_protect_at(rig,599)
        assert rig.live.close(3)
        from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
        from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
        from open_trader.polymarket_lp_market_reader import MarketReader
        from open_trader.polymarket_lp_market_calculation import CalculationPolicy
        rig.live=LPD2LiveExecution(rig.store,rig.adapter,account_id=rig.adapter.config.wallet_address,
            venue='polymarket',process_epoch='measurement-restart',policy=CalculationPolicy(None),
            load_settings_on_start=True,reader=MarketReader(rig.adapter.lp_reader_source_port()),
            clock=lambda:rig.seconds[0],utc_now=lambda:rig.date[0])
        rig.live.initialize(ValidatedStartAdmission('measurement-restart','offline-permit'))
        cancel=rig.sdk.cancel_orders
        def reject_startup(*,order_ids):
            rig.sdk.cancels.append(tuple(order_ids))
            return dict(canceled=[],not_canceled={oid:'offline startup rejected' for oid in order_ids})
        rig.sdk.cancel_orders=reject_startup
        _protect_at(rig,599)
        # Existing startup cleanup is independent of the new measured timer.
        assert rig.sdk.cancels==[('old-A',)] and _cancel_reasons(rig)==[]
        rig.sdk.cancel_orders=cancel
        _protect_at(rig,1198)
        assert rig.sdk.cancels==[('old-A',)]
        _protect_at(rig,1199)
        assert rig.sdk.cancels==[('old-A',),('old-A',)]
        assert ('old-A','measured_yield_unknown_timeout') in _cancel_reasons(rig)
    finally:rig.close()


def test_measured_timeout_never_cancels_income_manual_or_single_pool(tmp_path, monkeypatch):
    for case in ('income','manual','unclassified','order','token','account','single'):
        rig=_timed_rig(tmp_path/case,monkeypatch,pool='income' if case=='income' else 'trial')
        try:
            if case in ('manual','unclassified','order','token','account'):
                saved=rig.store.lp_load_observed_pool_roles(rig.account)
                role=dict(pool='trial',episode='other-A' if case=='order' else 'old-A')
                if case=='token':role['token_id']='wrong-token'
                if case=='account':role['account_id']='wrong-account'
                rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=saved['revision'],
                    roles={} if case in ('manual','unclassified') else {rig.cid('A'):role})
            elif case=='single':
                saved=rig.store.lp_load_current_settings(rig.account)
                rig.store.lp_save_current_settings(rig.account,expected_config_version=saved['config_version'],changes=dict(allocation_mode='minimum'))
                rig.live.load_settings()
            if case in ('income','boundary'):
                rig.store.save_lp_observation(rig.account,rig.cid('A'),dict(pool_result=_sample(rig,age=121 if case=='income' else 120)))
            _protect_at(rig,0);_protect_at(rig,600)
            assert rig.sdk.cancels==[],case
            assert not [p for p in _cancel_reasons(rig) if p[1]=='measured_yield_unknown_timeout']
            if case=='boundary':
                _protect_at(rig,600,utc_delta=.001)
                _protect_at(rig,1199)
                assert rig.sdk.cancels==[]
                _protect_at(rig,1200)
                assert rig.sdk.cancels==[('old-A',)]
            assert rig.sdk.posts==rig.sdk.market_orders==[]
        finally:rig.close()


def test_latest_measurement_attempt_records_failed_refresh_without_refreshing_sample(tmp_path,monkeypatch):
    from threading import Event
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        rig.market('A');rig.add_order('A','1')
        known=rig.measure(dict(A='1'))['observations'][rig.cid('A')]['pool_result']
        previous=rig.execution.lp_observed_pool_state()
        attempts=deepcopy(previous['measurement_attempts'])
        original_success=previous['observation_refresh']['last_success_at']
        sdk=rig.sdk;calls=[]
        for name in ('get_balance_allowance','list_open_orders','list_account_trades','list_positions','get_order_scoring'):
            method=getattr(sdk,name)
            def counted(*args,_method=method,_name=name,**kwargs):
                calls.append(_name);return _method(*args,**kwargs)
            monkeypatch.setattr(sdk,name,counted)
        original_id=rig.sdk.orders[0]['id'];rig.sdk.orders[0]['id']=''
        assert rig.store.lp_advance_trade_generation(rig.store.lp_trade_generation())
        failed=rig.execution.refresh_lp_observations()
        assert failed['state']=='unknown' and failed['reason']=='account_facts_unknown'
        state=rig.execution.lp_observed_pool_state()
        assert state['measurement_attempts']==attempts
        assert state['results'][rig.cid('A')]['result']==known
        assert state['observation_refresh']['last_success_at']==original_success
        detached=deepcopy(state);state['measurement_attempts'][rig.cid('A')]['result']['state']='reader mutation'
        before=deepcopy(rig.sdk.orders);before_calls=list(calls);generation=rig.store.lp_trade_generation()
        assert rig.execution.lp_observed_pool_state()==detached
        stop=Event();stop.set()
        assert rig.execution.refresh_lp_observations(stop_event=stop)['state']=='cancelled'
        assert rig.execution.lp_observed_pool_state()['measurement_attempts']==attempts
        assert rig.sdk.orders==before and calls==before_calls and rig.store.lp_trade_generation()==generation
        rig.sdk.orders[0]['id']=original_id
        assert rig.store.lp_advance_trade_generation(rig.store.lp_trade_generation())
        assert rig.execution.refresh_lp_observations()['state']=='ready'
        assert rig.execution.lp_observed_pool_state()['measurement_attempts']==attempts
    finally:rig.close()
