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
    [' 0.5', '.3', '.2'], ['.5', '.3\t', '.2'], ['0_5e-1', '.3', '.2'],
    ['０.５', '.3', '.2'], ['٠.٥', '.3', '.2'],
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


def calculate_shadow(frame, policy, now):
    from open_trader.polymarket_lp_market_calculation import calculate_current, select_current_composition
    return select_current_composition(frame, policy, calculate_current(frame, policy, now),
        as_of=now, shadow_blocked_conditions=())


def test_rank_weights_allocate_50_30_20():
    prepared,policy,now=allocation_frame()
    result=calculate_shadow(prepared,policy,now)
    shadow=result.allocation_shadow
    assert shadow['state']=='known'
    assert [row['quantity'] for row in shadow['ranks']]==[Decimal('250'),Decimal('150'),Decimal('100')]
    assert [row['target_usd'] for row in shadow['ranks']]==[Decimal('50'),Decimal('30'),Decimal('20')]
    assert [row['status'] for row in shadow['ranks']]==['WOULD_SEND']*3
    assert shadow['leftover_cash_usd']==0
    assert all(row['estimated_yield_pct_per_hour'] is not None for row in shadow['ranks'])
    assert result.target_tokens==('t1','t2','t3')


@pytest.mark.parametrize('reserved,headroom', [('0', '100'), ('40', '60'), ('100', '0')])
def test_shadow_buy_budget_excludes_inventory_without_changing_native_plan(tmp_path, monkeypatch, reserved, headroom):
    from tests.test_lp_d2_execution_bridge import rotation_runtime
    from tests.test_lp_order_registration_contract import WALLET
    results = []
    for enabled in (False, True):
        path = tmp_path/str(enabled)
        live, execution, sdk, ids, pools, order, qualify = rotation_runtime(path, monkeypatch, wallet='400', target=3)
        try:
            if enabled:
                account = hashlib.sha256(WALLET.casefold().encode()).hexdigest()
                live.store.lp_save_current_settings(account, expected_config_version=1,
                    changes=dict(allocation_weights=WEIGHTS, allocation_shadow_enabled=True))
                live.load_settings()
            buys = (() if reserved == '0' else (order('A', original='80'),) if reserved == '40' else
                (order('A', original='80'), order('B', original='80'), order('C', original='40')))
            sentinel = order('Z', 'manual-sell').model_copy(update=dict(side='SELL', price=Decimal('.60')))
            sdk.orders = (*buys, sentinel)
            sdk.positions = (dict(user=WALLET, condition_id=ids['Z'][0], token_id=ids['Z'][1],
                size='300', initial_value='115.6884', redeemable=False),)
            qualify('F')
            result = live.run_once(execution._lp.capture_qualified_pool())
            assert result.selection.inventory_cost == Decimal('115.6884')
            assert result.selection.buy_reserved == Decimal(reserved)
            assert result.selection.new_capacity == Decimal(headroom)
            if enabled:
                shadow = result.selection.allocation_shadow
                assert shadow['target_envelope_usd'] == Decimal('100')
                assert shadow['available_cash_usd'] == Decimal(headroom)
            else:
                assert result.selection.allocation_shadow is None
            assert sdk.orders == (*buys, sentinel) and sdk.cancels == sdk.market_orders == []
            assert all(p['side'] == 'BUY' and p['price'] == Decimal('.50') and p['size'] == Decimal('20') for p in sdk.posts)
            results.append((sdk.posts[:], tuple(a.target for a in result.start.token.actions),
                tuple(o.status for o in result.outcomes), result.selection.target_tokens))
        finally:
            assert live.close(timeout=3)
            live.trading.close()
    assert results[0] == results[1]


@pytest.mark.parametrize('minimums,existing,expected,reason,leftover', [
    (('20','20','20'),False,['WOULD_SEND']*3,None,'0'),
    (('20','20','60'),False,['WOULD_SEND','WOULD_SEND','NOT_SENT'],'below_reward_minimum','10'),
    (('149','20','20'),False,['NOT_SENT']*3,'below_reward_minimum','50'),
    (('20','20','20'),True,['WOULD_SEND','NOT_SENT','NOT_SENT'],'insufficient_funds','5'),
])
def test_shadow_contiguous_prefix(minimums, existing, expected, reason, leftover):
    prepared,policy,now=allocation_frame(budget='50',wallet='50',minimums=minimums,existing=existing)
    shadow=calculate_shadow(prepared,policy,now).allocation_shadow
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
    result=calculate_shadow(captured,policy,now)
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
            shadow_calls = []
            if enabled and not fault:
                original_shadow = polymarket_lp_allocation.rank_weights_shadow
                def counted(*args, **kwargs):
                    shadow_calls.append(kwargs['authority'])
                    return original_shadow(*args, **kwargs)
                monkeypatch.setattr(polymarket_lp_allocation, 'rank_weights_shadow', counted)
            if fault and enabled:
                def fail(*a,**kw): raise RuntimeError('must not leak exception details')
                monkeypatch.setattr(polymarket_lp_allocation,'rank_weights_shadow',fail)
            captured=producer.capture_qualified_pool()
            live.run_once(captured)
            result=live.run_once(captured)
            diagnostics=execution.lp_auto_state()['diagnostics']
            if enabled and not fault:
                assert shadow_calls == [True, True]  # One compute per live pass, not two.
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
    from open_trader.polymarket_lp_market_reader import SourceResult, freeze
    prepared,policy,now=allocation_frame()
    rows=[{**row,'direction':{**row['direction'],'daily_pool_usd':pool}}
        for row,pool in zip(prepared.sources['candidates'].value,('1000','999','900'))]
    prepared=replace(prepared,sources={**prepared.sources,'candidates':SourceResult('candidates',freeze(rows))})
    result=calculate_shadow(prepared,policy,now)
    assert result.target_tokens==('t1','t2','t3')
    shadow=result.allocation_shadow
    assert shadow['reason_codes']==('allocation_rank_not_proven',)
    assert shadow['would_send_count']==0 and shadow['leftover_cash_usd']==100
    assert all(row['status']=='NOT_SENT' for row in shadow['ranks'])
    assert shadow['ranks'][0]['estimated_yield_pct_per_hour'] < shadow['ranks'][1]['estimated_yield_pct_per_hour']


def test_shadow_unknown_incumbent_blocks_without_becoming_victim():
    from dataclasses import replace
    from open_trader.polymarket_lp_market_reader import SourceResult
    prepared,policy,now=allocation_frame(existing=True)
    prepared=replace(prepared,sources={**prepared.sources,'directions':SourceResult('directions',{})})
    result=calculate_shadow(prepared,policy,now)
    assert result.allocation_shadow['state']=='unknown'
    assert result.allocation_shadow['would_send_count']==0
    assert 'old' not in result.victim_order_ids
    assert result.buy_reserved==20


def test_shadow_retains_actual_quantity_without_resizing():
    from dataclasses import replace
    from open_trader.polymarket_lp_market_reader import SourceResult, freeze
    prepared,policy,now=allocation_frame(existing=True)
    direction=prepared.sources['directions'].value['old']
    prepared=replace(prepared,sources={**prepared.sources,'directions':SourceResult('directions',freeze({
        'old':{**direction,'daily_pool_usd':'1200'}}))})
    shadow=calculate_shadow(prepared,policy,now).allocation_shadow
    first=shadow['ranks'][0]
    assert (first['order_id'],first['status'],first['quantity'],first['actual_usd'],first['target_usd']) == (
        'old','RETAINED',Decimal('100'),Decimal('20'),Decimal('50'))
    assert shadow['leftover_cash_usd']==30  # Existing $20 was reserved exactly once.


def test_shadow_is_pure_and_diagnostics_bounded(monkeypatch):
    from dataclasses import replace
    import socket,sqlite3,json
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
    shadow=calculate_shadow(prepared,policy,now).allocation_shadow
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
            'admission','local_outcomes','round_start','yield_observation','yield_rounds','protection','cancel_blocks','rotation','unknown_buy_yields','instance_ownership'}
        assert state['diagnostics']['instance_ownership']==dict(instance_id=None,managed_order_ids=[],
            unattributed_buy_ids=[],ownership_allows_new_risk=True,
            financial_admission='requires_current_wallet_checks',alert_code=None)
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
    result=calculate_shadow(prepared,policy,now)
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
    prepared,policy,now=allocation_frame(budget='50',wallet='50',existing=True)
    shadow=calculate_shadow(prepared,policy,now).allocation_shadow
    assert [r['status'] for r in shadow['ranks']]==['WOULD_SEND','NOT_SENT','NOT_SENT']
    assert all(r['estimated_yield_pct_per_hour'] is not None for r in shadow['ranks'])
    assert all(r['risk_state']=='eligible' for r in shadow['ranks'])
    assert shadow['ranks'][1]['reason_codes'][0]=='insufficient_funds'


def test_shadow_pending_cancel_incumbent_keeps_cash_reserved():
    from dataclasses import replace
    prepared,policy,now=allocation_frame(existing=True)
    prepared=replace(prepared,orders=tuple({**r,'status':'CANCELING'} for r in prepared.orders))
    result=calculate_shadow(prepared,policy,now)
    assert result.allocation_shadow['state']=='unknown'
    assert result.allocation_shadow['would_send_count']==0
    assert result.buy_reserved==20


def test_shadow_capture_exception_leaves_minimum_unchanged():
    from dataclasses import replace
    from open_trader.polymarket_lp_current_inputs import QualifiedPoolCapture,prepare_current_inputs
    from open_trader.polymarket_lp_market_reader import SourceResult
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
    result=calculate_shadow(on,policy,now)
    assert result.allocation_shadow['state']=='error'
    assert result.allocation_shadow['error']=='RuntimeError'
    assert result.target_tokens==('t2','t3')


def test_shadow_missing_candidate_values_stays_unknown():
    from dataclasses import replace
    from open_trader.polymarket_lp_market_reader import SourceResult
    prepared,policy,now=allocation_frame()
    prepared=replace(prepared,sources={**prepared.sources,'candidates':SourceResult('candidates',None)})
    shadow=calculate_shadow(prepared,policy,now).allocation_shadow
    assert shadow['state']=='unknown' and shadow['would_send_count']==0


@pytest.mark.parametrize('bad,reason', [
    (dict(allocation_mode='rank_weights'), 'rank_weights_requires_c2'),
    (dict(allocation_weights=['bad', '.3', '.2']), 'allocation_weights_invalid'),
    (dict(allocation_mode='invalid'), 'allocation_mode_invalid'),
    (dict(allocation_shadow_enabled=1), 'allocation_shadow_enabled_invalid'),
])
def test_stored_invalid_allocation_reports_failure_and_allows_repair(tmp_path, monkeypatch, bad, reason):
    import json
    from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
    from open_trader.polymarket_lp_market_calculation import CalculationPolicy
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from tests.test_lp_d2_execution_bridge import bridge_runtime, attach
    from tests.test_lp_d2_integration import settle_control
    live, producer, sdk, public, now = bridge_runtime(tmp_path, monkeypatch, target_count=3)
    assert live.close(timeout=3)
    with live.store._transaction() as connection:
        document = json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()['payload'])
        document.update(allocation_mode='minimum', allocation_weights=WEIGHTS, allocation_shadow_enabled=False)
        document.update(bad)
        connection.execute('UPDATE lp_auto_pool SET payload=? WHERE singleton=1', (json.dumps(document),))
    account = live._settings_account()
    with pytest.raises(ValueError, match=reason):
        live.store.lp_load_current_settings(account)
    recovered = LPD2LiveExecution(live.store, live.trading, account_id=live.account_id, venue='polymarket',
        process_epoch='c1-invalid-allocation', policy=CalculationPolicy(None), utc_now=lambda: now,
        load_settings_on_start=True)
    try:
        recovered.initialize(ValidatedStartAdmission('c1-invalid-allocation', 'offline-permit'))
        execution = attach(recovered, producer, tmp_path)
        status = execution.lp_auto_state()
        assert status['settings_error'] == reason
        assert recovered.settings_status()['loaded'] is False
        assert recovered.policy.allocation_mode == 'minimum'
        recovered.run_once(producer.capture_qualified_pool())
        recovered.run_once(producer.capture_qualified_pool())
        assert sdk.posts == [] and sdk.cancels == []
        recovered.request_running(False)
        settle_control(recovered)
        assert recovered.state.snapshot().current.effective_running is False
        recovered.request_running(True)
        settle_control(recovered)
        assert recovered.state.snapshot().current.effective_running is True
        with pytest.raises(ValueError, match='rank_weights_requires_c2'):
            recovered.configure(dict(allocation_mode='rank_weights', expected_config_version=1))
        recovered.configure(dict(allocation_mode='minimum', allocation_weights=WEIGHTS,
            allocation_shadow_enabled=False, expected_config_version=1))
        recovered._settings_thread.join(timeout=3)
        assert not recovered._settings_thread.is_alive()
        repaired = recovered.settings_status()
        assert repaired['loaded'] is True and repaired['error'] is None and not repaired['pending']
        assert recovered.store.lp_load_current_settings(account)['allocation_mode'] == 'minimum'
        assert recovered.policy.allocation_mode == 'minimum'
        assert sdk.posts == [] and sdk.cancels == []
    finally:
        assert recovered.close(timeout=3)
        live.trading.close()


def test_shadow_only_computes_at_live_seam_and_reuses_500_candidate_yields(monkeypatch):
    from dataclasses import replace
    from open_trader import polymarket_lp_allocation as allocation, polymarket_lp_market_calculation as calculation
    from open_trader.polymarket_lp_market_reader import SourceResult, freeze
    prepared, policy, now = allocation_frame()
    row = prepared.sources['candidates'].value[0]
    rows = [dict(row, condition_id='c'+str(i), token_id='t'+str(i), direction={**row['direction'],
        'daily_pool_usd': str(1000000/(i+1)),
        'market': {**row['direction']['market'], 'condition_id':'c'+str(i), 'token_id':'t'+str(i)},
        'book': {**row['direction']['book'], 'condition_id':'c'+str(i), 'token_id':'t'+str(i)}}) for i in range(500)]
    prepared = replace(prepared, sources={**prepared.sources, 'candidates': SourceResult('candidates', freeze(rows))})
    calls = []
    estimator = allocation.minimum_order_estimate
    def counted(*args, **kwargs):
        calls.append((args[1]['quantity'], kwargs.get('include_observation', False)))
        return estimator(*args, **kwargs)
    monkeypatch.setattr(allocation, 'minimum_order_estimate', counted)
    monkeypatch.setattr(calculation, 'minimum_order_estimate', counted)
    result = calculation.calculate_current(prepared, policy, now)
    assert result.allocation_shadow is None
    assert len(calls) == 500
    selected = calculation.select_current_composition(prepared, policy, result, as_of=now, shadow_blocked_conditions=())
    assert selected.allocation_shadow['state'] == 'known'
    assert len(calls) == 503  # One native minimum estimate per candidate, three final sizes.


@pytest.mark.parametrize('fault', ['serializer', 'log'])
def test_shadow_publication_failure_preserves_iteration_summary(tmp_path, monkeypatch, fault):
    from open_trader import polymarket_lp_live_execution as module
    from tests.test_lp_d2_execution_bridge import bridge_runtime, attach
    live, producer, sdk, public, now = bridge_runtime(tmp_path, monkeypatch, target_count=3, candidate_count=3)
    try:
        execution = attach(live, producer, tmp_path)
        live.store.lp_save_current_settings(live._settings_account(), expected_config_version=1,
            changes=dict(allocation_weights=WEIGHTS, allocation_shadow_enabled=True))
        live.load_settings()
        if fault == 'serializer':
            original = module.yield_diagnostic_value
            def fail_shadow(value, **kwargs):
                if hasattr(value, 'get') and value.get('rank_limit') == 20:
                    raise RuntimeError('private publication details')
                return original(value, **kwargs)
            monkeypatch.setattr(module, 'yield_diagnostic_value', fail_shadow)
        else:
            def fail_log(*args, **kwargs):
                raise RuntimeError('private log details')
            monkeypatch.setattr(module.logger, 'info', fail_log)
        result = live.run_once(producer.capture_qualified_pool())
        diagnostics = execution.lp_auto_state()['diagnostics']
        assert diagnostics['accepted_observation']['observation_id'] == result.acceptance.observation.observation_id
        assert diagnostics['allocation_shadow']['state'] == 'error'
        assert diagnostics['allocation_shadow']['error'] == 'RuntimeError'
        assert 'private' not in str(diagnostics)
        assert diagnostics['yield_observation']['available'] is True
        assert sdk.cancels == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_shadow_schema_is_identical_for_known_unknown_and_error(monkeypatch):
    from open_trader import polymarket_lp_allocation as allocation
    from open_trader.polymarket_lp_market_calculation import calculate_current, select_current_composition
    prepared, policy, now = allocation_frame()
    values = calculate_current(prepared, policy, now)
    known = select_current_composition(prepared, policy, values, as_of=now, shadow_blocked_conditions=()).allocation_shadow
    unknown = select_current_composition(prepared, policy, values, as_of=now,
        financial_authority=False, shadow_blocked_conditions=()).allocation_shadow
    def fail(*args, **kwargs):
        raise RuntimeError('private')
    monkeypatch.setattr(allocation, 'rank_weights_shadow', fail)
    error = select_current_composition(prepared, policy, values, as_of=now, shadow_blocked_conditions=()).allocation_shadow
    expected = {'state', 'error', 'reason_codes', 'checked_at', 'target_envelope_usd', 'available_cash_usd',
        'leftover_cash_usd', 'would_send_count', 'retained_count', 'not_sent_count', 'missing_rank_count',
        'rank_limit', 'omitted_ranks', 'ranks', 'excluded_count', 'excluded', 'candidates_unknown_count'}
    assert set(known) == set(unknown) == set(error) == expected
    assert (known['state'], unknown['state'], error['state']) == ('known', 'unknown', 'error')
    assert unknown['would_send_count'] == error['would_send_count'] == 0


def test_shadow_mixed_pool_excludes_unknown_candidates_without_poisoning_rank():
    from dataclasses import replace
    from open_trader.polymarket_lp_current_inputs import QualifiedPoolCapture, prepare_current_inputs
    from open_trader.polymarket_lp_market_reader import SourceResult, freeze
    from open_trader.polymarket_lp_market_calculation import calculate_current, select_current_composition
    prepared, policy, now = allocation_frame()
    rows = {}
    for row in prepared.sources['candidates'].value:
        direction = {**row['direction'], 'history_summary': dict(condition_id=row['condition_id'],
            token_id=row['token_id'], state='known', checked_at=now, amplitude=Decimal('.01'))}
        rows[row['condition_id']] = SourceResult('source-'+row['condition_id'], freeze(dict(
            row={}, facts=dict(directions=[direction]))), checked_at=now)
    sample = rows['c1'].value['facts']['directions'][0]
    rows['failed-market'] = SourceResult('failed-source', complete=False, error='timeout')
    history_unknown = {**sample, 'market': {**sample['market'], 'condition_id':'history', 'token_id':'history-token'}}
    rows['history'] = SourceResult('history-source', freeze(dict(row={}, facts=dict(directions=[history_unknown]))))
    capture = QualifiedPoolCapture('mixed', now, True, 1, 0, rows)
    off = prepare_current_inputs(prepared, capture, replace(policy, allocation_shadow_enabled=False), now)
    on = prepare_current_inputs(prepared, capture, policy, now)
    assert on.sources['candidates'] == off.sources['candidates']
    # An eligible candidate with an uncomputable yield is excluded too.
    shadow_rows = list(on.sources['allocation_candidates'].value)
    shadow_rows.append(dict(condition_id='yield', token_id='yield-token',
        direction={**sample, 'daily_pool_usd':None}, eligibility=dict(state='eligible', reason_codes=())))
    on = replace(on, sources={**on.sources, 'allocation_candidates':replace(on.sources['allocation_candidates'], value=freeze(shadow_rows))})
    values = calculate_current(on, policy, now)
    shadow = select_current_composition(on, policy, values, as_of=now, shadow_blocked_conditions=()).allocation_shadow
    assert shadow['state'] == 'known' and shadow['would_send_count'] == 3
    assert [r['token_id'] for r in shadow['ranks']] == ['t1', 't2', 't3']
    assert shadow['candidates_unknown_count'] == shadow['excluded_count'] == 3
    assert {r['condition_id'] for r in shadow['excluded']} == {'failed-market', 'history', 'yield'}
    assert all('candidate_unknown' in r['reason_codes'] for r in shadow['excluded'])
    # Fewer known candidates remains a known partial pool, with cash left unallocated.
    fewer = replace(on, sources={**on.sources, 'allocation_candidates':replace(on.sources['allocation_candidates'],
        value=tuple(r for r in shadow_rows if r['condition_id'] != 'c3'))})
    partial = select_current_composition(fewer, policy, calculate_current(fewer, policy, now),
        as_of=now, shadow_blocked_conditions=()).allocation_shadow
    assert partial['state'] == 'known' and partial['would_send_count'] == 2
    assert partial['missing_rank_count'] == 1 and partial['leftover_cash_usd'] == 20


def test_acceptance_failure_never_publishes_default_authority_shadow(tmp_path, monkeypatch):
    from dataclasses import replace
    from open_trader import polymarket_lp_allocation as allocation
    from tests.test_lp_d2_execution_bridge import bridge_runtime, attach
    live, producer, sdk, public, now = bridge_runtime(tmp_path, monkeypatch, target_count=3, candidate_count=3)
    try:
        execution = attach(live, producer, tmp_path)
        live.store.lp_save_current_settings(live._settings_account(), expected_config_version=1,
            changes=dict(allocation_weights=WEIGHTS, allocation_shadow_enabled=True))
        live.load_settings()
        original_read = live.reader.read_current
        monkeypatch.setattr(live.reader, 'read_current', lambda *a, **kw:replace(original_read(*a, **kw), account='wrong-account'))
        calls = []
        original_shadow = allocation.rank_weights_shadow
        def counted(*args, **kwargs):
            calls.append(kwargs)
            return original_shadow(*args, **kwargs)
        monkeypatch.setattr(allocation, 'rank_weights_shadow', counted)
        result = live.run_once(producer.capture_qualified_pool())
        assert result.acceptance.observation is None and result.acceptance.status == 'invalid_identity'
        diagnostics = execution.lp_auto_state()['diagnostics']
        assert diagnostics['latest_attempt']['disposition'] == 'invalid_identity'
        assert 'allocation_shadow' not in diagnostics
        assert calls == [] and sdk.posts == [] and sdk.cancels == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_manual_read_does_not_compute_shadow(tmp_path, monkeypatch):
    from dataclasses import replace
    from open_trader import polymarket_lp_allocation as allocation
    from tests.test_lp_d2_manual_bridge import manual_runtime, entry
    execution, live, sdk, public, clock = manual_runtime(tmp_path, monkeypatch)
    try:
        live.policy = replace(live.policy, allocation_shadow_enabled=True, allocation_weights=('1',))
        calls = []
        original = allocation.rank_weights_shadow
        def counted(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)
        monkeypatch.setattr(allocation, 'rank_weights_shadow', counted)
        preview = execution.lp_preview(entry(clock))
        assert preview['state'] == 'previewed'
        assert live.state.snapshot().observation.calculation.allocation_shadow is None
        assert calls == [] and sdk.posts == [] and sdk.cancels == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_running_allocation_only_save_drops_stale_rows_without_cancel(tmp_path, monkeypatch):
    from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
    from tests.test_lp_d2_execution_bridge import bridge_runtime, attach
    live, producer, sdk, public, now = bridge_runtime(tmp_path, monkeypatch, target_count=3, candidate_count=3)
    try:
        execution = attach(live, producer, tmp_path)
        first = live.run_once(producer.capture_qualified_pool())
        stale = prepare_current_inputs(first.acceptance.observation.source_frame,
            producer.capture_qualified_pool(), live.policy, now).sources['candidates']
        assert all(row['policy_version'] == 1 for row in stale.value)
        execution.lp_auto_configure(dict(allocation_weights=WEIGHTS, allocation_shadow_enabled=False, expected_config_version=1))
        live._settings_thread.join(timeout=3)
        assert not live._settings_thread.is_alive()
        state = execution.lp_auto_state()
        assert state['effective_running'] is True and state['config_version'] == 2
        assert state['trading_config_version'] == 1
        result = live.run_once(stale)
        assert result.selection.new_target_keys
        assert result.start.token is None  # No sealed plan for an old policy row.
        assert sdk.posts == [] and sdk.cancels == []
        fresh = live.run_once(producer.capture_qualified_pool())
        assert fresh.outcomes and sdk.posts
        assert all(post['size'] == 20 and post['side'] == 'BUY' for post in sdk.posts)
        assert sdk.cancels == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_shadow_flag_preserves_all_other_diagnostics_and_source_consumers(tmp_path, monkeypatch):
    from dataclasses import replace
    from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
    from open_trader.polymarket_lp_market_calculation import calculate_current, select_current_composition
    from tests.test_lp_d2_execution_bridge import bridge_runtime, attach
    live, producer, sdk, public, now = bridge_runtime(tmp_path, monkeypatch, target_count=3, candidate_count=3)
    try:
        execution = attach(live, producer, tmp_path)
        capture = producer.capture_qualified_pool()
        result = live.run_once(capture)
        observation = result.acceptance.observation
        raw, ticket = observation.source_frame, observation.read_ticket
        before_state = live.state.snapshot()
        outputs = []
        for enabled in (False, True):
            policy = replace(live.policy, allocation_shadow_enabled=enabled, allocation_weights=tuple(WEIGHTS))
            prepared = prepare_current_inputs(raw, capture, policy, now)
            if enabled:
                assert set(prepared.sources) == set(off_sources) | {'allocation_candidates'}
                assert {k:v for k,v in prepared.sources.items() if k != 'allocation_candidates'} == off_sources
            else:
                off_sources = dict(prepared.sources)
            values = calculate_current(prepared, policy, now)
            selected = select_current_composition(prepared, policy, values, as_of=now, shadow_blocked_conditions=())
            if enabled:
                assert replace(selected, allocation_shadow=None) == off_selected
            else:
                off_selected = selected
            live._record_iteration(ticket, prepared, selected, result.acceptance, result.start, (), None,
                capture, now, now)
            diagnostic = execution.lp_auto_state()['diagnostics']
            if enabled:
                assert diagnostic['allocation_shadow']['state'] == 'known'
            outputs.append({k:v for k,v in diagnostic.items() if k != 'allocation_shadow'})
        assert outputs[0] == outputs[1]  # Includes coverage, admission, yields, rotation and all other keys.
        assert live.state.snapshot() == before_state
        assert 'allocation_candidates' not in observation.source_frame.sources  # Acceptance hashes raw facts.
        assert sdk.posts == [] and sdk.cancels == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()
