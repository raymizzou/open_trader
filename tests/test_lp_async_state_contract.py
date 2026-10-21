"""Per-order market acceptance never grants financial authority."""
from test_lp_async_source_contract import independent_watchdog
from dataclasses import replace
from decimal import Decimal
import pytest
from open_trader.polymarket_lp_current_state import CurrentLPState, EffectiveControlTransition
from open_trader.polymarket_lp_market_reader import SourceResult, CapturedSources, MarketReader
from open_trader.polymarket_lp_market_calculation import CalculationPolicy, calculate_current
from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
from test_lp_async_read_runtime_contract import Clock, Boundary, demand, collect, LPReadRuntime, CID, NOW

def financial(state,clock,rows,*,complete=True):
    sources=dict(balance=SourceResult('balance',(100,100),checked_at=clock.utc()),
        open_orders=SourceResult('orders',rows,complete=complete,checked_at=clock.utc()),
        positions=SourceResult('positions',(),checked_at=clock.utc()))
    frame=MarketReader.from_captured_sources(CapturedSources('account',sources))
    policy=CalculationPolicy(Decimal(100))
    result=calculate_current(prepare_current_inputs(frame,None,policy,clock.utc()),policy,clock.utc())
    ticket=state.begin_observation('generation',tuple(sources))
    return state.accept(ticket,frame,result,evaluated_at=clock.utc())

def row(order,matched='0'):
    return dict(order_id=order,condition_id=CID,token_id='yes',side='BUY',status='LIVE',
        price='0.40',original_size='20',size_matched=matched,order_type='GTC',expiration=None)

def state(clock):
    value=CurrentLPState('account','epoch',clock=clock,utc_now=clock.utc)
    value.apply_effective_control(EffectiveControlTransition('account','epoch',('epoch',0),0,None,None,True,'startup'),'generation')
    return value

def test_per_order_watermarks_preserve_valid_out_of_order_results():
    clock=Clock();owner=state(clock);financial(owner,clock,(row('A'),row('B','10')))
    boundary=Boundary(clock);boundary.holds={('A','book')}
    runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc);runtime.start()
    try:
        runtime.reconcile((demand('A'),demand('B')))
        b=next(r for r in collect(runtime,boundary,lambda rows:any(r.demand.order_id=='B' for r in rows)) if r.demand.order_id=='B')
        assert owner.apply_market_attempt(b)=='applied'
        boundary.release.set()
        a=next(r for r in collect(runtime,boundary,lambda rows:any(r.demand.order_id=='A' for r in rows)) if r.demand.order_id=='A')
        assert owner.apply_market_attempt(a)=='applied'
        snapshot=owner.snapshot()
        by_id={key[3]:value for key,value in snapshot.yields.items()}
        assert float(by_id['A'].yield_pct_per_hour.value)==pytest.approx(0.892857142857)
        assert float(by_id['B'].yield_pct_per_hour.value)==pytest.approx(0.862068965517)
        assert {o.key[3]:o.price.value*o.remaining.value for o in snapshot.orders}=={'A':8,'B':4}
        assert snapshot.buy_reserved.value==12 and snapshot.new_capacity.value==88
        for changed in (replace(a,attempt=0),replace(a,demand=replace(a.demand,terms_version=2)),
            replace(a,demand=replace(a.demand,account='other')),
            replace(a,demand=replace(a.demand,generation='old')),
            replace(a,demand=replace(a.demand,process_epoch='old'))):
            assert owner.apply_market_attempt(changed) in ('stale','invalid_identity')
            assert owner.snapshot()==snapshot
    finally:boundary.release.set();runtime.reconcile(());assert runtime.close(4)

def test_timeout_and_expiry_remove_known_without_promoting_financial_authority():
    clock=Clock();owner=state(clock);financial(owner,clock,(row('A'),))
    boundary=Boundary(clock);runtime=LPReadRuntime(boundary.factory,clock=clock,utc_now=clock.utc);runtime.start()
    try:
        runtime.reconcile((demand('A'),))
        first=collect(runtime,boundary,lambda rows:bool(rows))[0]
        assert owner.apply_market_attempt(first)=='applied'
        assert owner.snapshot().yields[('account',CID,'yes','A')].yield_pct_per_hour.state=='known'
        # Book freshness is independently enforced, including the equality boundary.
        clock.value=10
        assert owner.snapshot().yields[('account',CID,'yes','A')].yield_pct_per_hour.state=='known'
        clock.value=10.001
        assert owner.snapshot().yields[('account',CID,'yes','A')].yield_pct_per_hour.value is None
        since=owner.snapshot().yields[('account',CID,'yes','A')].unknown_since_monotonic
        financial(owner,clock,(row('A'),),complete=False)
        assert owner.snapshot().new_capacity.state=='unknown'
        failed=replace(first,attempt=2,started=10,deadline=15,completed=15,complete=False,reason='timeout')
        clock.value=15
        assert owner.invalidate_market_attempt(failed)=='applied'
        fresh=owner.snapshot()
        assert fresh.new_capacity.state=='unknown'
        assert fresh.yields[('account',CID,'yes','A')].unknown_since_monotonic==since
        assert owner.apply_market_attempt(first)=='stale'
        assert owner.snapshot()==fresh
        # A market result cannot repair missing position cost or a failed order source.
        healthy=replace(first,attempt=3,started=15,deadline=20,completed=15)
        owner.apply_market_attempt(healthy)
        assert owner.snapshot().new_capacity.state=='unknown'
    finally:boundary.release.set();runtime.reconcile(());assert runtime.close(4)
