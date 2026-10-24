"""LP430: current identity grants membership; history grants no send authority."""
from decimal import Decimal

from tests.test_lp_observed_pools import AsyncPoolRig


def roles(rig, values):
    saved=rig.store.lp_load_observed_pool_roles(rig.account)
    rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=saved['revision'],
        roles={rig.cid(n):dict(pool=pool,episode=oid) for n,(pool,oid) in values.items()})


def test_income_candidates_are_only_current_trial_members(tmp_path,monkeypatch):
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for n in 'ABCH':
            rig.market(n)
            rig.add_order(n,'1')
        roles(rig,{n:('trial','old-'+n) for n in 'ABCH'})
        rig.measure(dict(A='.6',B='.5',C='.4',H='2'))
        rig.release('old-H')
        result=rig.cycle(pool='income')
        rig.finish_cancels(result)
        assert set(result.selection.targets)=={rig.cid(n) for n in 'ABC'}
        assert {o.key[3] for o in result.rotation.victims}=={'old-A','old-B','old-C'}
        assert all(p['token_id']!='tH' for p in rig.sdk.posts)
        assert not any(oid.startswith('income:'+rig.cid('H')+':') for _,oid,_ in rig.requests)
    finally:rig.close()


def test_income_candidate_rejects_sample_from_old_trial_episode(tmp_path,monkeypatch):
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for n in 'CDM':
            rig.market(n);rig.add_order(n,'1',order_id='manual-M' if n=='M' else None)
        roles(rig,{n:('trial','old-'+n) for n in 'CD'})
        rig.measure(dict(C='.4',D='2',M='3'))
        rig.release('old-D');rig.add_order('D','1',order_id='D-new')
        result=rig.cycle(pool='income');rig.finish_cancels(result)
        assert result.selection.targets==(rig.cid('C'),)
        assert {o.key[3] for o in result.rotation.victims}=={'old-C'}
        assert all(p['token_id'] not in ('tD','tM') for p in rig.sdk.posts)
        assert {'D-new','manual-M'} <= {o.key[3] for o in rig.live.state.snapshot().active_orders}
    finally:rig.close()


def test_expired_departed_income_member_cannot_block_current_trial(tmp_path,monkeypatch):
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for n in 'HD':rig.market(n);rig.add_order(n,'1')
        roles(rig,dict(H=('income','old-H'),D=('trial','old-D')))
        original=rig.measure(dict(H='2'))['observations'][rig.cid('H')]['pool_result']
        rig.release('old-H');rig.tick(121)
        rig.measure(dict(D='.5'))
        rig.observe_initial_measurement(dict(D='.5'))  # publish actual H departure before comparison
        result=rig.cycle(pool='income');rig.finish_cancels(result)
        assert result.selection.targets==(rig.cid('D'),)
        assert {o.key[3] for o in result.rotation.victims}=={'old-D'}
        assert rig.sdk.posts==[]
        rig.release('old-D')
        rig.tick()
        rig.converge_current(lambda:bool(rig.sdk.posts))
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[('tD',Decimal('50'))]
        assert rig.execution.lp_observed_pool_state()['results'][rig.cid('H')]['result']==original
    finally:rig.close()


def test_fresh_departed_income_role_cannot_reopen_historical_order(tmp_path,monkeypatch):
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for n in 'HD':rig.market(n);rig.add_order(n,'1')
        roles(rig,dict(H=('income','old-H'),D=('trial','old-D')))
        rig.measure(dict(H='2',D='.5'))
        rig.release('old-H')
        result=rig.cycle(pool='income');rig.finish_cancels(result)
        assert result.selection.targets==(rig.cid('D'),)
        assert {o.key[3] for o in result.rotation.victims}=={'old-D'}
        assert all(p['token_id']!='tH' for p in rig.sdk.posts)
    finally:rig.close()


def test_restart_does_not_restore_fresh_income_handoff(tmp_path,monkeypatch):
    import httpx
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    from open_trader.polymarket_lp_scheduler import LPAutoScheduler
    from tests.test_lp_d2_execution_bridge import attach
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        rig.market('D');rig.add_order('D','1')
        roles(rig,dict(D=('trial','old-D')))
        rig.measure(dict(D='.5'))
        admitted=rig.cycle(pool='income');rig.finish_cancels(admitted)
        assert {o.key[3] for o in admitted.rotation.victims}=={'old-D'}
        rig.release('old-D')
        assert rig.live.close(timeout=3)
        rig.live=LPD2LiveExecution(rig.store,rig.adapter,account_id=rig.adapter.config.wallet_address,
            venue='polymarket',process_epoch='LP430-restart',policy=CalculationPolicy(None),
            load_settings_on_start=True,clock=lambda:rig.seconds,utc_now=rig.utc,
            read_transport=httpx.MockTransport(rig.http))
        rig.execution=attach(rig.live,rig.producer,tmp_path)
        rig.live.initialize(ValidatedStartAdmission('LP430-restart','offline-permit'))
        rig.scheduler=LPAutoScheduler(rig.execution,clock=rig.utc)
        rig.pump(lambda:rig.live.rounds.snapshot().round_number>0)
        result=rig.cycle(pool='income');rig.finish_cancels(result)
        assert rig.sdk.posts==[]
        trial=rig.cycle(pool='trial');rig.finish_cancels(trial)
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[('tD',Decimal('1'))]
    finally:rig.close()


def test_public_pool_membership_uses_current_exact_order_facts(tmp_path,monkeypatch):
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for n in 'ACH':rig.market(n);rig.add_order(n,'1')
        roles(rig,{n:('trial','old-'+n) for n in 'ACH'})
        rig.measure(dict(C='1',H='2'))
        rig.release('old-H')
        admitted=rig.cycle(pool='income');rig.finish_cancels(admitted)
        rig.market('B');rig.add_order('B','50',order_id='income-B')
        saved=rig.store.lp_load_observed_pool_roles(rig.account)
        rig.store.lp_save_observed_pool_roles(rig.account,expected_revision=saved['revision'],
            roles={**saved['roles'],rig.cid('B'):dict(pool='income',episode='income-B')})
        rig.add_order('A','1',order_id='manual-A')
        rig.cycle(pool='trial')
        rig.tick()
        rig.converge_current(lambda:rig.live.state.snapshot().observation.read_ticket.trade_version_at_start==rig.live.state.snapshot().trade_version)
        rig.pump(lambda:rig.live.read_runtime.status()['ready_count']==0
            and not any(s['active'] for s in rig.live.read_runtime.status()['scopes'])
            and rig.live.read_runtime.status()['observed_monotonic']>=rig.seconds)
        before=(rig.live.state.snapshot(),len(rig.requests),rig.store.lp_load_observed_pool_roles(rig.account))
        state=rig.execution.lp_auto_state()['observed_pools']['current_membership']
        again=rig.execution.lp_auto_state()['observed_pools']['current_membership']
        assert state==again and state['state']=='known'
        entries={o['order_id']:o for o in state['orders']}
        assert entries['old-A']['pool']=='trial'
        assert entries['income-B']['pool']=='income'
        assert entries['old-C']['pool']=='promotion'
        assert entries['manual-A']['pool']=='unclassified'
        assert 'old-H' not in entries
        assert before==(rig.live.state.snapshot(),len(rig.requests),rig.store.lp_load_observed_pool_roles(rig.account))
        rig.sdk.orders=None
        rig.cycle(pool='income')
        assert rig.execution.lp_auto_state()['observed_pools']['current_membership']['state']=='unknown'
    finally:rig.close()


def test_trial_refills_after_source_release_before_income_post(tmp_path,monkeypatch):
    from threading import Event
    gate,entered=Event(),Event()
    held=[False]
    original_http=AsyncPoolRig.http
    async def hold_income(self,req):
        import asyncio
        if held[0] and req.headers['x-lp-source']=='book' and req.headers['x-lp-order'].startswith('income:'):
            entered.set()
            await asyncio.get_running_loop().run_in_executor(None,gate.wait)
        return await original_http(self,req)
    monkeypatch.setattr(AsyncPoolRig,'http',hold_income)
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        for n in 'ABCE':rig.market(n,minimum='1',pool='1000' if n=='E' else '100')
        for n in 'ABC':rig.add_order(n,'1')
        roles(rig,{n:('trial','old-'+n) for n in 'ABC'})
        rig.measure(dict(A='.8'))
        admitted=rig.cycle(pool='income');rig.finish_cancels(admitted)
        assert {o.key[3] for o in admitted.rotation.victims}=={'old-A'}
        trial=rig.cycle(pool='trial');rig.finish_cancels(trial)
        assert rig.cid('A') not in trial.selection.targets
        assert rig.sdk.posts==[]  # ACK leaves the three actual BUY resources occupied.
        rig.release('old-A')
        held[0]=True
        rig.tick(11)
        rig.converge_current(lambda:any(p['token_id']=='tE' for p in rig.sdk.posts))
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts]==[('tE',Decimal('1'))]
        assert {'old-B','old-C'} <= {o['id'] for o in rig.sdk.orders}
        assert entered.wait(2),'income-book external boundary watchdog'
        held[0]=False;gate.set()
        rig.tick()
        rig.converge_current(lambda:any(p['token_id']=='tA' for p in rig.sdk.posts))
        assert [(p['token_id'],p['price']*p['size']) for p in rig.sdk.posts if p['token_id']=='tA']==[('tA',Decimal('50'))]
    finally:
        gate.set();rig.close()


def test_dashboard_affiliation_reads_do_not_grant_trading_authority(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tests.test_prediction_service import _lp436_accepted_rig
    rig, income_id, trial_id = _lp436_accepted_rig(tmp_path, monkeypatch)
    try:
        assert rig.store.lp_advance_trade_generation(rig.store.lp_trade_generation())
        rig.execution.refresh_lp_dashboard_snapshot()
        assert {r['order_id'] for r in rig.execution.lp_dashboard()['lp_orders_today']} == {income_id, trial_id}
        rig.sdk.get_balance_allowance = lambda **kw: SimpleNamespace(balance=None, allowances={})
        rig.tick(11)
        rig.converge_current(lambda: rig.execution.lp_auto_state()['observed_pools']['current_membership']['state'] == 'unknown')
        assert rig.store.lp_advance_trade_generation(rig.store.lp_trade_generation())
        rig.execution.refresh_lp_dashboard_snapshot()
        # Observe completed background display jobs before testing cached reads.
        for name in ('_lp_reward_refresh_thread', '_lp_orders_today_refresh_thread'):
            thread = getattr(rig.execution, name, None)
            if thread is not None:
                thread.join(timeout=3)
                assert not thread.is_alive(), name
        rig.pump(lambda: rig.live.read_runtime.status()['ready_count'] == 0
            and not any(s['active'] for s in rig.live.read_runtime.status()['scopes'])
            and rig.live.read_runtime.status()['observed_monotonic'] >= rig.seconds)
        before = rig.live.state.snapshot()
        assert before.new_capacity.state == 'unknown'
        roles_before = rig.store.lp_load_observed_pool_roles(rig.account)
        requests_before = tuple(rig.requests)
        posts_before, cancels_before = list(rig.sdk.posts), list(rig.sdk.cancels)
        generation_before = rig.store.lp_trade_generation()
        audits_before = {role['request_id']: rig.store.lp_actions('d2:'+role['request_id'])
            for role in roles_before['roles'].values()}
        for _ in range(3):
            dashboard = rig.execution.lp_dashboard()
            auto = rig.execution.lp_auto_state()
            assert auto['observed_pools']['current_membership']['state'] == 'unknown'
            rows = {r['order_id']: r for r in dashboard['lp_orders_today']}
            assert rows[income_id]['pool_affiliation']['pool'] == 'income'
            assert rows[trial_id]['pool_affiliation']['pool'] == 'trial'
        assert rig.live.state.snapshot() == before
        assert rig.store.lp_load_observed_pool_roles(rig.account) == roles_before
        assert tuple(rig.requests) == requests_before
        assert rig.sdk.posts == posts_before and rig.sdk.cancels == cancels_before
        assert rig.store.lp_trade_generation() == generation_before
        assert {rid: rig.store.lp_actions('d2:'+rid) for rid in audits_before} == audits_before
    finally:
        rig.close()
