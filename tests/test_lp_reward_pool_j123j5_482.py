"""Approved #482 J1/J2/J3/J5 contracts at public execution/display seams."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta


def test_unknown_monitor_keeps_last_confirmed_counts():
    from open_trader.polymarket_lp_monitor import LPMonitorSummary
    from open_trader.lp_monitor_readonly import safe_summary, render_page
    now = datetime(2026, 10, 29, tzinfo=UTC)
    monitor = LPMonitorSummary()
    auto = dict(effective_settings=dict(trial_target_buy_count=3, income_target_buy_count=3),
        observed_pools=dict(current_membership=dict(state='known', account_id='acct', observed_at=now,
            orders=[dict(account_id='acct', condition_id=name, token_id='t'+name, order_id=name,
                side='BUY', pool=pool) for name, pool in [('A','trial'),('B','trial'),('C','income')]])))
    def summary(owner=monitor, account='acct', at=now):
        return owner.summarize(auto=auto, dashboard={}, trades={}, audits=[], account_id=account, now=at)
    confirmed = summary()
    assert confirmed['orders']['counts'] == dict(trial=2, income=1, unassigned=0, sell=0)
    auto['observed_pools']['current_membership'].update(state='unknown', reason='orders_not_current_complete',
        observed_at=now+timedelta(seconds=5), orders=[])
    unknown = summary(at=now+timedelta(seconds=5))
    assert unknown['orders']['status'] == 'unknown'
    assert unknown['orders']['counts'] == confirmed['orders']['counts']
    assert unknown['orders']['confirmed_at'] == now.isoformat()
    assert unknown['orders']['counts_basis'] == 'last_confirmed'
    assert unknown['orders']['unconfirmed_reason'] == 'orders_not_current_complete'
    assert unknown['orders']['items'] == []
    assert unknown['balance']['capacity_usd'] is None
    for pool, count in [('trial',2),('income',1)]:
        assert unknown['pools']['items'][pool]['count'] == count
        assert unknown['pools']['items'][pool]['status'] == 'unknown'
        assert unknown['pools']['items'][pool]['used_usd'] is None
    projected = safe_summary(unknown)
    assert projected['orders']['confirmed_at'] == now.isoformat()
    page = render_page(projected)
    assert '最近确认' in page and '试挂 2 / 收益 1' in page
    assert 'orders_not_current_complete' in page and now.isoformat() in page
    import json
    from tests.test_dashboard_web import run_dashboard_js
    projected['orders']['unconfirmed_reason']='<script>orders_not_current_complete</script>'
    html=json.loads(run_dashboard_js('console.log(JSON.stringify(lpMonitorCards('+json.dumps(projected)+')));'))
    assert '最近确认：试挂 2 / 收益 1' in html and now.isoformat() in html
    assert '最近确认 / 目标' in html and '&lt;script&gt;orders_not_current_complete' in html
    assert '<script>orders_not_current_complete' not in html
    assert summary(LPMonitorSummary())['orders']['counts'] is None
    assert summary(account='other')['orders']['counts'] is None
    assert summary()['orders']['counts'] is None  # switching back cannot revive account history


def test_zero_yield_never_promotes_and_gap_is_visible(tmp_path, monkeypatch):
    from decimal import Decimal
    from tests.test_lp_observed_pools import AsyncPoolRig
    from open_trader.lp_monitor_readonly import render_page
    from open_trader.polymarket_lp_observed_pools import select_income_results
    for variant in ('zero', 'positive', 'unknown', 'mismatched'):
        rig = AsyncPoolRig(tmp_path/variant, monkeypatch)
        try:
            rig.market('A'); rig.add_order('A','1')
            rig.measure(dict(A='0' if variant=='zero' else '.1'))
            if variant=='unknown':
                rig.tick(121)  # detached sample is now expired, no current reward-share proof
                rig.hourly.clear()
                original = rig.http
                import httpx
                async def unavailable(req):
                    if req.headers['x-lp-source']=='reward_share':return httpx.Response(503)
                    return await original(req)
                rig.http=unavailable
            if variant=='mismatched':
                rig.release('old-A'); rig.add_order('A','1',order_id='manual-A')
                rig.publish_account({'manual-A'})
            result=rig.cycle(pool='income'); rig.finish_cancels(result)
            if variant=='positive':
                assert {oid for batch in rig.sdk.cancels for oid in batch}=={'old-A'}
                assert result.selection.targets==(rig.cid('A'),)
                assert rig.sdk.posts==[]  # cancel receipt is not actual release
            else:
                assert rig.sdk.cancels==rig.sdk.posts==[]
            if variant=='zero':
                state=rig.execution.lp_observed_pool_state()
                assert Decimal(state['results'][rig.cid('A')]['result']['yield_pct_per_hour'])==0
                summary=rig.execution.lp_monitor_summary(now=rig.utc())
                assert any(r['reason']=='zero_yield' and r['order_id']=='old-A'
                    for r in summary['pools']['income_gaps']['items'])
                assert 'zero_yield' in render_page(summary)
        finally:rig.close()
    for value in ('0','-1','NaN','Infinity','-Infinity'):
        assert select_income_results({'A':dict(eligible=True,result=dict(yield_pct_per_hour=value))})['members']==()


def test_better_trial_replaces_only_worst_income_order(tmp_path, monkeypatch):
    from decimal import Decimal
    from tests.test_lp_observed_pools import AsyncPoolRig
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for name in 'ABCDE':rig.market(name)
        for name,capital in [('A','50'),('B','30'),('C','20'),('D','1'),('E','1')]:rig.add_order(name,capital)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income' if n in 'ABC' else 'trial',episode='old-'+n) for n in 'ABCDE'})
        rig.measure(dict(A='.6',B='1',C='.4',D='.8',E='.5'))
        retained={n:deepcopy(rig.history['old-'+n]) for n in 'AB'}
        result=rig.cycle(pool='income');rig.finish_cancels(result)
        assert {o.key[3] for o in result.rotation.victims}=={'old-C','old-D'}
        assert {oid for batch in rig.sdk.cancels for oid in batch}=={'old-C','old-D'}
        assert set(result.selection.targets)=={rig.cid(n) for n in 'ABD'}
        assert rig.sdk.posts==[]
        # New manual same-market IDs acquire no authority from the old exact roles.
        rig.add_order('C','1',order_id='manual-C')
        rig.release('old-C','old-D')
        bought=rig.cycle(pool='income');rig.finish_cancels(bought)
        if not rig.sdk.posts:rig.finish_income_rebuild(rig.cid('D'),'tD',after=rig.utc())
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[('tD',Decimal('20'))]
        assert all(rig.history['old-'+n]==retained[n] for n in 'AB')
        assert {'old-A','old-B','old-E','manual-C'} <= {r['id'] for r in rig.sdk.orders}
        assert not {'old-A','old-B','old-E','manual-C'} & {oid for batch in rig.sdk.cancels for oid in batch}
    finally:rig.close()
    # Equal yield and worse candidates preserve exact incumbents.
    for rate in ('.4','.3999'):
        rig=AsyncPoolRig(tmp_path/rate,monkeypatch)
        try:
            for n in 'ABCD':rig.market(n);rig.add_order(n,dict(A='50',B='30',C='20',D='1')[n])
            rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
                roles={rig.cid(n):dict(pool='income',episode='old-'+n) for n in 'ABC'})
            rig.measure(dict(A='.6',B='1',C='.4',D=rate))
            stable=rig.cycle(pool='income');rig.finish_cancels(stable)
            assert rig.sdk.cancels==rig.sdk.posts==[]
        finally:rig.close()


def test_d2_waits_two_minutes_before_counting_sample(tmp_path, monkeypatch):
    from decimal import Decimal
    from tests.test_lp_observed_pools import AsyncPoolRig
    from tests.test_lp_income_pool_ab_482 import _finish
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        rig.market('A'); rig.add_order('A','1')
        rig.hourly['A']=Decimal(0)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid('A'):dict(pool='trial',episode='old-A')})
        rig.observe_initial_measurement({'A':'0'})
        def attempt():return rig.execution.lp_observed_pool_state()['measurement_attempts'].get(rig.cid('A'),{})
        rig.pump(lambda:bool(attempt()),drive=lambda:rig.live.run_once(pool='trial'))
        start=rig.seconds
        rig.tick(30)
        rig.pump(lambda:attempt().get('checked_at')==rig.utc().isoformat(),drive=lambda:rig.live.run_once(pool='trial'))
        assert attempt()['result']['state']=='unknown'
        assert attempt()['result']['reason']=='sample_warming_up'
        assert attempt()['result']['yield_pct_per_hour'] is None
        # Keep the external scoring/share cadence running before the boundary.
        for _ in range(2):
            rig.tick(30)
            rig.pump(lambda:attempt().get('checked_at')==rig.utc().isoformat(),drive=lambda:rig.live.run_once(pool='trial'))
        rig.tick(start+119.999-rig.seconds)
        rig.pump(lambda:attempt().get('checked_at')==rig.utc().isoformat(),drive=lambda:rig.live.run_once(pool='trial'))
        assert attempt()['result']['reason']=='sample_warming_up'
        assert attempt()['result']['yield_pct_per_hour'] is None
        def sources_published():
            state=rig.execution.lp_observed_pool_state()
            runtime=rig.live.read_runtime.status()
            observation=rig.live.state.snapshot().observation
            market=observation.source_frame.sources.get('market_order:old-A')
            return (state['reward_share']['state']=='known' and state['reward_share']['age_seconds']<=60
                and market is not None and market.complete and market.checked_at==rig.utc()
                and runtime['ready_count']==0 and not any(r['active'] for r in runtime['scopes']))
        rig.pump(sources_published,drive=lambda:rig.live.run_once(pool='trial'))
        rig.tick(.001)
        def mature():
            rig.live.run_once(pool='trial')
            _finish(rig.live.run_protection_once())
        rig.pump(lambda:attempt()['result']['state']=='known',drive=mature)
        assert rig.seconds==start+120
        assert attempt()['result']['state']=='known', attempt()
        assert Decimal(attempt()['result']['yield_pct_per_hour'])==0
        measured=attempt()['result']['measured_at']
        assert datetime.fromisoformat(measured)<=rig.utc()
        zero=rig.cycle(pool='income');rig.finish_cancels(zero)
        assert rig.sdk.cancels==rig.sdk.posts==[]
        rig.measure(dict(A='.1'))
        promote=rig.cycle(pool='income');rig.finish_cancels(promote)
        assert {oid for batch in rig.sdk.cancels for oid in batch}=={'old-A'}
        rig.release('old-A')
        after=rig.utc()
        rig.finish_income_rebuild(rig.cid('A'),'tA',after=after)
        accepted_id=next(o['id'] for o in rig.sdk.orders)
        assert accepted_id!='old-A'
        def new_attempt():return attempt().get('order_id')==accepted_id
        rig.converge_current(new_attempt,drive=lambda:rig.live.run_once(pool='trial'))
        assert attempt()['result']['state']=='unknown' and attempt()['result']['reason']=='sample_warming_up'
        assert attempt()['result']['yield_pct_per_hour'] is None
        assert rig.live.pool_result_projection()[rig.cid('A')]['result']['source_order']['order_id']=='old-A'
    finally:rig.close()


def test_positive_trial_replaces_known_zero_income_member(tmp_path,monkeypatch):
    from tests.test_lp_observed_pools import AsyncPoolRig
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for n in 'ABCD':rig.market(n);rig.add_order(n,dict(A='50',B='30',C='20',D='1')[n])
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=0,
            roles={rig.cid(n):dict(pool='income',episode='old-'+n) for n in 'ABC'})
        rig.measure(dict(A='.6',B='1',C='0',D='.8'))
        retained={n:deepcopy(rig.history['old-'+n]) for n in 'AB'}
        replacing=rig.cycle(pool='income');rig.finish_cancels(replacing)
        assert {o.key[3] for o in replacing.rotation.victims}=={'old-C','old-D'}
        assert {oid for batch in rig.sdk.cancels for oid in batch}=={'old-C','old-D'}
        assert rig.sdk.posts==[]
        assert all(rig.history['old-'+n]==retained[n] for n in 'AB')
    finally:rig.close()
