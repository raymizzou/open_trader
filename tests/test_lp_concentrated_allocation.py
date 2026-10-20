"""C1 settings and observational allocation contracts; no live service access."""
from decimal import Decimal
import hashlib

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

ACCOUNT = hashlib.sha256(b'c1-offline-account').hexdigest()
WEIGHTS = ['0.5', '0.3', '0.2']


def test_settings_validate_weights_and_default_minimum(tmp_path):
    store = PredictionArbitrageStore(tmp_path)
    old = store.lp_load_current_settings(ACCOUNT)
    assert old.get('allocation_mode', 'minimum') == 'minimum'
    assert old.get('allocation_shadow_enabled', False) is False
    saved = store.lp_save_current_settings(ACCOUNT, expected_config_version=0,
        changes=dict(target_buy_count=3, allocation_weights=WEIGHTS,
            allocation_mode='minimum', allocation_shadow_enabled=True))
    assert saved['allocation_weights'] == WEIGHTS
    assert saved['allocation_mode'] == 'minimum'
    assert saved['allocation_shadow_enabled'] is True
    assert saved['config_version'] == 1
    assert store.lp_load_current_settings(ACCOUNT) == saved
    with pytest.raises(ValueError, match='config_version_changed'):
        store.lp_save_current_settings(ACCOUNT, expected_config_version=0,
            changes=dict(allocation_shadow_enabled=False))
    assert store.lp_load_current_settings(ACCOUNT) == saved


@pytest.mark.parametrize('weights', [
    ['NaN', '.3', '.2'], ['Infinity', '.3', '.2'], ['-Infinity', '.3', '.2'],
    [True, '.3', '.2'], [Decimal('.5'), '.3', '.2'], [.5, '.3', '.2'],
    ['-.5', '.3', '1.2'], ['0', '.6', '.4'], ['.4', '.5', '.1'],
    ['.5', '.3', '.1'], ['.5', '.3', '.20000000000000000000000000001'],
    ['.5', '.5', '1e-999999999'], ['bad', '.3', '.2'], ['.5', '.5'],
    '.5,.3,.2', None,
])
def test_settings_reject_invalid_weights_atomically(tmp_path, weights):
    store = PredictionArbitrageStore(tmp_path)
    before = store.lp_load_current_settings(ACCOUNT)
    with pytest.raises(ValueError, match='allocation_weights'):
        store.lp_save_current_settings(ACCOUNT, expected_config_version=0,
            changes=dict(target_buy_count=3, allocation_weights=weights, allocation_shadow_enabled=True))
    assert store.lp_load_current_settings(ACCOUNT) == before


@pytest.mark.parametrize('changes,reason', [
    (dict(allocation_mode='rank_weights'), 'rank_weights_requires_c2'),
    (dict(allocation_mode='other'), 'allocation_mode_invalid'),
    (dict(allocation_shadow_enabled=1), 'allocation_shadow_enabled_invalid'),
    (dict(allocation_shadow_enabled='true'), 'allocation_shadow_enabled_invalid'),
])
def test_settings_fail_closed_activation(tmp_path, changes, reason):
    store = PredictionArbitrageStore(tmp_path)
    before = store.lp_load_current_settings(ACCOUNT)
    with pytest.raises(ValueError, match=reason):
        store.lp_save_current_settings(ACCOUNT, expected_config_version=0, changes=changes)
    assert store.lp_load_current_settings(ACCOUNT) == before


def test_settings_zero_ranks_and_missing_fields(tmp_path):
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    store = PredictionArbitrageStore(tmp_path)
    before = store.lp_load_current_settings(ACCOUNT)
    assert not set(before) & {'allocation_mode', 'allocation_weights', 'allocation_shadow_enabled'}
    policy = LPD2LiveExecution._policy_from_settings(before)
    assert policy.allocation_mode == 'minimum'
    assert policy.allocation_weights == () and policy.allocation_shadow_enabled is False
    saved = store.lp_save_current_settings(ACCOUNT, expected_config_version=0,
        changes=dict(allocation_weights=[], allocation_shadow_enabled=True))
    assert saved['allocation_weights'] == []


def test_settings_d2_policy_state_round_trip(tmp_path, monkeypatch):
    from tests.test_lp_d2_execution_bridge import bridge_runtime, attach
    live, producer, sdk, public, now = bridge_runtime(tmp_path, monkeypatch, target_count=3)
    try:
        execution = attach(live, producer, tmp_path)
        before = execution.lp_auto_state()
        assert 'allocation_mode' not in before
        execution.lp_auto_configure(dict(allocation_weights=WEIGHTS, allocation_shadow_enabled=True,
            allocation_mode='minimum', expected_config_version=1))
        live._settings_thread.join(timeout=3)
        assert not live._settings_thread.is_alive()
        state = execution.lp_auto_state()
        for key, expected in dict(allocation_weights=WEIGHTS, allocation_shadow_enabled=True,
                allocation_mode='minimum').items():
            assert state[key] == expected and state['effective_settings'][key] == expected
        assert live.policy.allocation_weights == tuple(WEIGHTS)
        assert state['trading_config_version'] == before['trading_config_version']
        assert state['desired_running'] == before['desired_running']
        with pytest.raises(ValueError, match='rank_weights_requires_c2'):
            execution.lp_auto_configure(dict(allocation_mode='rank_weights', expected_config_version=2))
        assert execution.lp_auto_state()['config_version'] == 2
        assert sdk.posts == [] and sdk.cancels == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_allocation_http_round_trip(tmp_path, monkeypatch):
    from tests.test_lp_auto_control import runtime_for
    from tests.test_lp_d2_execution_bridge import bridge_runtime, attach
    from tests.test_prediction_service import _server, _response, _production_request
    import json
    live, producer, sdk, public, now = bridge_runtime(tmp_path, monkeypatch, target_count=3)
    try:
        execution = attach(live, producer, tmp_path)
        runtime = runtime_for(tmp_path / 'http')
        runtime.execution = execution
        with _server(runtime, session_token='session-token', csrf_token='csrf-token') as base:
            root = '/api/prediction-arbitrage/lp/auto/'
            payload = dict(allocation_mode='minimum', allocation_weights=WEIGHTS,
                allocation_shadow_enabled=True, expected_config_version=1)
            status, result = _response(_production_request(base, root+'config', json.dumps(payload).encode()))
            assert status == 200, result
            live._settings_thread.join(timeout=3)
            status, state = _response(base+root+'state')
            assert status == 200 and state['allocation_weights'] == WEIGHTS
            status, error = _response(_production_request(base, root+'config',
                json.dumps(dict(allocation_mode='rank_weights')).encode()))
            assert status == 400 and 'rank_weights_requires_c2' in str(error)
        assert sdk.posts == [] and sdk.cancels == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_final_quantity_rechecks_reward_precision_and_stress():
    from open_trader.polymarket_lp_risk import evaluate_lp_entry
    from tests.test_polymarket_lp_risk import _direction, _account, NOW
    direction = _direction()
    assert evaluate_lp_entry(direction, account=_account(), now=NOW,
        quantity=Decimal('20'))['state'] == 'eligible'
    for value, code in [('19.99', 'below_reward_minimum'), ('20.001', 'quantity_precision_invalid'),
                        ('1000', 'exit_liquidity_insufficient')]:
        result = evaluate_lp_entry(direction, account=_account(), now=NOW, quantity=Decimal(value))
        assert result['state'] != 'eligible' and code in result['reason_codes']
    for value in (True, Decimal('NaN'), Decimal('Infinity'), Decimal('0'), Decimal('-1')):
        assert evaluate_lp_entry(direction, account=_account(), now=NOW, quantity=value)['state'] != 'eligible'


def test_allocation_cli_round_trip(monkeypatch, capsys):
    import json
    from open_trader import cli, lp_auto_cli
    state = dict(budget_usd='100', target_buy_count=3, buy_price_level=1,
        config_version=1, desired_running=True, pause_confirmed=False,
        round_interval_seconds=30, api_retry_interval_seconds=60, order_check_interval_seconds=10)
    calls = []
    class Client:
        def __init__(self, *args): pass
        def request(self, path): return dict(state)
        def write(self, action, payload):
            calls.append((action, payload))
            state.update({k:v for k,v in payload.items() if k != 'expected_config_version'})
            state['config_version'] += 1
            return dict(state)
        def redact(self, value): return value
    monkeypatch.setattr(lp_auto_cli, '_Client', Client)
    assert cli.main(['prediction-arb', 'lp-auto', 'config', '--allocation-mode', 'minimum',
        '--allocation-weights', '.5,.3,.2', '--allocation-shadow-enabled', 'true', '--json']) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['result'] == 'CONFIGURED'
    assert calls == [('config', dict(allocation_mode='minimum', allocation_weights=['.5','.3','.2'],
        allocation_shadow_enabled=True, expected_config_version=1))]
    assert state['desired_running'] is True and state['budget_usd'] == '100'
    calls.clear()
    for args in [('--allocation-mode','rank_weights'), ('--allocation-weights','NaN,.3,.2'),
                 ('--allocation-shadow-enabled','1')]:
        try:
            code = cli.main(['prediction-arb','lp-auto','config',*args,'--json'])
        except SystemExit as exc:
            code = exc.code
        assert code == 2
        assert json.loads(capsys.readouterr().out)['reason']
    assert calls == []


def allocation_frame(*, budget='100', wallet='100', minimums=('20','20','20'), existing=False):
    from datetime import UTC, datetime
    from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    from open_trader.polymarket_lp_market_reader import CapturedSources, MarketReader, SourceResult
    from tests.test_polymarket_lp_risk import _direction
    now = datetime(2026,10,20,tzinfo=UTC)
    rows=[]
    for i, minimum in enumerate(minimums, 1):
        direction = _direction(reward_min_size=minimum)
        direction['market'].update(condition_id=f'c{i}',token_id=f't{i}', market_id=f'm{i}',
            metadata_checked_at=now, fees_checked_at=now)
        direction['book'].update(condition_id=f'c{i}',token_id=f't{i}',received_at=now,
            bids=[dict(price='.20',size='10000'),dict(price='.19',size='10000')],
            asks=[dict(price='.22',size='10000')])
        direction.update(reward_checked_at=now, daily_pool_usd=str(1000/i))
        rows.append(dict(condition_id=f'c{i}',token_id=f't{i}',market_id=f'm{i}',
            price=Decimal('.20'),quantity=Decimal(minimum),direction=direction,evaluation_at=now))
    orders = [dict(account_id='account-A',order_id='old',condition_id='c-old',token_id='t-old',
        side='BUY',status='LIVE',price='.20',original_size='100',size_matched='0',created_at=now)] if existing else []
    sources=dict(balance=SourceResult('balance',(wallet,wallet),checked_at=now),
        open_orders=SourceResult('orders',orders,checked_at=now),
        positions=SourceResult('positions',[],checked_at=now))
    if existing:
        sources['market:c-old:t-old']=SourceResult('market',dict(
            metadata=dict(condition_id='c-old',reward_min_size='20',reward_max_spread='.10'),
            rewards=dict(complete=True,checked_at=now,markets=[dict(condition_id='c-old',daily_pool_usd='.01')]),
            book=dict(condition_id='c-old',token_id='t-old',received_at=now,
                bids=[dict(price='.20',size='10000')],asks=[dict(price='.22',size='10000')])),checked_at=now)
    raw=MarketReader.from_captured_sources(CapturedSources('account-A',sources))
    policy=CalculationPolicy(Decimal(budget),3,allocation_weights=tuple(WEIGHTS),allocation_shadow_enabled=True)
    prepared=prepare_current_inputs(raw,SourceResult('candidates',rows,checked_at=now),policy,now)
    return prepared,policy,now


def test_rank_weights_allocate_50_30_20():
    from open_trader.polymarket_lp_market_calculation import calculate_current
    prepared,policy,now=allocation_frame()
    result=calculate_current(prepared,policy,now)
    shadow=result.allocation_shadow
    assert shadow['state']=='known'
    assert [row['quantity'] for row in shadow['ranks']]==[Decimal('250'),Decimal('150'),Decimal('100')]
    assert [row['target_usd'] for row in shadow['ranks']]==[Decimal('50'),Decimal('30'),Decimal('20')]
    assert [row['status'] for row in shadow['ranks']]==['WOULD_SEND']*3
    assert shadow['leftover_cash_usd']==0
    assert all(row['estimated_yield_pct_per_hour'] is not None for row in shadow['ranks'])
    assert result.target_tokens==('t1','t2','t3')


@pytest.mark.parametrize('minimums,existing,expected,reason,leftover', [
    (('20','20','20'),False,['WOULD_SEND']*3,None,'0'),
    (('20','20','60'),False,['WOULD_SEND','WOULD_SEND','NOT_SENT'],'below_reward_minimum','10'),
    (('149','20','20'),False,['NOT_SENT']*3,'below_reward_minimum','50'),
    (('20','20','20'),True,['WOULD_SEND','NOT_SENT','NOT_SENT'],'insufficient_funds','5'),
])
def test_shadow_contiguous_prefix(minimums, existing, expected, reason, leftover):
    from open_trader.polymarket_lp_market_calculation import calculate_current
    prepared,policy,now=allocation_frame(budget='50',wallet='50',minimums=minimums,existing=existing)
    shadow=calculate_current(prepared,policy,now).allocation_shadow
    assert shadow['state']=='known'
    assert [row['target_usd'] for row in shadow['ranks']]==[Decimal('25'),Decimal('15'),Decimal('10')]
    assert [row['status'] for row in shadow['ranks']]==expected
    assert shadow['reason_codes']==((reason,) if reason else ())
    assert shadow['leftover_cash_usd']==Decimal(leftover)
    if reason:
        first=expected.index('NOT_SENT')
        assert reason in shadow['ranks'][first]['reason_codes']
        assert all(row['reason_codes']==('prefix_blocked',) for row in shadow['ranks'][first+1:])


def test_shadow_capture_preserves_unaffordable_high_rank():
    from open_trader.polymarket_lp_current_inputs import QualifiedPoolCapture, prepare_current_inputs
    from open_trader.polymarket_lp_market_reader import SourceResult, freeze
    from open_trader.polymarket_lp_market_calculation import calculate_current
    prepared,policy,now=allocation_frame(budget='50',wallet='50',minimums=('300','20','20'))
    rows={}
    for row in prepared.sources['candidates'].value:
        direction=dict(row['direction'])
        direction['history_summary']=dict(condition_id=row['condition_id'],token_id=row['token_id'],
            state='known',checked_at=now,amplitude=Decimal('.01'))
        rows[row['condition_id']]=SourceResult('source-'+row['condition_id'],freeze(dict(
            row=dict(send_options=()),facts=dict(directions=[direction]))),checked_at=now)
    capture=QualifiedPoolCapture('capture',now,True,1,0,rows)
    captured=prepare_current_inputs(prepared,capture,policy,now)
    assert [row['condition_id'] for row in captured.sources['candidates'].value]==['c2','c3']
    result=calculate_current(captured,policy,now)
    shadow=result.allocation_shadow
    assert [row['condition_id'] for row in shadow['ranks']]==['c1','c2','c3']
    assert [row['status'] for row in shadow['ranks']]==['NOT_SENT']*3
    assert 'below_reward_minimum' in shadow['ranks'][0]['reason_codes']
    assert shadow['leftover_cash_usd']==50
    assert result.target_tokens==('t2','t3')  # Native minimum fallback stays intact.


@pytest.mark.parametrize('fault', [False,True])
def test_shadow_preserves_native_calls_fixed_plan_and_outcomes(tmp_path,monkeypatch,fault):
    from tests.test_lp_d2_execution_bridge import bridge_runtime, attach
    from tests.test_lp_order_registration_contract import WALLET, _open_order
    from open_trader import polymarket_lp_allocation
    results=[]
    for enabled in (False,True):
        live,producer,sdk,public,now=bridge_runtime(tmp_path/str(enabled),monkeypatch,target_count=3,candidate_count=3)
        try:
            execution=attach(live,producer,tmp_path/str(enabled))
            sentinel=_open_order('manual-sell-sentinel','SELL',price='.40',original='10')
            sdk.orders=(sentinel,)
            if enabled:
                account=hashlib.sha256(WALLET.strip().casefold().encode()).hexdigest()
                live.store.lp_save_current_settings(account,expected_config_version=1,
                    changes=dict(allocation_weights=WEIGHTS,allocation_shadow_enabled=True))
                live.load_settings()
            if fault:
                def fail(*a,**kw): raise RuntimeError('must not leak exception details')
                monkeypatch.setattr(polymarket_lp_allocation,'rank_weights_shadow',fail)
            captured=producer.capture_qualified_pool()
            live.run_once(captured)
            result=live.run_once(captured)
            diagnostics=execution.lp_auto_state()['diagnostics']
            if enabled:
                shadow=diagnostics['allocation_shadow']
                assert shadow['state']==('error' if fault else 'known')
                if fault:
                    assert shadow['error']=='RuntimeError'
                    assert 'must not leak' not in str(shadow)
            else:
                assert 'allocation_shadow' not in diagnostics
            assert sdk.orders==(sentinel,) and sdk.cancels==[]
            assert sdk.posts and all(post['side']=='BUY' and post['size']==20 for post in sdk.posts)
            results.append((sdk.posts[:],sdk.cancels[:],tuple(a.target for a in result.start.token.actions),
                tuple(o.status for o in result.outcomes),result.selection.targets,result.selection.target_tokens))
        finally:
            assert live.close(timeout=3)
            live.trading.close()
    assert results[0]==results[1]


def test_final_sized_yield_must_preserve_rank():
    from dataclasses import replace
    from open_trader.polymarket_lp_market_calculation import calculate_current
    from open_trader.polymarket_lp_market_reader import SourceResult, freeze
    prepared,policy,now=allocation_frame()
    rows=[{**row,'direction':{**row['direction'],'daily_pool_usd':pool}}
        for row,pool in zip(prepared.sources['candidates'].value,('1000','999','900'))]
    prepared=replace(prepared,sources={**prepared.sources,'candidates':SourceResult('candidates',freeze(rows))})
    result=calculate_current(prepared,policy,now)
    assert result.target_tokens==('t1','t2','t3')
    shadow=result.allocation_shadow
    assert shadow['reason_codes']==('allocation_rank_not_proven',)
    assert shadow['would_send_count']==0 and shadow['leftover_cash_usd']==100
    assert all(row['status']=='NOT_SENT' for row in shadow['ranks'])
    assert shadow['ranks'][0]['estimated_yield_pct_per_hour'] < shadow['ranks'][1]['estimated_yield_pct_per_hour']


def test_shadow_unknown_incumbent_blocks_without_becoming_victim():
    from dataclasses import replace
    from open_trader.polymarket_lp_market_calculation import calculate_current
    from open_trader.polymarket_lp_market_reader import SourceResult
    prepared,policy,now=allocation_frame(existing=True)
    prepared=replace(prepared,sources={**prepared.sources,'directions':SourceResult('directions',{})})
    result=calculate_current(prepared,policy,now)
    assert result.allocation_shadow['state']=='unknown'
    assert result.allocation_shadow['would_send_count']==0
    assert 'old' not in result.victim_order_ids
    assert result.buy_reserved==20


def test_shadow_retains_actual_quantity_without_resizing():
    from dataclasses import replace
    from open_trader.polymarket_lp_market_calculation import calculate_current
    from open_trader.polymarket_lp_market_reader import SourceResult, freeze
    prepared,policy,now=allocation_frame(existing=True)
    direction=prepared.sources['directions'].value['old']
    prepared=replace(prepared,sources={**prepared.sources,'directions':SourceResult('directions',freeze({
        'old':{**direction,'daily_pool_usd':'1200'}}))})
    shadow=calculate_current(prepared,policy,now).allocation_shadow
    first=shadow['ranks'][0]
    assert (first['order_id'],first['status'],first['quantity'],first['actual_usd'],first['target_usd']) == (
        'old','RETAINED',Decimal('100'),Decimal('20'),Decimal('50'))
    assert shadow['leftover_cash_usd']==30  # Existing $20 was reserved exactly once.


def test_shadow_is_pure_and_diagnostics_bounded(monkeypatch):
    from dataclasses import replace
    import socket,sqlite3,json
    from open_trader.polymarket_lp_market_calculation import calculate_current
    from open_trader.polymarket_lp_market_reader import SourceResult,freeze
    from open_trader.polymarket_lp_diagnostics import yield_diagnostic_value
    prepared,policy,now=allocation_frame()
    row=prepared.sources['candidates'].value[0]
    rows=[dict(row,condition_id='c'+str(i),token_id='t'+str(i),direction={**row['direction'],
        'market':{**row['direction']['market'],'condition_id':'c'+str(i),'token_id':'t'+str(i)},
        'book':{**row['direction']['book'],'condition_id':'c'+str(i),'token_id':'t'+str(i)}}) for i in range(50)]
    prepared=replace(prepared,sources={**prepared.sources,'candidates':SourceResult('candidates',freeze(rows))})
    policy=replace(policy,target_count=50,allocation_weights=('.02',)*50)
    def forbidden(*a,**kw): pytest.fail('shadow acquired external facts or a DB connection')
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(sqlite3,'connect',forbidden)
    shadow=calculate_current(prepared,policy,now).allocation_shadow
    payload=yield_diagnostic_value(shadow)
    assert len(payload['ranks'])==20 and payload['omitted_ranks']==30
    assert len(json.dumps(payload).encode()) < 16*1024


def test_default_settings_keep_diagnostics_and_estimator_payload(tmp_path,monkeypatch):
    import json
    from dataclasses import replace
    from tests.test_lp_d2_execution_bridge import bridge_runtime,attach
    from tests.test_lp_yield_observability import candidate,NOW
    from open_trader.polymarket_lp_risk import minimum_order_estimate
    from open_trader.polymarket_lp_market_calculation import calculate_current
    prepared,policy,now=allocation_frame()
    result=calculate_current(prepared,replace(policy,allocation_shadow_enabled=False,allocation_weights=()),now)
    assert result.allocation_shadow is None
    direction=candidate(pool='24')['direction']
    estimate=minimum_order_estimate(direction,dict(price=Decimal('.40'),quantity=Decimal('20')),NOW)
    assert estimate==dict(state='known',basis='minimum_scoring_order',quantity=Decimal('20'),price=Decimal('.40'),
        capital_usd=Decimal('8'),hourly_reward_usd=Decimal('.0625'),yield_pct_per_hour=Decimal('.78125'),checked_at=NOW)
    live,producer,sdk,public,now=bridge_runtime(tmp_path,monkeypatch)
    try:
        execution=attach(live,producer,tmp_path)
        live.run_once(producer.capture_qualified_pool())
        state=execution.lp_auto_state()
        assert not set(state)&{'allocation_mode','allocation_weights','allocation_shadow_enabled'}
        assert set(state['diagnostics'])=={'version','reader_coverage','latest_attempt','accepted_observation',
            'admission','local_outcomes','round_start','yield_observation','yield_rounds','protection','cancel_blocks','rotation','unknown_buy_yields'}
        assert 'allocation_shadow' not in json.dumps(state)
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('fault,reason', [('stress','risk_rejected'),('opening','opening_blocked'),('authority','current_facts_unknown')])
def test_shadow_risk_opening_and_authority_truncate_suffix(fault,reason):
    from dataclasses import replace
    from open_trader.polymarket_lp_market_calculation import calculate_current,select_current_composition
    from open_trader.polymarket_lp_market_reader import SourceResult,freeze
    prepared,policy,now=allocation_frame()
    if fault=='stress':
        rows=list(prepared.sources['candidates'].value)
        row=rows[1]
        direction=row['direction']
        rows[1]={**row,'direction':{**direction,'book':{**direction['book'],
            'bids':[dict(price='.20',size='10000'),dict(price='.19',size='30')]}}}
        prepared=replace(prepared,sources={**prepared.sources,'candidates':SourceResult('candidates',freeze(rows))})
    result=calculate_current(prepared,policy,now)
    shadow=select_current_composition(prepared,policy,result,as_of=now,
        financial_authority=fault!='authority',shadow_blocked_conditions=('c2',) if fault=='opening' else ()).allocation_shadow
    if fault=='authority':
        assert shadow['state']=='unknown' and shadow['would_send_count']==0
    else:
        assert [row['status'] for row in shadow['ranks']]==['WOULD_SEND','NOT_SENT','NOT_SENT']
        assert shadow['leftover_cash_usd']==50
        assert shadow['ranks'][2]['reason_codes']==('prefix_blocked',)
    assert reason in shadow['reason_codes']


def test_shadow_reports_final_yield_for_funded_prefix_suffix():
    from open_trader.polymarket_lp_market_calculation import calculate_current
    prepared,policy,now=allocation_frame(budget='50',wallet='50',existing=True)
    shadow=calculate_current(prepared,policy,now).allocation_shadow
    assert [r['status'] for r in shadow['ranks']]==['WOULD_SEND','NOT_SENT','NOT_SENT']
    assert all(r['estimated_yield_pct_per_hour'] is not None for r in shadow['ranks'])
    assert all(r['risk_state']=='eligible' for r in shadow['ranks'])
    assert shadow['ranks'][1]['reason_codes'][0]=='insufficient_funds'


def test_shadow_pending_cancel_incumbent_keeps_cash_reserved():
    from dataclasses import replace
    from open_trader.polymarket_lp_market_calculation import calculate_current
    prepared,policy,now=allocation_frame(existing=True)
    prepared=replace(prepared,orders=tuple({**r,'status':'CANCELING'} for r in prepared.orders))
    result=calculate_current(prepared,policy,now)
    assert result.allocation_shadow['state']=='unknown'
    assert result.allocation_shadow['would_send_count']==0
    assert result.buy_reserved==20


def test_shadow_capture_exception_leaves_minimum_unchanged():
    from dataclasses import replace
    from open_trader.polymarket_lp_current_inputs import QualifiedPoolCapture,prepare_current_inputs
    from open_trader.polymarket_lp_market_reader import SourceResult
    from open_trader.polymarket_lp_market_calculation import calculate_current
    prepared,policy,now=allocation_frame()
    class BrokenObservationMarket(dict):
        def get(self,key,*args):
            if key=='market_id': raise RuntimeError('shadow-only field failure')
            return super().get(key,*args)
    rows={}
    for index,row in enumerate(prepared.sources['candidates'].value):
        direction={**row['direction'],'history_summary':dict(condition_id=row['condition_id'],token_id=row['token_id'],
            state='known',checked_at=now,amplitude=Decimal('.01'))}
        if index==0:
            direction.update(reward_active=False,market=BrokenObservationMarket(direction['market']))
        rows[row['condition_id']]=SourceResult('source',dict(row={},facts=dict(directions=[direction])),checked_at=now)
    capture=QualifiedPoolCapture('capture',now,True,1,0,rows)
    off=prepare_current_inputs(prepared,capture,replace(policy,allocation_shadow_enabled=False),now)
    on=prepare_current_inputs(prepared,capture,policy,now)
    assert on.sources['candidates'].value==off.sources['candidates'].value
    result=calculate_current(on,policy,now)
    assert result.allocation_shadow['state']=='error'
    assert result.allocation_shadow['error']=='RuntimeError'
    assert result.target_tokens==('t2','t3')


def test_shadow_missing_candidate_values_stays_unknown():
    from dataclasses import replace
    from open_trader.polymarket_lp_market_reader import SourceResult
    from open_trader.polymarket_lp_market_calculation import calculate_current
    prepared,policy,now=allocation_frame()
    prepared=replace(prepared,sources={**prepared.sources,'candidates':SourceResult('candidates',None)})
    shadow=calculate_current(prepared,policy,now).allocation_shadow
    assert shadow['state']=='unknown' and shadow['would_send_count']==0
