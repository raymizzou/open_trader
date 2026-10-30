"""#463 public execution/HTTP contracts; external exchange stays offline."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import pytest


def test_all_holdings_summary_reconciles_coverage_and_reasons(tmp_path, monkeypatch):
    from tests.test_lp_observed_pools import PoolRig
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_market_reader import MarketReader
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from tests.test_lp_d2_execution_bridge import attach
    rig = PoolRig(tmp_path, monkeypatch, build_owner=False)
    rig.producer = PolymarketLPService(rig.store, rig.adapter)
    rig.live = LPD2LiveExecution(rig.store, rig.adapter, account_id=rig.adapter.config.wallet_address,
        venue='polymarket', process_epoch='holdings-monitor', policy=CalculationPolicy(None),
        reader=MarketReader(rig.adapter.lp_reader_source_port()))
    rig.live.initialize(ValidatedStartAdmission('holdings-monitor', 'offline-permit'))
    rig.execution = attach(rig.live, rig.producer, tmp_path)
    rig.live.run_once()
    try:
        for name in 'ABCDEG':
            rig.market(name)
        rig.add_order('A', '4')
        rig.sdk.orders[0]['side'] = 'SELL'
        rig.sdk.positions = tuple(dict(condition_id=rig.cid(n), token_id='t'+n,
            size=q, average_price=cost, current_value='0') for n, q, cost in [
                ('A', '10', '.3'), ('B', '2.074', '.3'), ('C', '.009', '.3'),
                ('D', '3', None), ('E', '4', '.3'), ('G', '1', '.995')])
        book = rig.public.get_order_book
        rig.public.get_order_book = lambda **kw: book(**kw).model_copy(update={
            'min_order_size': Decimal(5) if kw['token_id'] == 'tB' else Decimal(1)})
        market = rig.public.get_market
        def metadata(**kw):
            value = market(**kw)
            return value.model_copy(update={'state': value.state.model_copy(update={'closed': True, 'accepting_orders': False})}) if kw['id'] == 'mE' else value
        rig.public.get_market = metadata
        rig.live.run_once()
        before = (rig.sdk.balance_reads, rig.sdk.order_reads, rig.sdk.position_reads, rig.store.lp_trade_generation())
        result = rig.execution.lp_monitor_summary(now=rig.live._utc_now())['holdings']
        assert Decimal(result['position_quantity']) == Decimal('20.083')
        assert Decimal(result['covered_quantity']) == 10
        assert Decimal(result['uncovered_quantity']) == Decimal('10.083')
        assert result['reasoned_quantity'] == result['uncovered_quantity']
        rows = {r['token_id']: r for r in result['items']}
        assert {k: r['state'] for k, r in rows.items()} == dict(tA='covered', tB='below_minimum',
            tC='dust', tD='cost_unknown', tE='market_ended', tG='cost_above_maximum')
        assert all(r['reason'] and r['current_value'] == '0' for r in rows.values())
        assert rows['tD']['cost_floor'] is None and rows['tD']['cost_source'] == 'unknown'
        assert rows['tA']['sells'][0]['quantity'] == '10'
        assert rig.execution.lp_monitor_summary(now=rig.live._utc_now())['holdings'] == result
        assert before == (rig.sdk.balance_reads, rig.sdk.order_reads, rig.sdk.position_reads, rig.store.lp_trade_generation())
        assert rig.sdk.posts == rig.sdk.cancels == []
        rig.execution._lp_d2 = None
        missing = rig.execution.lp_monitor_summary(now=rig.live._utc_now())['holdings']
        assert missing['status'] == 'unknown' and missing['position_quantity'] is None
    finally:
        rig.close()
    from tests.test_lp_all_holdings_sell import mixed_holding, monitored
    from tests.test_lp_fill_then_hang import d2_tick
    live, sdk, public, clock, terminals = mixed_holding(tmp_path / 'mixed', monkeypatch)
    try:
        execution = monitored(live, tmp_path / 'mixed')
        for _ in range(6):
            d2_tick(live)
        before = (len(sdk.posts), len(sdk.cancels), sdk.order_reads, sdk.position_reads)
        summary = execution.lp_monitor_summary(now=live._utc_now())['holdings']
        row, = summary['items']
        assert Decimal(row['quantity']) == Decimal(row['covered_quantity']) == 110
        assert Decimal(row['uncovered_quantity']) == 0 and row['state'] == 'covered'
        assert row['cost_source'] == 'mixed_max' and Decimal(row['cost_floor']) == Decimal('.31')
        assert Decimal(summary['position_quantity']) == Decimal(summary['covered_quantity']) == 110
        assert Decimal(summary['reasoned_quantity']) == Decimal(summary['uncovered_quantity']) == 0
        assert before == (len(sdk.posts), len(sdk.cancels), sdk.order_reads, sdk.position_reads)
    finally:
        assert live.close(3)
        live.trading.close()


def test_monitor_orders_and_pools_share_one_snapshot(tmp_path, monkeypatch):
    from tests.test_lp_observed_pools import AsyncPoolRig
    rig = AsyncPoolRig(tmp_path, monkeypatch)
    try:
        for name in 'ABCDEFG':
            rig.market(name)
            rig.add_order(name, '1')
        rig.sdk.orders[-1]['side'] = 'SELL'
        rig.store.lp_save_observed_pool_roles(rig.account, expected_revision=0,
            roles={rig.cid(n): dict(pool='trial' if n in 'ABC' else 'income', episode='old-'+n)
                   for n in 'ABCDE'})
        rig.publish_account({'old-'+n for n in 'ABCDEF'})
        before = rig.store.lp_trade_generation()
        posts, cancels, requests = len(rig.sdk.posts), len(rig.sdk.cancels), len(rig.requests)
        result = rig.execution.lp_monitor_summary(now=rig.utc())
        assert result['orders']['counts'] == dict(trial=3, income=2, unassigned=1, sell=1)
        assert {r['order_id'] for r in result['orders']['items']} == {'old-'+n for n in 'ABCDEFG'}
        assert result['pools']['items']['income']['count'] == 2
        assert result['pools']['items']['trial']['target'] == 3
        assert result['orders']['status'] == 'attention'
        captured=rig.execution.lp_auto_state()
        for seconds, expected in [(299, 'attention'), (1, 'abnormal')]:
            rig.tick(seconds)
            if seconds == 299:
                rig.pump(lambda: rig.execution.lp_auto_state()['observed_pools']['current_membership']['observed_at'] == rig.utc().isoformat(),
                         drive=lambda: rig.live.run_once(pool='trial'))
            # +1s is still a fresh confirmed observation; do not demand a read
            # before the real five-second financial cache deadline.
            assert rig.execution.lp_monitor_summary(now=rig.utc())['orders']['status'] == expected
        assert len(rig.sdk.posts) == posts and len(rig.sdk.cancels) == cancels
        assert rig.store.lp_trade_generation() == before  # monitor GET adds no trade writes
        rig.sdk.get_balance_allowance = lambda **kw: (_ for _ in ()).throw(OSError('offline'))
        rig.tick(11)
        rig.converge_current(lambda: rig.execution.lp_auto_state()['observed_pools']['current_membership']['state'] == 'unknown')
        historical=rig.execution.lp_monitor_summary(now=rig.utc())['orders']
        assert historical['counts']==dict(trial=3,income=2,unassigned=1,sell=1)
        assert historical['status']=='unknown' and historical['counts_basis']=='last_confirmed'
        from copy import deepcopy
        from open_trader.polymarket_lp_monitor import LPMonitorSummary
        monitor=LPMonitorSummary();origin=datetime.fromisoformat(captured['observed_pools']['current_membership']['observed_at'])
        def observe(seconds,*,full=False,missing=False,targets=None):
            at=origin+timedelta(seconds=seconds);auto=deepcopy(captured)
            member=auto['observed_pools']['current_membership'];member['observed_at']=at.isoformat()
            if full:
                for row in member['orders']:
                    if row['order_id']=='old-F':row['pool']='income'
            if missing:member['state']='unknown'
            if targets is not None:auto['effective_settings'].update(targets)
            return monitor.summarize(auto=auto,dashboard={},trades={},audits=[],account_id=rig.live.account_id,now=at)['orders']
        for seconds,expected,options in [(0,'attention',{}),(299,'attention',{}),(300,'abnormal',{}),
                (301,'normal',{'full':True}),(400,'attention',{}),(699,'attention',{}),(700,'abnormal',{}),
                (701,'unknown',{'missing':True}),(1000,'abnormal',{}),(1299,'abnormal',{}),(1300,'abnormal',{})]:
            assert observe(seconds,**options)['status']==expected
        assert observe(1301,targets={'trial_target_buy_count':3,'income_target_buy_count':2})['status']=='normal'
    finally:
        rig.close()


def test_monitor_missing_sources_remain_unknown(tmp_path, monkeypatch):
    from copy import deepcopy
    from tests.test_lp_observed_pools import AsyncPoolRig
    rig = AsyncPoolRig(tmp_path, monkeypatch)
    try:
        now = rig.utc()
        saved_store = rig.execution._store
        rig.execution._store = None
        empty = rig.execution.lp_monitor_summary(now=now)
        assert empty['balance']['capacity_usd'] is None
        assert empty['balance']['status'] == 'unknown'
        assert empty['fills']['hour']['count'] is None
        assert empty['cancellations']['hour']['success'] is None
        assert rig.execution.lp_monitor_summary(now=now+timedelta(seconds=299))['balance']['status'] == 'unknown'
        assert rig.execution.lp_monitor_summary(now=now+timedelta(seconds=300))['balance']['status'] == 'abnormal'
        rig.execution._store = saved_store
        # Recover from complete current sources. An explicit observed zero is known.
        good = dict(account_id=rig.live.account_id, authenticated=True, checked_at=now,
                    balance='0', allowance='0', positions_complete=True, open_orders_complete=True,
                    state='ready', stale=False)
        rig.execution._lp_dashboard_cache = good
        recovered = rig.execution.lp_monitor_summary(now=now)
        assert recovered['balance']['status'] == 'normal'
        assert recovered['balance']['balance_usd'] == '0'
        assert recovered['balance']['capacity_usd'] is not None
        for patch in [dict(authenticated=False), dict(account_id='wrong'), dict(balance='NaN'),
                      dict(positions_complete=False), dict(checked_at=None), dict(stale=True)]:
            rig.execution._lp_dashboard_cache = {**deepcopy(good), **patch}
            value = rig.execution.lp_monitor_summary(now=now)
            assert value['balance']['status'] == 'unknown', patch
            assert value['balance']['capacity_usd'] is None
        rig.execution._lp_dashboard_cache = {**good, 'checked_at': now-timedelta(seconds=301)}
        stale = rig.execution.lp_monitor_summary(now=now)
        assert stale['balance']['status'] == 'attention'
        assert stale['balance']['balance_usd'] == '0'
        rig.execution._lp_dashboard_cache = ['malformed']
        assert rig.execution.lp_monitor_summary(now=now)['balance']['capacity_usd'] is None
        assert rig.sdk.posts == rig.sdk.cancels == []
        from open_trader.polymarket_lp_monitor import LPMonitorSummary
        valid_auto=rig.execution.lp_auto_state()
        for malformed in [None,[],{'observed_pools':[]},
                {**valid_auto,'observed_pools':{'current_membership':{'state':'known',
                    'account_id':rig.live.account_id,'observed_at':now,'orders':[None]}}}]:
            result=LPMonitorSummary().summarize(auto=malformed,dashboard={},trades=[],audits=None,
                account_id=rig.live.account_id,now=now)
            assert result['orders']['counts'] is None
            assert result['balance']['capacity_usd'] is None
        audit=dict(account_id=rig.live.account_id,process_epoch='old',request_id='r',session_id='d2:r',
            condition_id='c',token_id='t',order_id='o',kind='CANCEL',requested_at=now)
        for malformed in [None,[None],[{**audit,'receipt':[]}],[{**audit,'progress':'broken'}]]:
            result=LPMonitorSummary().summarize(auto=valid_auto,dashboard={},trades={},audits=malformed,
                account_id=rig.live.account_id,now=now)
            assert result['cancellations']['status']=='unknown'
            assert result['cancellations']['hour']['success'] is None
    finally:
        rig.close()


def test_monitor_cancel_history_survives_restart(tmp_path, monkeypatch):
    from tests.test_lp_observed_pools import AsyncPoolRig
    from tests.test_lp_d2_execution_bridge import attach
    from open_trader.lp_business_writer import RequestRecord, ReceiptRecord, ProgressRecord
    rig = AsyncPoolRig(tmp_path, monkeypatch)
    try:
        for n in 'AB':
            rig.market(n)
            rig.add_order(n, '1')
        rig.publish_account({'old-A', 'old-B'})
        now = rig.utc()
        def audit(key, order, outcome, age, *, account=None, final=False):
            account = account or rig.live.account_id
            at = now-timedelta(seconds=age)
            assert rig.store.lp_log_business_record(RequestRecord(account, 'prior-process', key, 'd2:'+key,
                rig.cid('A'), 'tA', 'CANCEL', Decimal('.4'), Decimal('2.5'), at, order_id=order))
            if outcome:
                assert rig.store.lp_log_business_record(ReceiptRecord(account, 'prior-process', key,
                    'd2:'+key, rig.cid('A'), 'tA', order, outcome, at))
            if final:
                assert rig.store.lp_log_business_record(ProgressRecord(account, 'prior-process', key,
                    'd2:'+key, rig.cid('A'), 'tA', order, Decimal('2.5'), Decimal('0'), Decimal('.4'), 'CANCELED', at))
        audit('success', 'gone-success', 'accepted', 600, final=True)
        audit('rejected', 'gone-rejected', 'rejected', 500)
        audit('unknown', 'gone-unknown', 'unknown', 400)
        audit('blocked', 'gone-blocked', 'not_sent', 300)
        audit('outside', 'gone-old', 'rejected', 90000)
        audit('foreign', 'foreign', 'unknown', 60, account='different-account')
        audit('pending', 'old-A', 'accepted', 121)
        audit('recent', 'old-B', None, 119)
        value = rig.execution.lp_monitor_summary(now=now)['cancellations']
        assert value['hour'] == dict(success=1, rejected=1, unknown=1, blocked=1)
        assert value['today'] == dict(success=1, rejected=1, unknown=1, blocked=1)
        assert value['status'] == 'abnormal'
        assert {r['order_id'] for r in value['stuck']} == {'old-A'}
        assert {r['order_id'] for r in value['items'] if r['outcome']=='unknown'} == {'gone-unknown'}
        assert {r['order_id'] for r in rig.execution.lp_monitor_summary(now=now)['orders']['items']} == {'old-A','old-B'}
        from open_trader.prediction_arbitrage_execution import PredictionExecutionService
        restarted = PredictionExecutionService(store=rig.store, trading=rig.adapter, monitor=object(),
            notifier=object(), lock_path=tmp_path/'fresh-execution.lock')
        assert restarted.lp_monitor_summary(now=now)['cancellations']['hour'] == dict(success=1, rejected=1, unknown=1, blocked=1)
        with pytest.raises(ValueError):rig.store.lp_monitor_cancel_audits(rig.live.account_id,instance_id='other-instance')
        assert all(r['order_id'] != 'foreign' for r in value['items'])
        assert rig.sdk.cancels == []
        with pytest.raises(ValueError):
            rig.live.cancel_independent(('foreign-account','not-a-market','not-a-token','not-an-order'))
        uncovered=rig.execution.lp_monitor_summary(now=now)['cancellations']
        assert uncovered['hour']['blocked'] is None
        assert uncovered['boot_reference']['blocked']>=1
        assert '未知' in uncovered['coverage']
        assert rig.sdk.cancels == []
    finally:
        rig.close()


@pytest.mark.parametrize('reverse', [False, True], ids=['mixed-to-lower', 'lower-to-mixed'])
def test_monitor_cancel_audits_preserve_wallet_identity_across_case_restart(tmp_path, reverse):
    from copy import deepcopy
    from open_trader.lp_business_writer import RequestRecord, ReceiptRecord
    from open_trader.polymarket_lp_monitor import LPMonitorSummary
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

    mixed = '0x' + 'aB12' * 10
    original, restarted_account = (mixed.lower(), mixed) if reverse else (mixed, mixed.lower())
    foreign = '0x' + 'cd34' * 10
    now = datetime(2026, 10, 26, 12, tzinfo=UTC)
    at = now - timedelta(minutes=5)
    store = PredictionArbitrageStore(tmp_path)
    store.lp_bind_instance(original, 'polymarket', 'tokyo-test')
    for account, key in [(original, 'ours'), (foreign, 'foreign')]:
        assert store.lp_log_business_record(RequestRecord(account, 'old-process', key, 'd2:' + key,
            'condition-A', 'token-A', 'CANCEL', Decimal('.4'), Decimal('2.5'), at,
            order_id='order-' + key))
        assert store.lp_log_business_record(ReceiptRecord(account, 'old-process', key, 'd2:' + key,
            'condition-A', 'token-A', 'order-' + key, 'unknown', at))
    # Store connections close after each action. Reopen the same durable DB,
    # without an execution owner or any trading/network collaborator.
    del store
    restarted = PredictionArbitrageStore(tmp_path, initialize=False)
    restarted.lp_bind_instance(restarted_account, 'polymarket', 'tokyo-test')
    with restarted._read_connection() as connection:
        before = list(connection.iterdump())

    def summary(rows, account=restarted_account):
        return LPMonitorSummary().summarize(auto={}, dashboard={}, trades={}, audits=rows,
            account_id=account, now=now)['cancellations']

    audits = restarted.lp_monitor_cancel_audits(restarted_account, instance_id='tokyo-test')
    assert [row['request_id'] for row in audits] == ['ours']
    assert audits[0]['account_id'] == audits[0]['receipt']['account_id'] == original
    assert audits[0]['order_id'] == 'order-ours'
    value = summary(audits)
    assert value['hour'] == value['today'] == dict(success=0, rejected=0, unknown=1, blocked=0)
    assert value['status'] == 'abnormal'
    assert [(row['request_id'], row['order_id'], row['outcome']) for row in value['items']] == [
        ('ours', 'order-ours', 'unknown')]
    with pytest.raises(ValueError, match='monitor_instance_identity_mismatch'):
        restarted.lp_monitor_cancel_audits(restarted_account, instance_id='other-instance')
    with pytest.raises(ValueError, match='monitor_instance_identity_mismatch'):
        restarted.lp_monitor_cancel_audits(foreign, instance_id='tokyo-test')

    # Equivalent wallet spelling changes attribution only. Every evidence
    # identity remains exact; mismatches must never count as successful/zero.
    for evidence in ('receipt', 'progress'):
        for key in ('account_id', 'process_epoch', 'request_id', 'session_id',
                    'condition_id', 'token_id', 'order_id'):
            broken = deepcopy(audits)
            if evidence == 'progress':
                broken[0]['progress'] = {k: audits[0][k] for k in (
                    'account_id', 'process_epoch', 'request_id', 'session_id',
                    'condition_id', 'token_id', 'order_id')}
                broken[0]['progress']['status'] = 'CANCELED'
            broken[0][evidence][key] = 'different-' + key
            result = summary(broken)
            assert result['status'] == 'unknown', (evidence, key)
            assert all(count is None for count in result['hour'].values())
    for bad_account in (None, '', [], 123):
        assert summary([], bad_account)['status'] == 'unknown'
        broken = deepcopy(audits)
        broken[0]['account_id'] = bad_account
        assert summary(broken)['status'] == 'unknown'
    zero = summary([])
    assert zero['status'] == 'normal'
    assert zero['hour'] == dict(success=0, rejected=0, unknown=0, blocked=0)
    with restarted._read_connection() as connection:
        assert list(connection.iterdump()) == before  # no historical rewrite/replay


def test_monitor_pool_runtime_and_candidate_thresholds(tmp_path, monkeypatch):
    from copy import deepcopy
    from tests.test_lp_observed_pools import AsyncPoolRig
    from open_trader.polymarket_lp_monitor import LPMonitorSummary
    rig = AsyncPoolRig(tmp_path, monkeypatch)
    try:
        for n in 'ABCDEF': rig.market(n); rig.add_order(n,'1')
        rig.store.lp_save_observed_pool_roles(rig.account, expected_revision=0,
            roles={rig.cid(n): dict(pool='trial' if n in 'ABC' else 'income',episode='old-'+n) for n in 'ABCDEF'})
        rig.publish_account({'old-'+n for n in 'ABCDEF'})
        now = rig.utc()
        auto = rig.execution.lp_auto_state()
        dashboard = dict(account_id=rig.live.account_id, candidate_valid_count=2, candidate_state='ready',
            candidate_stale=False, candidate_last_success_at=now, candidate_checked_at=now)
        # Detached public summarizer inputs represent independent source facts;
        # no replacement of execution internals or its collaborators.
        def summarize(auto_value, dashboard_value, at=now, monitor=None):
            return (monitor or LPMonitorSummary()).summarize(auto=auto_value,
                dashboard=dashboard_value, trades={}, audits=[], account_id=rig.live.account_id, now=at)['pools']
        good = summarize(auto,dashboard)
        assert good['status']=='normal'
        assert good['items']['trial']['budget_usd']=='20'
        assert Decimal(good['items']['trial']['used_usd'])==3
        assert good['items']['trial']['last_change_at'] is None  # no historical change fact fabricated at GET time
        assert '未保存' in good['items']['trial']['last_change_reason']
        for patch, reason in [(dict(desired_running=True,effective_running=False),'想开'),
                              (dict(fatal={'origin':'startup_read'}),'致命')]:
            value=summarize({**auto,**patch},dashboard)
            assert value['status']=='abnormal' and reason in value['reason']
        assert summarize(auto,{**dashboard,'candidate_valid_count':0})['status']=='abnormal'
        assert '想开' not in summarize({**auto,'desired_running':False,'effective_running':False},dashboard)['reason']
        monitor=LPMonitorSummary()
        failed={**dashboard,'candidate_stale':True,'candidate_state':'stale'}
        for seconds,expected in [(0,'attention'),(299,'attention'),(599,'attention'),(600,'abnormal')]:
            at=now+timedelta(seconds=seconds)
            fresh=deepcopy(auto)
            fresh['observed_pools']['current_membership']['observed_at']=at.isoformat()
            value=summarize(fresh,failed,at,monitor)
            assert value['status']==expected
            assert value['candidate_last_success_at']==now.isoformat()
        assert summarize(auto,{**dashboard,'candidate_last_success_at':None})['status']=='unknown'
        unavailable=deepcopy(auto);unavailable['effective_settings']['trial_budget_usd']=None
        assert summarize(unavailable,dashboard)['items']['trial']['budget_usd'] is None
    finally: rig.close()


def test_monitor_fills_use_complete_own_account_facts(tmp_path, monkeypatch):
    from copy import deepcopy
    from tests.test_lp_observed_pools import AsyncPoolRig
    from open_trader.polymarket_lp_monitor import LPMonitorSummary
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        now=datetime(2026,10,25,15,5,tzinfo=UTC)  # Tokyo 00:05
        auto=rig.execution.lp_auto_state()
        auto['observed_pools']['current_membership']['observed_at']=now.isoformat()
        def fill(tid,order,side,price,size,at):
            return dict(trade_id=tid,condition_id='market-A',token_id='yes',side=side,price=price,size=size,
                status='CONFIRMED',trader_side='TAKER',taker_order_id=order,matched_at=at,maker_orders=[])
        rows=[fill('t1','buy1','BUY','.35','40',now-timedelta(minutes=2)),
              fill('t2','sell1','SELL','.40','10',now-timedelta(minutes=1))]
        cache=dict(account_id=rig.live.account_id,state='ready',complete=True,stale=False,
                   checked_at=now,total=2,items=rows)
        def summary(source):
            return LPMonitorSummary().summarize(auto=auto,dashboard={},trades=source,audits=[],
                account_id=rig.live.account_id,now=now)['fills']
        result=summary(cache)
        assert result['hour']==dict(count=2,amount_usd='18.00')
        assert result['today']==dict(count=2,amount_usd='18.00')
        assert result['status']=='attention'
        assert result['inventory_cost_usd']=='0'
        assert {r['side'] for r in result['items']}=={'BUY','SELL'}
        assert all(r['pool'] is None for r in result['items'])
        dup=deepcopy(rows[0]); other=deepcopy(rows[0]);other['taker_order_id']='buy2'
        extended=summary({**cache,'items':rows+[dup,other],'total':4})
        assert extended['hour']==dict(count=3,amount_usd='32.00')
        midnight=now.replace(hour=15,minute=0,second=0,microsecond=0)
        boundary=[fill('before','b','BUY','.5','2',midnight-timedelta(microseconds=1)),
                  fill('at','a','BUY','.5','2',midnight)]
        value=summary({**cache,'items':boundary,'total':2})
        assert value['today']==dict(count=1,amount_usd='1.0')
        assert value['hour']==dict(count=2,amount_usd='2.0')
        maker=dict(trade_id='maker',condition_id='market-A',token_id='counterparty',side='SELL',price='.9',size='999',
            status='CONFIRMED',trader_side='MAKER',taker_order_id='not-ours',matched_at=now,
            maker_orders=[dict(order_id='ours-1',token_id='yes',side='BUY',price='.35',matched_amount='40'),
                          dict(order_id='ours-2',token_id='yes',side='BUY',price='.4',matched_amount='10')])
        assert summary({**cache,'items':[maker,maker],'total':2})['hour']==dict(count=2,amount_usd='18.00')
        for patch in [dict(complete=False),dict(account_id='foreign'),dict(total=3),dict(stale=True),
                      dict(items=[{**rows[0],'trade_id':None}],total=1),dict(items=[{**rows[0],'trader_side':None}],total=1),
                      dict(items=None,total=0),dict(items=[None],total=1)]:
            value=summary({**cache,**patch})
            assert value['status']=='unknown' and value['hour']['count'] is None
        assert summary({**cache,'items':[{**r,'status':'FAILED'} for r in rows]})['hour']['count']==0
        unknown_auto=deepcopy(auto)
        unknown_auto['observed_pools']['current_membership'].update(state='unknown',orders=[dict(
            condition_id='market-A',token_id='yes',order_id='buy1',side='BUY',pool='income')])
        uncertain=LPMonitorSummary().summarize(auto=unknown_auto,dashboard={},trades=cache,audits=[],
            account_id=rig.live.account_id,now=now)['fills']
        assert uncertain['items'][0]['pool'] is None
    finally:rig.close()


def test_monitor_http_is_readonly_and_keeps_unknowns(tmp_path, monkeypatch):
    import json
    from urllib.request import Request
    from tests.test_prediction_service import _Runtime, _server, _response
    from tests.test_lp_observed_pools import AsyncPoolRig
    rig=AsyncPoolRig(tmp_path,monkeypatch)
    try:
        runtime=_Runtime();runtime.mode='production';runtime.production_owner=True
        runtime.store=rig.store;runtime.execution=rig.execution
        before=rig.store.lp_trade_generation()
        calls=len(rig.requests);posts=len(rig.sdk.posts);cancels=len(rig.sdk.cancels)
        with rig.store._read_connection() as connection:database_before=list(connection.iterdump())
        with _server(runtime) as base:
            code,body=_response(base+'/api/prediction-arbitrage/lp/monitor')
            assert code==200
            for key in ('orders','cancellations','pools','balance','fills'):
                assert body[key]['status'] in ('normal','attention','abnormal','unknown')
                assert body[key]['reason']
            assert body['balance']['capacity_usd'] is None
            assert 'private_key' not in json.dumps(body) and 'wallet_address' not in json.dumps(body)
            code,_=_response(Request(base+'/api/prediction-arbitrage/lp/monitor',data=b'{}',method='POST',headers={'Content-Type':'application/json'}))
            assert code in (403,404,405)
        assert rig.store.lp_trade_generation()==before
        with rig.store._read_connection() as connection:assert list(connection.iterdump())==database_before
        assert len(rig.requests)==calls and len(rig.sdk.posts)==posts and len(rig.sdk.cancels)==cancels
        runtime.mode='shadow';runtime.production_owner=False
        with _server(runtime) as base:
            code,body=_response(base+'/api/prediction-arbitrage/lp/monitor')
            assert code==200 and all(body[k]['status']=='unknown' for k in ('orders','cancellations','pools','balance','fills'))
    finally:rig.close()


@pytest.mark.parametrize('case', ['below_cost_cancel_unknown', 'current_reason_after_denial'])
def test_holdings_monitor_reports_current_blockers_and_below_cost_orders(tmp_path, monkeypatch, case):
    from tests.test_lp_all_holdings_sell import imported_holding, monitored
    from tests.test_lp_fill_then_hang import d2_tick
    from tests.test_lp_order_registration_contract import _open_order, TOKEN_ID
    from open_trader import lp_monitor_readonly as readonly
    live, sdk, public, clock = imported_holding(tmp_path, monkeypatch, ask='.18')
    sdk.positions = tuple(dict(p, average_price='.19') for p in sdk.positions)
    execution = monitored(live, tmp_path)
    try:
        if case == 'below_cost_cancel_unknown':
            sdk.orders = (_open_order('monitor-below-cost', 'SELL', price='.18', original='10'),)
            def cancel(*, order_ids):
                sdk.cancels.append(order_ids)
                raise TimeoutError('offline exact cancellation outcome unknown')
            sdk.cancel_orders = cancel
            d2_tick(live)
            summary = execution.lp_monitor_summary(now=live._utc_now())
            section = summary['holdings']; row, = section['items']
            assert row['covered_quantity'] == '10' and row['uncovered_quantity'] == '0'
            assert row['state'] == 'below_cost_sell' and '低于成本' in row['reason'] and '撤单' in row['reason']
            assert section['status'] == 'abnormal' and '低于成本' in section['reason']
            page = readonly.render_page(summary)
            assert '低于成本' in page and '撤单最终结果未确认' in page
            assert 'holding-row abnormal' in page
            assert sdk.cancels == [('monitor-below-cost',)] and sdk.posts == sdk.market_orders == []
        else:
            sign = sdk.create_limit_order
            def denied_sign(**kwargs):
                signed = sign(**kwargs)
                live.run_once()  # an actual newer account observation revokes this original quote
                return signed
            sdk.create_limit_order = denied_sign
            d2_tick(live)
            row, = live.store.lp_holding_sell_orders(live.account_id, 'polymarket')
            assert row['state'] == 'rejected' and row['reason'] == 'admission_changed'
            market, book = public.get_market, public.get_order_book
            ended = [True]
            def metadata(**kwargs):
                value = market(**kwargs)
                return value.model_copy(update={'state': value.state.model_copy(update={
                    'closed': True, 'accepting_orders': False})}) if ended[0] else value
            public.get_market = metadata
            public.get_order_book = lambda **kwargs: book(**kwargs).model_copy(update={'min_order_size': Decimal(5)})
            sdk.positions = tuple(dict(p, size='2') for p in sdk.positions)
            for is_ended, state, reason in [(True, 'market_ended', '市场已结束'), (False, 'below_minimum', '低于最小下单量')]:
                ended[0] = is_ended
                clock[0] += 43201  # expire the real 12-hour metadata cache before observing the changed market
                d2_tick(live)
                summary = execution.lp_monitor_summary(now=live._utc_now())
                row, = summary['holdings']['items']
                assert row['token_id'] == TOKEN_ID and row['state'] == state and reason in row['reason']
                page = readonly.render_page(summary)
                assert reason in page and '准入时账户观察已改变' not in page
            assert sdk.posts == sdk.cancels == sdk.market_orders == []
    finally:
        assert live.close(3)
        live.trading.close()


def test_monitor_explains_read_cleanup_fault_and_recovery():
    from copy import deepcopy
    from open_trader.polymarket_lp_monitor import LPMonitorSummary
    now = datetime(2026, 10, 27, 3, tzinfo=UTC)
    auto = dict(desired_running=True, effective_running=True, fatal=None,
        effective_settings=dict(trial_target_buy_count=1, income_target_buy_count=0),
        observed_pools=dict(current_membership=dict(state='known', account_id='ours',
            observed_at=now.isoformat(), orders=[])))
    dashboard = dict(account_id='ours', candidate_valid_count=3, candidate_state='ready',
        candidate_stale=False, candidate_last_success_at=now.isoformat())
    item = dict(lane='low', kind='position', since=(now-timedelta(minutes=8)).isoformat(),
        active=True, blocks_new_buy=True, reason='delayed_read_cleanup')
    runtime = dict(cleanup_faults=1, new_buy_allowed=False, new_buy_block_reasons=['cleanup_fault'],
        cleanup_fault_details=dict(total=1, omitted=0, items=[item]))
    monitor = LPMonitorSummary()
    def summary(a=auto, d=dashboard):
        return monitor.summarize(auto=a, dashboard=d, trades={}, audits=[], account_id='ours', now=now)
    healthy = summary()
    blocked_auto = {**auto, 'diagnostics': {'async_reads': runtime}}
    blocked = summary(blocked_auto)
    assert blocked['pools']['status'] == 'abnormal'
    assert blocked['pools']['reason'] == '读取清理故障，暂停新增 BUY'
    assert blocked['pools']['read_health'] == dict(status='abnormal', reason='读取清理故障，暂停新增 BUY',
        cleanup_fault_details=dict(total=1, omitted=0, items=[item]), new_buy_block_reasons=['cleanup_fault'])
    assert blocked['orders'] == healthy['orders'] and blocked['balance'] == healthy['balance']
    retained = deepcopy(blocked)
    runtime['cleanup_faults'] = 0
    runtime['new_buy_allowed'] = True
    runtime['new_buy_block_reasons'] = []
    runtime['cleanup_fault_details'] = dict(total=0, omitted=0, items=[])
    recovered = summary(blocked_auto)
    assert recovered['pools']['status'] == healthy['pools']['status']
    assert recovered['pools']['reason'] == healthy['pools']['reason']
    assert recovered['pools']['read_health']['status'] == 'normal'
    assert blocked == retained  # no references to subsequently changed input
    assert summary()['pools']['read_health']['status'] == 'unknown'
    good = dict(cleanup_faults=1, new_buy_allowed=False, new_buy_block_reasons=['cleanup_fault'],
        cleanup_fault_details=dict(total=1, omitted=0, items=[item]))
    for patch in [None, [], {'total': 1, 'items': []},
            dict(total=1, omitted=0, items=[{**item, 'since': None}]),
            dict(total=1, omitted=0, items=[{**item, 'since': '2026-10-27T03:00:00'}]),
            dict(total=1, omitted=0, items=[{**item, 'lane': 'secret-url'}]),
            dict(total=1, omitted=0, items=[{**item, 'blocks_new_buy': 'yes'}]),
            dict(total=17, omitted=0, items=[item]*17)]:
        value = summary({**auto, 'diagnostics': {'async_reads': {**good, 'cleanup_fault_details': patch}}})
        assert value['pools']['read_health']['status'] == 'unknown'
        assert value['pools']['read_health']['cleanup_fault_details'] is None
        assert value['pools']['reason'] == healthy['pools']['reason']
    for patch, reason in [(dict(fatal={'origin': 'startup'}), '致命'),
            (dict(desired_running=True, effective_running=False), '想开')]:
        value = summary({**auto, **patch, 'diagnostics': {'async_reads': good}})
        assert reason in value['pools']['reason']
    stopped = summary({**auto, 'desired_running': False, 'effective_running': False,
        'diagnostics': {'async_reads': good}})
    assert '读取清理' not in stopped['pools']['reason']
    foreign = deepcopy(auto)
    foreign['observed_pools']['current_membership']['account_id'] = 'foreign'
    foreign['diagnostics'] = {'async_reads': good}
    assert summary(foreign)['pools']['status'] == 'unknown'
    failed_candidates = {**dashboard, 'candidate_valid_count': 0}
    assert summary(blocked_auto, failed_candidates)['pools']['reason'] == '有效候选为零'


def test_financial_timeout_streak_explains_orders_and_pools_and_resets():
    from copy import deepcopy
    import hashlib
    from open_trader.polymarket_lp_diagnostics import diagnostic_json_copy
    from open_trader.polymarket_lp_monitor import LPMonitorSummary
    from open_trader.polymarket_lp_read_runtime import LPReadRuntime
    from test_lp_timeout_pool_rotation import PoolBoundary, advance
    from test_lp_async_read_runtime_contract import Clock, collect, demand
    clock = Clock()
    boundary = PoolBoundary(clock)
    runtime = LPReadRuntime(boundary.factory, clock=clock, utc_now=clock.utc)
    runtime.start()
    monitor = LPMonitorSummary()
    request = demand('O', kind='financial', sources=('balance','open_orders','positions'))
    auto = dict(desired_running=True, effective_running=True, fatal=None,
        effective_settings=dict(trial_target_buy_count=3, income_target_buy_count=3),
        observed_pools=dict(current_membership=dict(state='unknown', account_id='account',
            observed_at=clock.utc().isoformat(), orders=[])))
    def summarize(a):
        return monitor.summarize(auto=a, dashboard={}, trades={}, audits=[], account_id='account', now=clock.utc())
    def captured():
        a = deepcopy(auto)
        reads = diagnostic_json_copy(runtime.status())
        streak = reads['financial_timeout_streak']
        # The live owner publishes its expected identity independently of the
        # runtime snapshot. Bind this public status to our current request.
        reads['financial_identity'] = dict(identity=streak['identity'],
            account_fingerprint=hashlib.sha256(b'account').hexdigest())
        a['diagnostics'] = dict(async_reads=reads)
        return a
    try:
        runtime.reconcile((request,))
        boundary.wait(lambda: bool(boundary.transports.get('financial'))
            and boundary.transports['financial'][0]['clob'].leased == 2)
        advance(runtime, boundary, clock, 5)
        boundary.wait(lambda: all(not s['active'] for s in runtime.status()['scopes'])
            and all(t.closed.is_set() for t in boundary.transports['financial'][0].values()))
        old_snapshot = captured()
        for stamp, minutes in [(65, None), (65.001, 1), (130, 2)]:
            advance(runtime, boundary, clock, stamp)
            a = captured()
            summary = summarize(a)
            for section in ('orders','pools'):
                if minutes is None:
                    assert '账户挂单/余额读取卡住' not in summary[section]['reason']
                else:
                    assert summary[section]['status'] == 'abnormal'
                    assert summary[section]['reason'] == f'账户挂单/余额读取卡住 {minutes} 分钟'
            assert summary['orders']['counts'] is None
            assert summary['balance']['balance_usd'] is None
            assert all(p['count'] is None for p in summary['pools']['items'].values())
        assert '账户挂单/余额读取卡住' not in summarize(old_snapshot)['orders']['reason']
        good = captured()
        for changes, expected in [(dict(fatal={'origin':'startup'}),'致命'),
                (dict(desired_running=True,effective_running=False),'想开')]:
            special = summarize({**good, **changes})
            assert expected in special['pools']['reason']
            assert '账户挂单/余额读取卡住' not in special['orders']['reason']
        stopped = summarize({**good, 'desired_running':False, 'effective_running':False})
        assert all('账户挂单/余额读取卡住' not in stopped[k]['reason'] for k in ('orders','pools'))
        for patch in (None, {}, {'state':'timed_out','elapsed_seconds':125},
                {**good['diagnostics']['async_reads']['financial_timeout_streak'], 'identity':'foreign'},
                {**good['diagnostics']['async_reads']['financial_timeout_streak'], 'account_fingerprint':'foreign'},
                {**good['diagnostics']['async_reads']['financial_timeout_streak'], 'elapsed_seconds':float('nan')},
                {**good['diagnostics']['async_reads']['financial_timeout_streak'], 'observed_at':old_snapshot['diagnostics']['async_reads']['financial_timeout_streak']['observed_at']}):
            invalid = deepcopy(good)
            invalid['diagnostics']['async_reads']['financial_timeout_streak'] = patch
            unknown = summarize(invalid)
            assert all(unknown[k]['status'] == 'unknown' for k in ('orders','pools'))
            assert unknown['orders']['counts'] is None and unknown['balance']['balance_usd'] is None
        # A complete current attempt clears the outage even while the historical
        # last timeout diagnostic remains available. GET itself starts no reads.
        collect(runtime, boundary, lambda rows: any(r.complete for r in rows))
        fresh = captured()
        assert fresh['diagnostics']['async_reads']['last_financial_timeout'] is not None
        before = list(boundary.events)
        recovered = summarize(fresh)
        assert all('账户挂单/余额读取卡住' not in recovered[k]['reason'] for k in ('orders','pools'))
        assert before == boundary.events
    finally:
        runtime.reconcile(())
        assert runtime.close(4)
